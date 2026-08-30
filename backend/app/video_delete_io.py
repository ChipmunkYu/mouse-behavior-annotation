"""Crash-recoverable filesystem protocol for hard deleting a video.

The manifest deliberately contains only controlled root names and relative keys.  Database
work is owned by the caller: quarantine files before its transaction, restore on rollback,
and call :meth:`VideoDeleteIO.purge` only after the database commit.
"""
from __future__ import annotations

import json
import os
import re
import stat
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePath
from typing import Callable, Iterable, Mapping

from .cleanup_io import trusted_root
from .config import Settings


MANIFEST_VERSION = 2
_OPERATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_TABLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# Freeze platform capabilities while these names still refer to the original stdlib
# functions. Tests deliberately replace os.unlink/os.rename to inject failures; those
# replacements must not change whether descriptor-relative operations are available.
_DESCRIPTOR_DIRECTORY_OPERATIONS_SUPPORTED = (
    os.name == "posix"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and all(
        function in os.supports_dir_fd
        for function in (os.open, os.stat, os.unlink, os.rmdir, os.rename, os.mkdir)
    )
    and os.stat in os.supports_follow_symlinks
    # Descriptor traversal currently calls listdir(fd); scandir is path-based only.
    and os.listdir in os.supports_fd
)


class VideoDeleteIOError(RuntimeError):
    """A safe-to-report protocol failure (message never includes an absolute path)."""


@dataclass(frozen=True)
class DeletePath:
    root_kind: str
    relative_key: str
    path_kind: str = "file"


@dataclass(frozen=True)
class DeleteTreeIdentity:
    relative_key: str
    kind: str
    size: int
    mtime_ns: int
    device: int
    inode: int


@dataclass(frozen=True)
class DeleteIdentity:
    kind: str
    size: int
    mtime_ns: int
    device: int
    inode: int
    tree: tuple[DeleteTreeIdentity, ...] = ()


@dataclass
class DeleteEntry:
    root_kind: str
    relative_key: str
    quarantine_key: str
    path_kind: str
    identity: DeleteIdentity | None
    state: str = "pending"


@dataclass
class DeleteManifest:
    version: int
    operation_id: str
    video_id: int
    phase: str
    entries: list[DeleteEntry]
    project_id: int | None = None
    frozen_ids_by_table: tuple[tuple[str, tuple[int, ...]], ...] = ()
    terminal_job_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class RecoveryResult:
    operation_id: str
    video_id: int
    action: str
    ok: bool
    error: str | None = None


class VideoDeleteIO:
    """Quarantine/restore/purge protocol restricted to ``Settings`` data roots."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.data_dir = Path(settings.data_dir)
        self.roots: Mapping[str, Path] = {
            "videos": settings.videos_dir,
            "display_proxies": settings.display_proxies_dir,
            "exports": settings.exports_dir,
            "clips": settings.clips_dir,
            "thumbnails": settings.thumbnails_dir,
            "import_batches": settings.import_batches_dir,
            "detection_imports": settings.detection_imports_dir,
        }
        self.quarantine_dir = self.data_dir / "video-delete-quarantine"

    def prepare(
        self,
        video_id: int,
        paths: Iterable[DeletePath | tuple[str, str]],
        *,
        operation_id: str | None = None,
        project_id: int | None = None,
        frozen_ids_by_table: Mapping[str, Iterable[int]] | None = None,
        terminal_job_ids: Iterable[int] = (),
    ) -> DeleteManifest:
        """Validate, deduplicate and persist an identity-frozen manifest."""
        self._require_positive_id(video_id, "video-id-invalid")
        if project_id is not None:
            self._require_positive_id(project_id, "project-id-invalid")
        frozen_ids = self._freeze_ids_by_table(frozen_ids_by_table or {})
        terminal_ids = self._freeze_ids(terminal_job_ids, "terminal-job-ids-invalid")
        operation_id = operation_id or uuid.uuid4().hex
        self._validate_operation_id(operation_id)
        if self._lexists(self._operation_dir(operation_id)):
            raise VideoDeleteIOError("operation-already-exists")

        entries: list[DeleteEntry] = []
        seen: set[tuple[str, str]] = set()
        for ordinal, supplied in enumerate(paths):
            item = supplied if isinstance(supplied, DeletePath) else DeletePath(*supplied)
            source, normalized = self._resolve(item.root_kind, item.relative_key)
            dedupe_key = (item.root_kind, os.path.normcase(normalized))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            if item.path_kind not in {"file", "directory"}:
                raise VideoDeleteIOError("path-kind-invalid")
            identity = self._identity(source, missing_ok=True, expected_kind=item.path_kind)
            entries.append(
                DeleteEntry(
                    root_kind=item.root_kind,
                    relative_key=normalized,
                    quarantine_key=f"files/{ordinal:06d}",
                    path_kind=item.path_kind,
                    identity=identity,
                    state="absent" if identity is None else "pending",
                )
            )
        self._operation_dir(operation_id, create=True)
        manifest = DeleteManifest(
            MANIFEST_VERSION,
            operation_id,
            video_id,
            "prepared",
            entries,
            project_id,
            frozen_ids,
            terminal_ids,
        )
        self._write_manifest(manifest)
        return manifest

    def quarantine(self, manifest: DeleteManifest | str) -> DeleteManifest:
        """Atomically move every present file aside; roll back in reverse on failure."""
        value = self._coerce_manifest(manifest)
        if value.phase not in {"prepared", "quarantining"}:
            raise VideoDeleteIOError("manifest-phase-invalid")
        value.phase = "quarantining"
        self._write_manifest(value)
        moved: list[DeleteEntry] = []
        try:
            for entry in value.entries:
                if entry.state == "absent":
                    continue
                source, _ = self._resolve(entry.root_kind, entry.relative_key)
                target = self._quarantine_path(value.operation_id, entry.quarantine_key)
                self._require_location(entry, source, target, expected="source")
                self._ensure_files_directory(value.operation_id)
                if source.stat().st_dev != target.parent.stat().st_dev:
                    raise VideoDeleteIOError("quarantine-not-same-device")
                os.replace(source, target)
                self._fsync_directory(source.parent)
                self._fsync_directory(target.parent)
                entry.state = "quarantined"
                moved.append(entry)
                self._write_manifest(value)
        except Exception as exc:
            try:
                self._restore_entries(value, reversed(moved))
                value.phase = "restored"
                self._write_manifest(value)
                self._remove_operation(value.operation_id)
            except Exception as restore_exc:
                raise VideoDeleteIOError(f"quarantine-failed;restore-failed:{self._reason(restore_exc)}") from exc
            raise VideoDeleteIOError(f"quarantine-failed:{self._reason(exc)}") from exc
        value.phase = "quarantined"
        self._write_manifest(value)
        return value

    def restore(self, manifest: DeleteManifest | str) -> DeleteManifest:
        """Restore quarantined files without ever replacing an existing path."""
        value = self._coerce_manifest(manifest)
        self._restore_entries(value, reversed(value.entries))
        value.phase = "restored"
        self._write_manifest(value)
        self._remove_operation(value.operation_id)
        return value

    def purge(self, manifest: DeleteManifest | str) -> None:
        """Permanently remove frozen files after the caller has committed DB deletion."""
        value = self._coerce_manifest(manifest)
        for entry in value.entries:
            if entry.identity is None:
                continue
            source, _ = self._resolve(entry.root_kind, entry.relative_key)
            target = self._quarantine_path(value.operation_id, entry.quarantine_key)
            source_exists, target_exists = self._lexists(source), self._lexists(target)
            if source_exists and target_exists:
                raise VideoDeleteIOError("both-locations-exist")
            candidate = target if target_exists else source if source_exists else None
            if candidate is None:
                continue
            if entry.path_kind == "directory":
                retrying = entry.state == "purging"
                if not self._descriptor_directory_operations_supported():
                    # Path based recursive deletion cannot be made safe against a directory
                    # being exchanged for a symlink/reparse point after validation.
                    raise VideoDeleteIOError("directory-descriptor-operations-unavailable")
                entry.state = "purging"
                value.phase = "purging"
                self._write_manifest(value)
                self._remove_directory_tree(candidate, entry.identity, allow_missing=retrying)
            else:
                self._require_identity(candidate, entry.identity)
                candidate.unlink()
            self._fsync_directory(candidate.parent)
            entry.state = "purged"
            value.phase = "purging"
            self._write_manifest(value)
        value.phase = "purged"
        self._write_manifest(value)
        self._remove_operation(value.operation_id)

    def recover(self, video_exists: Callable[[int], bool]) -> list[RecoveryResult]:
        """Recover every durable manifest: restore existing videos, purge deleted ones."""
        if not self.quarantine_dir.exists():
            return []
        self._ensure_directory(self.quarantine_dir)
        results: list[RecoveryResult] = []
        for child in sorted(self.quarantine_dir.iterdir(), key=lambda path: path.name):
            try:
                child_stat = child.lstat()
            except OSError:
                continue
            if (self._is_link_or_reparse(child_stat) or not stat.S_ISDIR(child_stat.st_mode)
                    or not _OPERATION_RE.fullmatch(child.name)):
                continue
            try:
                if not self._lexists(child / "manifest.json"):
                    self._remove_safe_orphan(child)
                    results.append(RecoveryResult(child.name, -1, "orphan-removed", True))
                    continue
                manifest = self.load(child.name)
                exists = bool(video_exists(manifest.video_id))
                if exists:
                    self.restore(manifest)
                    action = "restored"
                else:
                    self.purge(manifest)
                    action = "purged"
                results.append(RecoveryResult(child.name, manifest.video_id, action, True))
            except Exception as exc:
                video_id = locals().get("manifest").video_id if "manifest" in locals() else -1
                results.append(RecoveryResult(child.name, video_id, "stopped", False, self._reason(exc)))
            finally:
                if "manifest" in locals():
                    del manifest
        return results

    def load(self, operation_id: str) -> DeleteManifest:
        self._validate_operation_id(operation_id)
        path = self._operation_dir(operation_id) / "manifest.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("version") != MANIFEST_VERSION or raw.get("operation_id") != operation_id:
                raise ValueError
            entries = [
                DeleteEntry(
                    root_kind=item["root_kind"],
                    relative_key=item["relative_key"],
                    quarantine_key=item["quarantine_key"],
                    path_kind=item["path_kind"],
                    identity=self._decode_identity(item["identity"]),
                    state=item["state"],
                )
                for item in raw["entries"]
            ]
            video_id = raw["video_id"]
            self._require_positive_id(video_id, "manifest-invalid")
            project_id = raw.get("project_id")
            if project_id is not None:
                self._require_positive_id(project_id, "manifest-invalid")
            frozen_raw = raw.get("frozen_ids_by_table", {})
            if not isinstance(frozen_raw, dict):
                raise ValueError
            frozen_ids = self._freeze_ids_by_table(frozen_raw, error="manifest-invalid")
            terminal_ids = self._freeze_ids(raw.get("terminal_job_ids", []), "manifest-invalid")
            manifest = DeleteManifest(
                raw["version"], operation_id, video_id, raw["phase"], entries,
                project_id, frozen_ids, terminal_ids,
            )
            for entry in entries:
                if entry.path_kind not in {"file", "directory"}:
                    raise ValueError
                self._resolve(entry.root_kind, entry.relative_key)
                self._quarantine_path(operation_id, entry.quarantine_key)
            return manifest
        except VideoDeleteIOError:
            raise
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise VideoDeleteIOError("manifest-invalid") from exc

    def _restore_entries(self, manifest: DeleteManifest, entries: Iterable[DeleteEntry]) -> None:
        for entry in entries:
            if entry.identity is None:
                continue
            source, _ = self._resolve(entry.root_kind, entry.relative_key)
            target = self._quarantine_path(manifest.operation_id, entry.quarantine_key)
            source_exists, target_exists = self._lexists(source), self._lexists(target)
            if source_exists and target_exists:
                raise VideoDeleteIOError("both-locations-exist")
            if source_exists:
                if entry.path_kind == "directory":
                    if not self._descriptor_directory_operations_supported():
                        raise VideoDeleteIOError("directory-descriptor-operations-unavailable")
                    self._validate_directory_descriptor(source, entry.identity)
                else:
                    self._require_identity(source, entry.identity)
                entry.state = "restored"
                continue
            if not target_exists:
                raise VideoDeleteIOError("file-missing-from-both-locations")
            if entry.path_kind == "directory":
                if not self._descriptor_directory_operations_supported():
                    raise VideoDeleteIOError("directory-descriptor-operations-unavailable")
                self._restore_directory(entry, source, target)
                entry.state = "restored"
                self._write_manifest(manifest)
                continue
            self._require_identity(target, entry.identity)
            source.parent.mkdir(parents=True, exist_ok=True)
            checked, normalized = self._resolve(entry.root_kind, entry.relative_key)
            if checked != source or normalized != entry.relative_key or self._lexists(source):
                raise VideoDeleteIOError("restore-destination-changed")
            os.replace(target, source)
            self._fsync_directory(target.parent)
            self._fsync_directory(source.parent)
            entry.state = "restored"
            self._write_manifest(manifest)

    def _require_location(self, entry: DeleteEntry, source: Path, target: Path, *, expected: str) -> None:
        source_exists, target_exists = self._lexists(source), self._lexists(target)
        if source_exists and target_exists:
            raise VideoDeleteIOError("both-locations-exist")
        candidate = source if expected == "source" else target
        if not self._lexists(candidate):
            raise VideoDeleteIOError(f"expected-{expected}-missing")
        if entry.identity is None:
            raise VideoDeleteIOError("identity-missing")
        self._require_identity(candidate, entry.identity)

    @staticmethod
    def _identity(path: Path, *, missing_ok: bool = False,
                  expected_kind: str | None = None) -> DeleteIdentity | None:
        try:
            value = path.lstat()
        except FileNotFoundError:
            if missing_ok:
                return None
            raise VideoDeleteIOError("file-missing")
        if VideoDeleteIO._is_link_or_reparse(value):
            raise VideoDeleteIOError("symlink-rejected")
        kind = "file" if stat.S_ISREG(value.st_mode) else "directory" if stat.S_ISDIR(value.st_mode) else None
        if kind is None:
            raise VideoDeleteIOError("untrusted-file-type")
        if expected_kind is not None and kind != expected_kind:
            raise VideoDeleteIOError("path-kind-mismatch")
        tree = VideoDeleteIO._directory_tree_identity(path) if kind == "directory" else ()
        if not value.st_dev or not value.st_ino:
            raise VideoDeleteIOError("stable-identity-unavailable")
        return DeleteIdentity(kind, value.st_size, value.st_mtime_ns, value.st_dev, value.st_ino, tree)

    def _require_identity(self, path: Path, identity: DeleteIdentity) -> None:
        if self._identity(path, expected_kind=identity.kind) != identity:
            raise VideoDeleteIOError("identity-mismatch")

    def _require_directory_identity(self, path: Path, identity: DeleteIdentity,
                                    *, allow_missing: bool) -> None:
        value = path.lstat()
        if (self._is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode)
                or value.st_dev != identity.device or value.st_ino != identity.inode):
            raise VideoDeleteIOError("identity-mismatch")
        current = {item.relative_key: item for item in self._directory_tree_identity(path)}
        expected = {item.relative_key: item for item in identity.tree}
        for key, item in current.items():
            frozen = expected.get(key)
            if frozen is None:
                raise VideoDeleteIOError("identity-mismatch")
            if item.kind == "directory" and allow_missing:
                if (frozen.kind, frozen.device, frozen.inode) != (item.kind, item.device, item.inode):
                    raise VideoDeleteIOError("identity-mismatch")
            elif item != frozen:
                raise VideoDeleteIOError("identity-mismatch")
        if not allow_missing and current != expected:
            raise VideoDeleteIOError("identity-mismatch")

    @staticmethod
    def _is_link_or_reparse(value: os.stat_result) -> bool:
        attributes = getattr(value, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return stat.S_ISLNK(value.st_mode) or bool(attributes & reparse)

    @staticmethod
    def _validate_directory_tree(root: Path) -> None:
        """Reject links/reparse points and special nodes without following them."""
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                children = list(os.scandir(directory))
            except OSError as exc:
                raise VideoDeleteIOError("directory-check-failed") from exc
            for child in children:
                try:
                    value = child.stat(follow_symlinks=False)
                except OSError as exc:
                    raise VideoDeleteIOError("directory-check-failed") from exc
                if VideoDeleteIO._is_link_or_reparse(value):
                    raise VideoDeleteIOError("symlink-rejected")
                if stat.S_ISDIR(value.st_mode):
                    pending.append(Path(child.path))
                elif not stat.S_ISREG(value.st_mode):
                    raise VideoDeleteIOError("untrusted-file-type")

    @staticmethod
    def _directory_tree_identity(root: Path) -> tuple[DeleteTreeIdentity, ...]:
        VideoDeleteIO._validate_directory_tree(root)
        result: list[DeleteTreeIdentity] = []
        pending = [root]
        while pending:
            directory = pending.pop()
            for child in os.scandir(directory):
                value = child.stat(follow_symlinks=False)
                path = Path(child.path)
                kind = "directory" if stat.S_ISDIR(value.st_mode) else "file"
                result.append(DeleteTreeIdentity(
                    path.relative_to(root).as_posix(), kind, value.st_size, value.st_mtime_ns,
                    value.st_dev, value.st_ino,
                ))
                if kind == "directory":
                    pending.append(path)
        return tuple(sorted(result, key=lambda item: item.relative_key))

    @staticmethod
    def _decode_identity(raw: object) -> DeleteIdentity | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError
        values = dict(raw)
        tree = values.get("tree", [])
        if not isinstance(tree, list):
            raise ValueError
        values["tree"] = tuple(DeleteTreeIdentity(**item) for item in tree)
        return DeleteIdentity(**values)

    @staticmethod
    def _descriptor_directory_operations_supported() -> bool:
        """Whether Python exposes the primitives needed for no-follow dir-fd traversal."""
        return _DESCRIPTOR_DIRECTORY_OPERATIONS_SUPPORTED

    @staticmethod
    def _directory_open_flags() -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def _open_directory_target(self, root: Path) -> tuple[int, str, int]:
        """Open ``root`` from a trusted configured anchor, never following a child link."""
        anchors = [self.quarantine_dir, *self.roots.values()]
        anchor = next((Path(item) for item in anchors if root.is_relative_to(Path(item))), None)
        if anchor is None or root == anchor:
            raise VideoDeleteIOError("path-out-of-bounds")
        try:
            parent_fd = os.open(anchor, self._directory_open_flags())
            parts = root.relative_to(anchor).parts
            for part in parts[:-1]:
                child_fd = os.open(part, self._directory_open_flags(), dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = child_fd
            leaf = parts[-1]
            root_fd = os.open(leaf, self._directory_open_flags(), dir_fd=parent_fd)
            return parent_fd, leaf, root_fd
        except OSError as exc:
            if "parent_fd" in locals():
                os.close(parent_fd)
            raise VideoDeleteIOError("directory-open-failed") from exc

    @staticmethod
    def _tree_item(relative_key: str, kind: str, value: os.stat_result) -> DeleteTreeIdentity:
        return DeleteTreeIdentity(
            relative_key, kind, value.st_size, value.st_mtime_ns, value.st_dev, value.st_ino,
        )

    def _check_open_directory(self, descriptor: int, frozen: DeleteTreeIdentity | DeleteIdentity,
                              *, allow_changed_metadata: bool) -> None:
        value = os.fstat(descriptor)
        if (self._is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode)
                or value.st_dev != frozen.device or value.st_ino != frozen.inode):
            raise VideoDeleteIOError("identity-mismatch")
        if not allow_changed_metadata and (
            value.st_size != frozen.size or value.st_mtime_ns != frozen.mtime_ns
        ):
            raise VideoDeleteIOError("identity-mismatch")

    def _walk_frozen_directory(self, descriptor: int, relative: str,
                               expected: Mapping[str, DeleteTreeIdentity], *,
                               allow_missing: bool, delete: bool) -> None:
        try:
            names = os.listdir(descriptor)
        except OSError as exc:
            raise VideoDeleteIOError("directory-check-failed") from exc
        expected_names = {
            key[len(relative) + 1:].split("/", 1)[0] if relative else key.split("/", 1)[0]
            for key in expected
            if (not relative and "/" not in key)
            or (relative and key.startswith(relative + "/") and "/" not in key[len(relative) + 1:])
        }
        if set(names) - expected_names or (not allow_missing and set(names) != expected_names):
            raise VideoDeleteIOError("identity-mismatch")
        for name in sorted(names):
            key = f"{relative}/{name}" if relative else name
            frozen = expected.get(key)
            if frozen is None:
                raise VideoDeleteIOError("identity-mismatch")
            try:
                value = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except OSError as exc:
                raise VideoDeleteIOError("directory-check-failed") from exc
            if self._is_link_or_reparse(value):
                raise VideoDeleteIOError("symlink-rejected")
            kind = "directory" if stat.S_ISDIR(value.st_mode) else "file" if stat.S_ISREG(value.st_mode) else None
            if kind is None:
                raise VideoDeleteIOError("untrusted-file-type")
            actual = self._tree_item(key, kind, value)
            if kind == "directory" and allow_missing:
                if (actual.kind, actual.device, actual.inode) != (frozen.kind, frozen.device, frozen.inode):
                    raise VideoDeleteIOError("identity-mismatch")
            elif actual != frozen:
                raise VideoDeleteIOError("identity-mismatch")

            self._before_open_purge_child(key)
            if kind == "directory":
                try:
                    child_fd = os.open(name, self._directory_open_flags(), dir_fd=descriptor)
                except OSError as exc:
                    raise VideoDeleteIOError("directory-open-failed") from exc
                try:
                    self._check_open_directory(child_fd, frozen, allow_changed_metadata=allow_missing)
                    self._walk_frozen_directory(
                        child_fd, key, expected, allow_missing=allow_missing, delete=delete,
                    )
                finally:
                    os.close(child_fd)
                if delete:
                    current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if (self._is_link_or_reparse(current) or current.st_dev != frozen.device
                            or current.st_ino != frozen.inode or not stat.S_ISDIR(current.st_mode)):
                        raise VideoDeleteIOError("identity-mismatch")
                    os.rmdir(name, dir_fd=descriptor)
            elif delete:
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if self._tree_item(key, "file", current) != frozen:
                    raise VideoDeleteIOError("identity-mismatch")
                os.unlink(name, dir_fd=descriptor)

    def _before_open_purge_child(self, _relative_key: str) -> None:
        """Fault-injection barrier; production intentionally does nothing."""

    def _remove_directory_tree(self, root: Path, identity: DeleteIdentity, *, allow_missing: bool) -> None:
        """Purge through pinned directory descriptors; never recurse through path names."""
        parent_fd, leaf, root_fd = self._open_directory_target(root)
        try:
            self._check_open_directory(root_fd, identity, allow_changed_metadata=allow_missing)
            expected = {item.relative_key: item for item in identity.tree}
            self._walk_frozen_directory(root_fd, "", expected, allow_missing=allow_missing, delete=True)
            current = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if (self._is_link_or_reparse(current) or current.st_dev != identity.device
                    or current.st_ino != identity.inode or not stat.S_ISDIR(current.st_mode)):
                raise VideoDeleteIOError("identity-mismatch")
            os.rmdir(leaf, dir_fd=parent_fd)
        finally:
            os.close(root_fd)
            os.close(parent_fd)

    def _validate_directory_descriptor(self, root: Path, identity: DeleteIdentity) -> None:
        parent_fd, _leaf, root_fd = self._open_directory_target(root)
        try:
            self._check_open_directory(root_fd, identity, allow_changed_metadata=False)
            expected = {item.relative_key: item for item in identity.tree}
            self._walk_frozen_directory(root_fd, "", expected, allow_missing=False, delete=False)
        finally:
            os.close(root_fd)
            os.close(parent_fd)

    def _open_verified_directory_name(self, parent_fd: int, leaf: str, pinned_fd: int,
                                      identity: DeleteIdentity) -> int:
        """Pin ``leaf`` without following it and match both frozen and already-pinned identity."""
        try:
            descriptor = os.open(leaf, self._directory_open_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise VideoDeleteIOError("identity-mismatch") from exc
        try:
            self._check_open_directory(descriptor, identity, allow_changed_metadata=False)
            current = os.fstat(descriptor)
            pinned = os.fstat(pinned_fd)
            if (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode)) != (
                pinned.st_dev, pinned.st_ino, stat.S_IFMT(pinned.st_mode),
            ):
                raise VideoDeleteIOError("identity-mismatch")
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    def _remove_unverified_restore_destination(self, source_parent_fd: int, source_leaf: str,
                                               quarantine_parent_fd: int) -> None:
        """Remove a non-directory node or move a directory aside, without traversing it."""
        try:
            value = os.stat(source_leaf, dir_fd=source_parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        try:
            if self._is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
                os.unlink(source_leaf, dir_fd=source_parent_fd)
            else:
                diagnostic_leaf = f"restore-rejected-{uuid.uuid4().hex}"
                os.rename(
                    source_leaf,
                    diagnostic_leaf,
                    src_dir_fd=source_parent_fd,
                    dst_dir_fd=quarantine_parent_fd,
                )
            try:
                os.stat(source_leaf, dir_fd=source_parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                os.fsync(source_parent_fd)
                os.fsync(quarantine_parent_fd)
                return
            raise VideoDeleteIOError("directory-restore-cleanup-failed")
        except VideoDeleteIOError:
            raise
        except OSError as exc:
            raise VideoDeleteIOError("directory-restore-cleanup-failed") from exc

    def _before_restore_directory_rename(self, _entry: DeleteEntry) -> None:
        """Fault-injection barrier immediately before the final name/identity recheck."""

    def _restore_directory(self, entry: DeleteEntry, source: Path, target: Path) -> None:
        """Validate and rename a quarantined directory using pinned parent descriptors."""
        assert entry.identity is not None
        target_parent_fd, target_leaf, target_fd = self._open_directory_target(target)
        source_root = Path(self.roots[entry.root_kind])
        try:
            source_parent_fd = os.open(source_root, self._directory_open_flags())
        except OSError as exc:
            os.close(target_fd)
            os.close(target_parent_fd)
            raise VideoDeleteIOError("directory-restore-failed") from exc
        try:
            self._check_open_directory(target_fd, entry.identity, allow_changed_metadata=False)
            expected = {item.relative_key: item for item in entry.identity.tree}
            self._walk_frozen_directory(target_fd, "", expected, allow_missing=False, delete=False)
            parts = source.relative_to(source_root).parts
            for part in parts[:-1]:
                try:
                    child_fd = os.open(part, self._directory_open_flags(), dir_fd=source_parent_fd)
                except FileNotFoundError:
                    os.mkdir(part, dir_fd=source_parent_fd)
                    child_fd = os.open(part, self._directory_open_flags(), dir_fd=source_parent_fd)
                os.close(source_parent_fd)
                source_parent_fd = child_fd
            try:
                os.stat(parts[-1], dir_fd=source_parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise VideoDeleteIOError("restore-destination-changed")
            self._before_restore_directory_rename(entry)
            checked_fd = self._open_verified_directory_name(
                target_parent_fd, target_leaf, target_fd, entry.identity,
            )
            os.close(checked_fd)
            os.rename(target_leaf, parts[-1], src_dir_fd=target_parent_fd, dst_dir_fd=source_parent_fd)
            try:
                restored_fd = self._open_verified_directory_name(
                    source_parent_fd, parts[-1], target_fd, entry.identity,
                )
            except Exception as verification_exc:
                try:
                    self._remove_unverified_restore_destination(
                        source_parent_fd, parts[-1], target_parent_fd,
                    )
                except Exception as cleanup_exc:
                    raise VideoDeleteIOError(
                        f"directory-restore-verification-failed;cleanup-failed:{self._reason(cleanup_exc)}"
                    ) from verification_exc
                raise
            else:
                os.close(restored_fd)
            os.fsync(target_parent_fd)
            os.fsync(source_parent_fd)
        except OSError as exc:
            raise VideoDeleteIOError("directory-restore-failed") from exc
        finally:
            os.close(target_fd)
            os.close(target_parent_fd)
            os.close(source_parent_fd)

    @staticmethod
    def _lexists(path: Path) -> bool:
        try:
            path.lstat()
            return True
        except FileNotFoundError:
            return False

    def _resolve(self, root_kind: str, relative_key: str) -> tuple[Path, str]:
        root_input = self.roots.get(root_kind)
        if root_input is None:
            raise VideoDeleteIOError("unknown-root-kind")
        if not isinstance(relative_key, str) or not relative_key or "\x00" in relative_key:
            raise VideoDeleteIOError("relative-key-invalid")
        raw = Path(relative_key)
        if raw.is_absolute() or any(part in {"", ".", ".."} for part in PurePath(relative_key).parts):
            raise VideoDeleteIOError("path-out-of-bounds")
        root, reason = trusted_root(self.data_dir, root_input)
        if root is None:
            raise VideoDeleteIOError(reason or "root-untrusted")
        candidate = Path(os.path.abspath(root / raw))
        if candidate == root or not candidate.is_relative_to(root):
            raise VideoDeleteIOError("path-out-of-bounds")
        current = root
        for part in candidate.relative_to(root).parts:
            current /= part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise VideoDeleteIOError("path-check-failed") from exc
            value = current.lstat()
            if self._is_link_or_reparse(value):
                raise VideoDeleteIOError("symlink-rejected")
        normalized = candidate.relative_to(root).as_posix()
        return candidate, normalized

    def _operation_dir(self, operation_id: str, *, create: bool = False) -> Path:
        self._validate_operation_id(operation_id)
        if create:
            quarantine_missing = not self._lexists(self.quarantine_dir)
            self.quarantine_dir.mkdir(parents=True, exist_ok=True)
            self._ensure_directory(self.quarantine_dir)
            if quarantine_missing:
                self._fsync_directory(self.data_dir)
            result = self.quarantine_dir / operation_id
            result.mkdir()
            self._ensure_directory(result)
            self._fsync_directory(self.quarantine_dir)
            return result
        return self.quarantine_dir / operation_id

    def _ensure_files_directory(self, operation_id: str) -> Path:
        operation_dir = self._operation_dir(operation_id)
        result = operation_dir / "files"
        if not self._lexists(result):
            result.mkdir()
            self._ensure_directory(result)
            self._fsync_directory(operation_dir)
        else:
            self._ensure_directory(result)
        return result

    def _quarantine_path(self, operation_id: str, key: str) -> Path:
        if not re.fullmatch(r"files/[0-9]{6}", key):
            raise VideoDeleteIOError("quarantine-key-invalid")
        result = self._operation_dir(operation_id) / Path(key)
        if not result.is_relative_to(self._operation_dir(operation_id)):
            raise VideoDeleteIOError("quarantine-path-invalid")
        return result

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        mode = path.lstat().st_mode
        value = path.lstat()
        if VideoDeleteIO._is_link_or_reparse(value) or not stat.S_ISDIR(mode):
            raise VideoDeleteIOError("protocol-directory-untrusted")

    def _write_manifest(self, manifest: DeleteManifest) -> None:
        operation_dir = self._operation_dir(manifest.operation_id)
        self._ensure_directory(operation_dir)
        path = operation_dir / "manifest.json"
        temporary = operation_dir / "manifest.json.tmp"
        payload = asdict(manifest)
        payload["frozen_ids_by_table"] = {
            table: list(ids) for table, ids in manifest.frozen_ids_by_table
        }
        payload["terminal_job_ids"] = list(manifest.terminal_job_ids)
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._fsync_directory(operation_dir)
        finally:
            if self._lexists(temporary):
                temporary.unlink()
                self._fsync_directory(operation_dir)

    def _remove_safe_orphan(self, operation_dir: Path) -> None:
        """Remove only a provably pre-manifest operation directory."""
        self._ensure_directory(operation_dir)
        children = list(operation_dir.iterdir())
        if not children:
            pass
        elif len(children) == 1 and children[0].name == "manifest.json.tmp":
            temporary = children[0]
            mode = temporary.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise VideoDeleteIOError("orphan-operation-unsafe")
            temporary.unlink()
            self._fsync_directory(operation_dir)
        else:
            raise VideoDeleteIOError("orphan-operation-unsafe")
        operation_dir.rmdir()
        self._fsync_directory(self.quarantine_dir)

    def _remove_operation(self, operation_id: str) -> None:
        operation_dir = self._operation_dir(operation_id)
        files_dir = operation_dir / "files"
        if files_dir.exists():
            self._ensure_directory(files_dir)
            if any(files_dir.iterdir()):
                raise VideoDeleteIOError("quarantine-not-empty")
            files_dir.rmdir()
            self._fsync_directory(operation_dir)
        manifest_path = operation_dir / "manifest.json"
        if self._lexists(manifest_path):
            manifest_path.unlink()
            self._fsync_directory(operation_dir)
        operation_dir.rmdir()
        self._fsync_directory(self.quarantine_dir)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _coerce_manifest(self, value: DeleteManifest | str) -> DeleteManifest:
        return self.load(value) if isinstance(value, str) else self.load(value.operation_id)

    @staticmethod
    def _require_positive_id(value: object, error: str) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise VideoDeleteIOError(error)

    @classmethod
    def _freeze_ids(cls, values: Iterable[int], error: str) -> tuple[int, ...]:
        if isinstance(values, (str, bytes)):
            raise VideoDeleteIOError(error)
        try:
            result = tuple(values)
        except TypeError as exc:
            raise VideoDeleteIOError(error) from exc
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in result):
            raise VideoDeleteIOError(error)
        return tuple(sorted(set(result)))

    @classmethod
    def _freeze_ids_by_table(
        cls,
        values: Mapping[str, Iterable[int]],
        *,
        error: str = "frozen-ids-invalid",
    ) -> tuple[tuple[str, tuple[int, ...]], ...]:
        if not isinstance(values, Mapping):
            raise VideoDeleteIOError(error)
        result: list[tuple[str, tuple[int, ...]]] = []
        for table, ids in values.items():
            if not isinstance(table, str) or not _TABLE_NAME_RE.fullmatch(table):
                raise VideoDeleteIOError(error)
            result.append((table, cls._freeze_ids(ids, error)))
        return tuple(sorted(result))

    @staticmethod
    def _validate_operation_id(value: str) -> None:
        if not isinstance(value, str) or not _OPERATION_RE.fullmatch(value):
            raise VideoDeleteIOError("operation-id-invalid")

    @staticmethod
    def _reason(exc: Exception) -> str:
        return str(exc) if isinstance(exc, VideoDeleteIOError) else type(exc).__name__


def recover_video_deletions(settings: Settings, video_exists: Callable[[int], bool]) -> list[RecoveryResult]:
    """Convenience startup hook without coupling this module to a database."""
    return VideoDeleteIO(settings).recover(video_exists)
