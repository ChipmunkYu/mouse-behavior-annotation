"""Filesystem and JSONL primitives shared by cleanup producers and consumers."""
from __future__ import annotations

import json
import os
import shutil
import stat
import threading
from pathlib import Path
from typing import Any


_CLEANUP_LOG_LOCK = threading.RLock()


def trusted_root(data_dir: Path, root_dir: Path) -> tuple[Path | None, str | None]:
    """Anchor below resolved data_dir, rejecting symlinks in every child-root component."""
    anchor_input = Path(os.path.abspath(data_dir))
    anchor = anchor_input.resolve()
    root_input = Path(os.path.abspath(root_dir))
    try:
        relative = root_input.relative_to(anchor_input)
    except ValueError:
        try:
            relative = root_input.relative_to(anchor)
        except ValueError:
            return None, "root-out-of-bounds"
    if not relative.parts:
        return None, "root-is-anchor"
    current = anchor
    for part in relative.parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError:
            return None, "root-check-failed"
        if stat.S_ISLNK(mode):
            return None, "root-symlink"
        if not stat.S_ISDIR(mode):
            return None, "root-component-not-directory"
    return anchor / relative, None


def safe_path(
    stored: str | Path | None, root_dir: Path, data_dir: Path
) -> tuple[Path | None, str | None]:
    """Return an un-resolved path below a trusted root with no existing symlink component."""
    if not stored:
        return None, "missing-path"
    root, reason = trusted_root(data_dir, root_dir)
    if root is None:
        return None, reason
    raw = Path(stored)
    if raw.is_absolute():
        absolute = Path(os.path.abspath(raw))
        lexical_root = Path(os.path.abspath(root_dir))
        try:
            relative = absolute.relative_to(lexical_root)
        except ValueError:
            try:
                relative = absolute.relative_to(root)
            except ValueError:
                return None, "out-of-bounds"
        path = root / relative
    else:
        path = Path(os.path.abspath(root / raw))
    if path == root or not path.is_relative_to(root):
        return None, "out-of-bounds"
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return None, "symlink"
        except FileNotFoundError:
            continue
        except OSError:
            return None, "path-check-failed"
    return path, None


def _snapshot(path: Path) -> tuple[int, int, int] | None:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    return value.st_dev, value.st_ino, value.st_mode


def remove_checked(
    path: Path,
    *,
    directory: bool = False,
    root_dir: Path | None = None,
    data_dir: Path | None = None,
) -> tuple[bool, str | None]:
    """Delete only if lstat identity/type survives a final check; never follow symlinks."""
    if root_dir is not None and data_dir is not None:
        checked, reason = safe_path(str(path), root_dir, data_dir)
        if checked != path:
            return False, reason or "path-replaced"
    before = _snapshot(path)
    if before is None:
        return False, None
    mode = before[2]
    if stat.S_ISLNK(mode) or (directory and not stat.S_ISDIR(mode)):
        return False, "replaced-or-symlink"
    if not directory and not stat.S_ISREG(mode):
        return False, "replaced-or-symlink"
    if directory:
        for current, directories, files in os.walk(path, followlinks=False):
            for name in directories + files:
                try:
                    if stat.S_ISLNK((Path(current) / name).lstat().st_mode):
                        return False, "internal-symlink"
                except (FileNotFoundError, OSError):
                    return False, "path-check-failed"
    if root_dir is not None and data_dir is not None:
        checked, reason = safe_path(str(path), root_dir, data_dir)
        if checked != path:
            return False, reason or "path-replaced"
    if _snapshot(path) != before:
        return False, "path-replaced"
    try:
        if directory:
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        return False, str(exc)
    return True, None


def append_cleanup_issues(log_path: Path, issues: list[dict[str, Any]]) -> None:
    if not issues:
        return
    with _CLEANUP_LOG_LOCK:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            for entry in issues:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def update_cleanup_log(log_path: Path, transform, *, write: bool = True):
    """Serialize read/rewrite with all in-process appenders."""
    with _CLEANUP_LOG_LOCK:
        lines = log_path.read_text(encoding="utf-8").splitlines()
        replacement = transform(lines)
        if not write:
            return replacement
        temporary = log_path.with_name(log_path.name + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as fh:
                for line in replacement:
                    fh.write(line + "\n")
            os.replace(temporary, log_path)
        finally:
            temporary.unlink(missing_ok=True)
        return replacement
