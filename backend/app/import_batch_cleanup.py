"""Conservative cleanup for unfinished three-file video import batches."""
from __future__ import annotations

import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, exists
from sqlalchemy.orm import Session

from .cleanup_io import append_cleanup_issues, remove_checked, safe_path, trusted_root
from .models import (
    Annotation,
    DetectionImport,
    DetectionSuppression,
    IdentityEdit,
    Review,
    Submission,
    Video,
    VideoImportBatch,
)

CANCELLABLE_BATCH_STATUSES = ("uploading", "failed")


def _before_pristine_video_delete() -> None:
    """Test synchronization point between preflight and conditional DELETE."""


class BatchCleanupConflict(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class BatchFile:
    role: str
    stored: str
    path: Path
    root: Path


def _checked_file(role: str, stored: str | None, settings) -> BatchFile | None:
    if not stored:
        return None
    root = settings.videos_dir if role == "video" else settings.detection_imports_dir
    path, reason = safe_path(stored, root, settings.data_dir)
    trusted, root_reason = trusted_root(settings.data_dir, root)
    if path is None or trusted is None or path.parent != trusted:
        raise BatchCleanupConflict(
            f"Batch {role} path is unsafe: {reason or root_reason or 'invalid-path'}"
        )
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        mode = None
    except OSError as exc:
        raise BatchCleanupConflict(f"Batch {role} path cannot be checked: {exc}") from exc
    if mode is not None and (stat.S_ISLNK(mode) or not stat.S_ISREG(mode)):
        raise BatchCleanupConflict(f"Batch {role} path is not a regular file")
    return BatchFile(role, stored, path, root)


def _same_path(stored: str | None, candidate: BatchFile, settings) -> bool:
    if not stored:
        return False
    root = settings.videos_dir if candidate.role == "video" else settings.detection_imports_dir
    path, reason = safe_path(stored, root, settings.data_dir)
    return reason is None and path == candidate.path


def _pristine_import_video(db: Session, video: Video) -> bool:
    """A failed completion may leave a draft Video, but no user work may be erased."""
    if (
        video.workflow_status != "draft"
        or video.status not in ("uploaded", "needs_transcode")
        or video.duration is not None
        or video.fps is not None
        or video.width is not None
        or video.height is not None
        or video.annotation_revision != 1
        or video.detection_import_revision != 0
        or video.identity_revision != 0
        or video.media_revision != 1
        or video.submitted_at is not None
        or video.approved_at is not None
        or video.approved_by is not None
    ):
        return False
    checks = (
        db.query(Annotation.id).filter(Annotation.video_id == video.id),
        db.query(Review.id).filter(Review.video_id == video.id),
        db.query(DetectionImport.id).filter(DetectionImport.video_id == video.id),
        db.query(IdentityEdit.id).filter(IdentityEdit.video_id == video.id),
        db.query(DetectionSuppression.id).filter(DetectionSuppression.video_id == video.id),
        db.query(Submission.id).filter(Submission.video_id == video.id),
    )
    return all(query.first() is None for query in checks)


def _preflight(
    db: Session, batch: VideoImportBatch, settings
) -> tuple[list[BatchFile], Video | None]:
    files = [
        item
        for item in (
            _checked_file("video", batch.video_path, settings),
            _checked_file("tracks", batch.tracks_path, settings),
            _checked_file("metadata", batch.metadata_path, settings),
        )
        if item is not None
    ]
    created_video = db.get(Video, batch.created_video_id) if batch.created_video_id else None
    if batch.created_video_id is not None and created_video is None:
        raise BatchCleanupConflict("Import batch references a missing created video")
    if created_video is not None:
        if created_video.project_id != batch.project_id or not _pristine_import_video(db, created_video):
            raise BatchCleanupConflict("Import batch video has already been consumed")
        video_file = next((item for item in files if item.role == "video"), None)
        if video_file is None or not _same_path(created_video.storage_path, video_file, settings):
            raise BatchCleanupConflict("Import batch video authority does not match its file")

    for item in files:
        if item.role == "video":
            consumers = db.query(Video.id, Video.storage_path).filter(
                Video.storage_path.is_not(None)
            ).all()
            if any(
                _same_path(stored, item, settings)
                and (created_video is None or video_id != created_video.id)
                for video_id, stored in consumers
            ):
                raise BatchCleanupConflict("Import batch video file is referenced by another video")
        else:
            consumers = db.query(
                DetectionImport.tracks_path, DetectionImport.metadata_path
            ).all()
            index = 0 if item.role == "tracks" else 1
            if any(_same_path(row[index], item, settings) for row in consumers):
                raise BatchCleanupConflict(f"Import batch {item.role} file has already been consumed")
    return files, created_video


def _shared_by_other_batch(
    db: Session, batch: VideoImportBatch, item: BatchFile, settings
) -> bool:
    columns = {
        "video": VideoImportBatch.video_path,
        "tracks": VideoImportBatch.tracks_path,
        "metadata": VideoImportBatch.metadata_path,
    }
    values = db.query(columns[item.role]).filter(VideoImportBatch.id != batch.id).all()
    return any(_same_path(stored, item, settings) for (stored,) in values)


def _restore_claim(db: Session, batch_id: int, original_status: str) -> None:
    try:
        db.rollback()
        now = datetime.utcnow()
        db.query(VideoImportBatch).filter(
            VideoImportBatch.id == batch_id,
            VideoImportBatch.status == "cancelling",
        ).update({
            VideoImportBatch.status: original_status,
            VideoImportBatch.updated_at: now,
        }, synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()


def cleanup_import_batch(
    db: Session,
    batch_id: int,
    project_id: int,
    settings,
    *,
    dry_run: bool = False,
    stale_before: datetime | None = None,
    allow_active_upload_slots: bool = False,
) -> dict[str, Any]:
    """Delete one safe unfinished batch using a committed conditional state claim."""
    batch = db.get(VideoImportBatch, batch_id)
    if batch is None or batch.project_id != project_id:
        raise LookupError("Import batch not found")
    original_status = batch.status
    if original_status not in CANCELLABLE_BATCH_STATUSES:
        raise BatchCleanupConflict(f"Import batch in status '{original_status}' cannot be cancelled")
    slot_states = (
        batch.video_upload_state,
        batch.tracks_upload_state,
        batch.metadata_upload_state,
    )
    if not allow_active_upload_slots and "uploading" in slot_states:
        raise BatchCleanupConflict("Import batch has a file upload in progress")
    if stale_before is not None and not batch.updated_at < stale_before:
        raise BatchCleanupConflict("Import batch is not stale")

    files, created_video = _preflight(db, batch, settings)
    retained = [item for item in files if _shared_by_other_batch(db, batch, item, settings)]
    deletable = [item for item in files if item not in retained]
    if dry_run:
        return {"batch_id": batch.id, "files": len(deletable), "retained_shared": len(retained)}

    filters = [
        VideoImportBatch.id == batch_id,
        VideoImportBatch.project_id == project_id,
        VideoImportBatch.status == original_status,
    ]
    if stale_before is not None:
        filters.append(VideoImportBatch.updated_at < stale_before)
    if not allow_active_upload_slots:
        filters.extend((
            VideoImportBatch.video_upload_state != "uploading",
            VideoImportBatch.tracks_upload_state != "uploading",
            VideoImportBatch.metadata_upload_state != "uploading",
        ))
    now = datetime.utcnow()
    claimed = db.query(VideoImportBatch).filter(*filters).update(
        {
            VideoImportBatch.status: "cancelling",
            VideoImportBatch.updated_at: now,
        }, synchronize_session=False
    )
    if claimed != 1:
        db.rollback()
        raise BatchCleanupConflict("Import batch state changed concurrently")
    # The claim must be visible before filesystem work; this also releases SQLite's write lock.
    db.commit()

    try:
        db.expire_all()
        batch = db.get(VideoImportBatch, batch_id)
        if batch is None or batch.status != "cancelling":
            raise BatchCleanupConflict("Import batch state changed concurrently")
        files, created_video = _preflight(db, batch, settings)
        retained = [item for item in files if _shared_by_other_batch(db, batch, item, settings)]
        deletable = [item for item in files if item not in retained]
        # A Video must be removed by one conditional statement.  This prevents
        # ORM cascades from silently deleting authority added after preflight.
        if created_video is not None:
            _before_pristine_video_delete()
            video_id = created_video.id
            removed_video = db.execute(
                delete(Video).where(
                    Video.id == video_id,
                    Video.project_id == batch.project_id,
                    Video.filename == batch.video_filename,
                    Video.storage_path == batch.video_path,
                    Video.status.in_(("uploaded", "needs_transcode")),
                    Video.duration.is_(None),
                    Video.fps.is_(None),
                    Video.width.is_(None),
                    Video.height.is_(None),
                    Video.workflow_status == "draft",
                    Video.annotation_revision == 1,
                    Video.detection_import_revision == 0,
                    Video.identity_revision == 0,
                    Video.media_revision == 1,
                    Video.submitted_at.is_(None),
                    Video.approved_at.is_(None),
                    Video.approved_by.is_(None),
                    ~exists().where(Annotation.video_id == video_id),
                    ~exists().where(Review.video_id == video_id),
                    ~exists().where(DetectionImport.video_id == video_id),
                    ~exists().where(IdentityEdit.video_id == video_id),
                    ~exists().where(DetectionSuppression.video_id == video_id),
                    ~exists().where(Submission.video_id == video_id),
                ).execution_options(synchronize_session=False)
            ).rowcount
            if removed_video != 1:
                raise BatchCleanupConflict("Import batch video changed concurrently")
        removed_batch = db.query(VideoImportBatch).filter(
            VideoImportBatch.id == batch_id,
            VideoImportBatch.status == "cancelling",
        ).delete(synchronize_session=False)
        if removed_batch != 1:
            raise BatchCleanupConflict("Import batch state changed concurrently")
        db.commit()
    except Exception:
        _restore_claim(db, batch_id, original_status)
        raise

    issues: list[dict[str, Any]] = []
    deleted = 0
    for item in deletable:
        removed, reason = remove_checked(
            item.path, root_dir=item.root, data_dir=settings.data_dir
        )
        deleted += int(removed)
        if reason is not None:
            issues.append({
                "kind": "import-batch-delete-failed",
                "batch_id": batch_id,
                "project_id": project_id,
                "role": item.role,
                "path": str(item.path),
                "reason": reason,
            })
    append_cleanup_issues(settings.cleanup_log, issues)
    return {
        "batch_id": batch_id,
        "files": deleted,
        "retained_shared": len(retained),
        "issues": issues,
    }


def cleanup_replaced_batch_file(
    db: Session, batch: VideoImportBatch, role: str, stored: str | None, settings
) -> None:
    """Remove an old slot generation only when no authority still names it."""
    if not stored:
        return
    issue: dict[str, Any] | None = None
    try:
        item = _checked_file(role, stored, settings)
        if item is None or _shared_by_other_batch(db, batch, item, settings):
            return
        if role == "video":
            referenced = any(
                _same_path(path, item, settings)
                for (path,) in db.query(Video.storage_path).filter(Video.storage_path.is_not(None)).all()
            )
        else:
            index = 0 if role == "tracks" else 1
            referenced = any(
                _same_path(row[index], item, settings)
                for row in db.query(
                    DetectionImport.tracks_path, DetectionImport.metadata_path
                ).all()
            )
        if referenced:
            return
        _removed, reason = remove_checked(
            item.path, root_dir=item.root, data_dir=settings.data_dir
        )
        if reason is not None:
            issue = {
                "kind": "import-batch-delete-failed", "batch_id": batch.id,
                "project_id": batch.project_id, "role": role,
                "path": str(item.path), "reason": reason,
            }
    except BatchCleanupConflict as exc:
        issue = {
            "kind": "unsafe-import-batch-path", "batch_id": batch.id,
            "project_id": batch.project_id, "role": role,
            "path": stored, "reason": exc.detail,
        }
    if issue is not None:
        append_cleanup_issues(settings.cleanup_log, [issue])
