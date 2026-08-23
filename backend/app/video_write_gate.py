"""Single Video-level SQLite write gate for all lifecycle/state writers."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .models import DetectionImport, Submission, Video
from .video_operation_gate import VideoOperationBusyError, VideoOperationGateCoordinator
from .video_operation_dependency import VIDEO_OPERATION_BUSY_DETAIL


@dataclass(frozen=True)
class VideoWriteState:
    video: Video
    detection_import: DetectionImport | None


def _before_video_lock() -> None:
    """Test synchronization point after optimistic reads and before UPDATE."""


def _after_video_lock() -> None:
    """Test fault-injection point after the no-op UPDATE acquired the write lock."""


def _is_locked_error(exc: OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


@contextmanager
def video_write_gate(
    db: Session,
    *,
    project_id: int,
    video_id: int,
    require_active_import: bool = False,
    expected_active_import_id: int | None = None,
    expected_detection_revision: int | None = None,
    expected_edit_version: int | None = None,
    expected_annotation_revision: int | None = None,
    expected_media_revision: int | None = None,
    expected_storage_path: str | None = None,
    allow_submitted: bool = False,
    operation_gate: VideoOperationGateCoordinator | None = None,
) -> Iterator[VideoWriteState]:
    """Serialize and validate a complete Video writer transaction."""
    try:
        _before_video_lock()
        gate_context = operation_gate.acquire(video_id) if operation_gate is not None else _null_gate()
        with gate_context:
            result = db.execute(
                update(Video)
                .where(Video.id == video_id, Video.project_id == project_id)
                .values(id=Video.id)
            )
            if result.rowcount != 1:
                raise HTTPException(status_code=404, detail="Video not found in this project")
            _after_video_lock()
            db.expire_all()
            video = db.get(Video, video_id)
            detection_import = (
                db.query(DetectionImport)
                .filter(DetectionImport.video_id == video_id, DetectionImport.active == True)
                .first()
            )
            if require_active_import and detection_import is None:
                raise HTTPException(status_code=400, detail="No detection import is active for this video")
            if (
                expected_active_import_id is not None
                and (detection_import is None or detection_import.id != expected_active_import_id)
            ):
                raise HTTPException(status_code=409, detail="Active detection import changed concurrently")
            if (
                expected_detection_revision is not None
                and video.detection_import_revision != expected_detection_revision
            ):
                raise HTTPException(status_code=409, detail="Detection import revision changed concurrently")
            if (
                expected_edit_version is not None
                and (detection_import is None or detection_import.edit_version != expected_edit_version)
            ):
                raise HTTPException(status_code=409, detail="Identity revision changed concurrently")
            if (
                expected_annotation_revision is not None
                and video.annotation_revision != expected_annotation_revision
            ):
                raise HTTPException(status_code=409, detail="Annotation revision changed concurrently")
            if expected_media_revision is not None and video.media_revision != expected_media_revision:
                raise HTTPException(status_code=409, detail="Video media revision changed concurrently")
            if expected_storage_path is not None and video.storage_path != expected_storage_path:
                raise HTTPException(status_code=409, detail="Video storage key changed concurrently")
            submitted = video.workflow_status == "submitted" or (
                db.query(Submission.id)
                .filter(Submission.video_id == video_id, Submission.status == "submitted")
                .first()
                is not None
            )
            if submitted and not allow_submitted:
                raise HTTPException(
                    status_code=409,
                    detail="Video is submitted and locked; withdraw before modifying it",
                )
            yield VideoWriteState(video=video, detection_import=detection_import)
    except VideoOperationBusyError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=VIDEO_OPERATION_BUSY_DETAIL) from exc
    except OperationalError as exc:
        db.rollback()
        if _is_locked_error(exc):
            raise HTTPException(
                status_code=409, detail="Video is being modified; retry the request"
            ) from exc
        raise
    except BaseException:
        db.rollback()
        raise


@contextmanager
def _null_gate() -> Iterator[None]:
    yield
