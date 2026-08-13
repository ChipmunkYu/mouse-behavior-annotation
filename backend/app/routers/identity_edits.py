"""Sparse draft identity Split/Merge and strict LIFO undo HTTP compatibility."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import project_access
from ..draft_detection_edits import (
    commit_merge,
    commit_split,
    merge_preview,
    split_preview,
    undo_latest,
)
from ..models import DetectionImport, DraftIdentityEdit, Video
from ..schemas import IdentityEditCheckRequest, IdentityEditCommitRequest, IdentityEditRevertRequest
from ..video_write_gate import video_write_gate

router = APIRouter(tags=["identity-edits"])
_EDIT_ROLES = {"owner", "admin", "annotator"}


def _require_editor(membership) -> None:
    if membership.role not in _EDIT_ROLES:
        raise HTTPException(status_code=403, detail="Only owner/admin/annotator can edit tracks")


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


@router.post("/api/projects/{project_id}/videos/{video_id}/identity-edits/check")
def check_identity_edit(
    project_id: int,
    video_id: int,
    body: IdentityEditCheckRequest,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> dict:
    _require_editor(access[1])
    _require_video(db, video_id, project_id)
    detection_import = _get_active_import(db, video_id)
    if body.operation == "split":
        if len(body.track_ids) != 1:
            raise HTTPException(status_code=400, detail="Split requires exactly one track_id")
        return split_preview(db, detection_import, body.track_ids[0], body.frame)
    return merge_preview(db, detection_import, body.track_ids)


@router.post("/api/projects/{project_id}/videos/{video_id}/identity-edits")
def commit_identity_edit(
    project_id: int,
    video_id: int,
    body: IdentityEditCommitRequest,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> dict:
    _require_editor(access[1])
    with video_write_gate(
        db,
        project_id=project_id,
        video_id=video_id,
        require_active_import=True,
        expected_detection_revision=body.base_detection_import_revision,
        expected_edit_version=body.base_identity_revision,
    ) as state:
        video, detection_import = state.video, state.detection_import
        if body.operation == "split":
            if len(body.track_ids) != 1 or body.frame is None:
                raise HTTPException(status_code=400, detail="Split requires exactly one track_id and frame")
            return commit_split(
                db, detection_import, video, track_id=body.track_ids[0], frame=body.frame,
                expected_version=body.base_identity_revision, operator_id=access[1].user_id,
            )
        return commit_merge(
            db, detection_import, video, track_ids=body.track_ids,
            expected_version=body.base_identity_revision, operator_id=access[1].user_id,
        )


@router.get("/api/projects/{project_id}/videos/{video_id}/identity-edits/history")
def get_identity_edit_history(
    project_id: int,
    video_id: int,
    limit: int = 1,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> list[dict]:
    video = _require_video(db, video_id, project_id)
    detection_import = _get_active_import(db, video_id)
    edits = (
        db.query(DraftIdentityEdit)
        .filter(DraftIdentityEdit.detection_import_id == detection_import.id)
        .order_by(DraftIdentityEdit.applied_edit_version.desc())
        .limit(max(1, limit))
        .all()
    )
    return [
        {
            "id": edit.id,
            "video_id": video.id,
            "detection_import_id": detection_import.id,
            "operation": edit.operation,
            "base_identity_revision": edit.applied_edit_version - 1,
            "result_identity_revision": edit.applied_edit_version,
            "params": edit.params,
            "affected_detections": None,
            "affected_annotations": None,
            "operator_id": edit.operator_id,
            "created_at": edit.created_at,
            "reverted_edit_id": None,
        }
        for edit in edits
    ]


@router.post("/api/projects/{project_id}/videos/{video_id}/identity-edits/{edit_id}/revert")
def revert_identity_edit(
    project_id: int,
    video_id: int,
    edit_id: int,
    body: IdentityEditRevertRequest,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> dict:
    _require_editor(access[1])
    with video_write_gate(
        db, project_id=project_id, video_id=video_id, require_active_import=True,
        expected_detection_revision=body.base_detection_import_revision,
        expected_edit_version=body.base_identity_revision,
    ) as state:
        latest = (
            db.query(DraftIdentityEdit)
            .filter(DraftIdentityEdit.detection_import_id == state.detection_import.id)
            .order_by(DraftIdentityEdit.applied_edit_version.desc()).first()
        )
        if latest is None or latest.id != edit_id:
            raise HTTPException(status_code=409, detail="Only the latest draft edit can be undone")
        if latest.operation not in ("split", "merge"):
            raise HTTPException(status_code=409, detail="Latest draft edit must use its matching endpoint")
        return undo_latest(
            db, state.detection_import, state.video, requested_edit_id=edit_id,
            expected_operation=latest.operation, expected_version=body.base_identity_revision,
        )
