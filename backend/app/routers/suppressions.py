"""Compatibility suppression API backed by sparse DraftIdentityEdit state."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import project_access
from ..draft_detection_edits import (
    commit_suppress,
    undo_latest,
)
from ..models import DetectionImport, DraftIdentityEdit, Video
from ..schemas import SuppressionCreateRequest, SuppressionRevertRequest
from ..video_write_gate import video_write_gate

router = APIRouter(tags=["detection-suppressions"])
_EDIT_ROLES = {"owner", "admin", "annotator"}


def _require_editor(membership) -> None:
    if membership.role not in _EDIT_ROLES:
        raise HTTPException(status_code=403, detail="Only owner/admin/annotator can suppress tracks")


def _require_video(db: Session, video_id: int, project_id: int) -> Video:
    video = db.get(Video, video_id)
    if video is None or video.project_id != project_id:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


def _get_active_import(db: Session, video_id: int) -> DetectionImport:
    detection_import = (
        db.query(DetectionImport)
        .filter(DetectionImport.video_id == video_id, DetectionImport.active == True)
        .first()
    )
    if detection_import is None:
        raise HTTPException(status_code=400, detail="No active detection import for this video")
    return detection_import


@router.get("/api/projects/{project_id}/videos/{video_id}/detection-suppressions")
def list_active_suppressions(
    project_id: int,
    video_id: int,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> list[dict]:
    _require_video(db, video_id, project_id)
    detection_import = _get_active_import(db, video_id)
    edits = (
        db.query(DraftIdentityEdit)
        .filter(
            DraftIdentityEdit.detection_import_id == detection_import.id,
            DraftIdentityEdit.operation == "suppress_track",
        )
        .order_by(DraftIdentityEdit.applied_edit_version.desc())
        .all()
    )
    return [
        {
            "id": edit.id,
            "scope": "corrected_track",
            "result_identity_revision": edit.applied_edit_version,
            "created_at": edit.created_at,
            "frozen_detection_count": len(edit.changes),
        }
        for edit in edits
    ]


@router.post("/api/projects/{project_id}/videos/{video_id}/detection-suppressions")
def create_suppression(
    project_id: int,
    video_id: int,
    body: SuppressionCreateRequest,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> dict:
    _require_editor(access[1])
    with video_write_gate(
        db, project_id=project_id, video_id=video_id, require_active_import=True,
        expected_detection_revision=body.base_detection_import_revision,
        expected_edit_version=body.base_identity_revision,
    ) as state:
        return commit_suppress(
            db, state.detection_import, state.video, track_id=body.track_id,
            expected_version=body.base_identity_revision, operator_id=access[1].user_id,
        )


@router.post(
    "/api/projects/{project_id}/videos/{video_id}/detection-suppressions/{suppression_id}/revert"
)
def revert_suppression(
    project_id: int,
    video_id: int,
    suppression_id: int,
    body: SuppressionRevertRequest,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> dict:
    _require_editor(access[1])
    with video_write_gate(
        db, project_id=project_id, video_id=video_id, require_active_import=True,
        expected_detection_revision=body.base_detection_import_revision,
        expected_edit_version=body.base_identity_revision,
    ) as state:
        return undo_latest(
            db, state.detection_import, state.video, requested_edit_id=suppression_id,
            expected_operation="suppress_track", expected_version=body.base_identity_revision,
        )
