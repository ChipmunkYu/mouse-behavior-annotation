"""检测抑制（Phase 2）：冻结整个 corrected_track，可撤销历史抑制。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import project_access
from ..models import (
    CorrectedDetectionAssignment,
    CorrectedTrack,
    DetectionImport,
    DetectionSuppression,
    SuppressionDetection,
    Video,
)
from ..routers.identity_edits import (
    _invalidate_review_state,
    _materialize_cda_snapshot,
    _revalidate_video_annotations,
)
from ..routers.detection_imports import _get_suppressed_detection_ids
from ..schemas import SuppressionCreateRequest, SuppressionRevertRequest

router = APIRouter(tags=["detection-suppressions"])


def _require_video(db: Session, video_id: int, project_id: int) -> Video:
    video = db.get(Video, video_id)
    if video is None or video.project_id != project_id:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


def _get_active_import(db: Session, video_id: int) -> DetectionImport:
    imp = (
        db.query(DetectionImport)
        .filter(DetectionImport.video_id == video_id, DetectionImport.active == True)
        .first()
    )
    if imp is None:
        raise HTTPException(status_code=400, detail="No active detection import for this video")
    return imp


def _check_revisions(video: Video, base_di_rev: int, base_id_rev: int) -> None:
    if video.detection_import_revision != base_di_rev:
        raise HTTPException(
            status_code=409,
            detail=f"Detection import revision mismatch: expected {video.detection_import_revision}, got {base_di_rev}",
        )
    if video.identity_revision != base_id_rev:
        raise HTTPException(
            status_code=409,
            detail=f"Identity revision mismatch: expected {video.identity_revision}, got {base_id_rev}",
        )


# ---------------------------------------------------------------------------
# GET / — 活动且可撤销的抑制列表
# ---------------------------------------------------------------------------

@router.get("/api/projects/{project_id}/videos/{video_id}/detection-suppressions")
def list_active_suppressions(
    project_id: int,
    video_id: int,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> list[dict]:
    video = _require_video(db, video_id, project_id)
    imp = _get_active_import(db, video_id)
    reverted_ids = [
        row[0]
        for row in db.query(DetectionSuppression.reverted_suppression_id)
        .filter(
            DetectionSuppression.video_id == video.id,
            DetectionSuppression.detection_import_id == imp.id,
            DetectionSuppression.reverted_suppression_id != None,
        )
        .all()
    ]
    query = db.query(DetectionSuppression).filter(
        DetectionSuppression.video_id == video.id,
        DetectionSuppression.detection_import_id == imp.id,
        DetectionSuppression.reverted_suppression_id == None,
    )
    if reverted_ids:
        query = query.filter(~DetectionSuppression.id.in_(reverted_ids))
    suppressions = query.order_by(DetectionSuppression.created_at.desc()).all()
    return [
        {
            "id": suppression.id,
            "scope": suppression.scope,
            "result_identity_revision": suppression.result_identity_revision,
            "created_at": suppression.created_at,
            "frozen_detection_count": len(suppression.detections),
        }
        for suppression in suppressions
    ]


# ---------------------------------------------------------------------------
# POST / — 提交抑制
# ---------------------------------------------------------------------------

@router.post("/api/projects/{project_id}/videos/{video_id}/detection-suppressions")
def create_suppression(
    project_id: int,
    video_id: int,
    body: SuppressionCreateRequest,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> dict:
    video = _require_video(db, video_id, project_id)
    imp = _get_active_import(db, video_id)
    _check_revisions(video, body.base_detection_import_revision, body.base_identity_revision)

    old_rev = video.identity_revision
    new_rev = old_rev + 1
    suppressed_ids = _get_suppressed_detection_ids(db, imp.id, old_rev)
    detection_ids: list[int] = []
    affected_display_track_ids: set[int] = set()

    track = (
        db.query(CorrectedTrack)
        .filter(
            CorrectedTrack.detection_import_id == imp.id,
            CorrectedTrack.display_track_id == body.track_id,
            CorrectedTrack.active == True,
        )
        .first()
    )
    if track is None:
        raise HTTPException(status_code=400, detail="Track not found or not active")

    affected_display_track_ids.add(track.display_track_id)

    cda_rows = (
        db.query(CorrectedDetectionAssignment.raw_detection_id)
        .filter(
            CorrectedDetectionAssignment.corrected_track_id == track.id,
            CorrectedDetectionAssignment.identity_revision == old_rev,
        )
        .all()
    )
    detection_ids = [raw_id for (raw_id,) in cda_rows if raw_id not in suppressed_ids]
    if not detection_ids:
        raise HTTPException(status_code=409, detail="Track is already fully suppressed")

    _materialize_cda_snapshot(db, imp.id, old_rev, new_rev)

    suppression = DetectionSuppression(
        video_id=video.id,
        detection_import_id=imp.id,
        base_identity_revision=old_rev,
        result_identity_revision=new_rev,
        scope=body.scope,
        operator_id=access[1].user_id,
    )
    db.add(suppression)
    db.flush()
    db.add_all([
        SuppressionDetection(suppression_id=suppression.id, raw_detection_id=raw_id)
        for raw_id in detection_ids
    ])

    db.flush()

    needs_ids = _revalidate_video_annotations(db, imp, video, new_rev)
    video.identity_revision = new_rev

    _invalidate_review_state(db, video, needs_ids)

    db.commit()

    return {
        "suppression_id": suppression.id,
        "identity_revision": new_rev,
        "frozen_detection_count": len(detection_ids),
        "affected_track_ids": sorted(affected_display_track_ids),
        "needs_mouse_ids_annotation_ids": sorted(set(needs_ids)),
    }


# ---------------------------------------------------------------------------
# POST /{sid}/revert — 撤销抑制
# ---------------------------------------------------------------------------

@router.post("/api/projects/{project_id}/videos/{video_id}/detection-suppressions/{suppression_id}/revert")
def revert_suppression(
    project_id: int,
    video_id: int,
    suppression_id: int,
    body: SuppressionRevertRequest,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> dict:
    video = _require_video(db, video_id, project_id)
    imp = _get_active_import(db, video_id)
    _check_revisions(video, body.base_detection_import_revision, body.base_identity_revision)

    suppression = db.get(DetectionSuppression, suppression_id)
    if suppression is None or suppression.video_id != video_id:
        raise HTTPException(status_code=404, detail="Suppression not found")
    if suppression.detection_import_id != imp.id:
        raise HTTPException(
            status_code=409,
            detail="Suppression does not belong to the active detection import",
        )

    existing_revert = (
        db.query(DetectionSuppression)
        .filter(DetectionSuppression.reverted_suppression_id == suppression_id)
        .first()
    )
    if existing_revert is not None:
        raise HTTPException(status_code=409, detail="This suppression has already been reverted")

    old_rev = video.identity_revision
    new_rev = old_rev + 1

    _materialize_cda_snapshot(db, imp.id, old_rev, new_rev)

    sd_rows = (
        db.query(SuppressionDetection)
        .filter(SuppressionDetection.suppression_id == suppression.id)
        .all()
    )
    affected_track_ids: set[int] = set()
    for sd in sd_rows:
        cda = (
            db.query(CorrectedDetectionAssignment)
            .filter(
                CorrectedDetectionAssignment.raw_detection_id == sd.raw_detection_id,
                CorrectedDetectionAssignment.identity_revision == old_rev,
            )
            .first()
        )
        if cda:
            track = db.get(CorrectedTrack, cda.corrected_track_id)
            if track:
                affected_track_ids.add(track.display_track_id)
        db.delete(sd)

    revert_supp = DetectionSuppression(
        video_id=video.id,
        detection_import_id=imp.id,
        base_identity_revision=old_rev,
        result_identity_revision=new_rev,
        scope=suppression.scope,
        operator_id=access[1].user_id,
        reverted_suppression_id=suppression.id,
    )
    db.add(revert_supp)

    db.flush()
    needs_ids = _revalidate_video_annotations(db, imp, video, new_rev)
    video.identity_revision = new_rev

    _invalidate_review_state(db, video, needs_ids)

    db.commit()

    return {
        "identity_revision": new_rev,
        "freed_detection_count": len(sd_rows),
        "affected_track_ids": sorted(affected_track_ids),
        "needs_mouse_ids_annotation_ids": sorted(set(needs_ids)),
        "message": "Suppression reverted",
    }
