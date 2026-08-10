"""审核工作流接口（批次 3 + 批次 4 自动入队）：提交 / 审核队列 / 审核历史 / 审核裁决。

流程：draft → submitted → approved/rejected（rejected 可重新提交）。
- submit：仅 owner/admin/annotator；至少 1 条标注；仅 draft/rejected 可提交；
  设置 submitted/submitted_at，清空 approved 字段；本修订所有标注 review_status=pending、reviewer_id=null。
- queue：仅 owner/admin/reviewer；返回 submitted 视频。
- review：仅 owner/admin/reviewer；仅 submitted；追加 Review(annotation_revision=当前修订)。
  approved → 视频 approved/approved_at/approved_by，标注 approved/reviewer；
  提交成功后自动创建当前 video+revision 的媒体任务（dedupe 幂等）与每条标注 Clip
  pending 行并调度；若已存在 queued/running/succeeded 任务则幂等，failed 则重试。
  rejected → 视频 rejected，标注 rejected/reviewer，approved 字段清空，不入队。
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import project_access
from ..media_jobs import enqueue_media_job
from ..models import (
    Annotation,
    BehaviorCategory,
    CorrectedDetectionAssignment,
    CorrectedTrack,
    DetectionImport,
    RawDetection,
    Review,
    SuppressionDetection,
    Video,
)
from ..schemas import ReviewCreate, ReviewOut, VideoOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reviews"])

_SUBMIT_ROLES = {"owner", "admin", "annotator"}
_REVIEW_ROLES = {"owner", "admin", "reviewer"}


def _now() -> datetime:
    return datetime.utcnow()


def _get_video_in_project(db: Session, project_id: int, video_id: int) -> Video:
    video = db.get(Video, video_id)
    if video is None or video.project_id != project_id:
        raise HTTPException(status_code=404, detail="Video not found in this project")
    return video


def _to_review_out(review: Review) -> ReviewOut:
    return ReviewOut(
        id=review.id,
        project_id=review.project_id,
        video_id=review.video_id,
        reviewer_id=review.reviewer_id,
        result=review.result,
        comment=review.comment,
        annotation_revision=review.annotation_revision,
        detection_import_revision=review.detection_import_revision,
        identity_revision=review.identity_revision,
        created_at=review.created_at,
        reviewer=review.reviewer.username if review.reviewer else None,
    )


def _revalidate_annotations(db: Session, video: Video, imp: DetectionImport) -> tuple[list[dict], bool]:
    """重新校验所有标注的 mouse_ids 对抗当前 import/identity revisions 和类别规则。
    返回 (invalid_annotations, all_valid)。
    """
    annotations = (
        db.query(Annotation)
        .filter(Annotation.video_id == video.id)
        .all()
    )
    identity_rev = video.identity_revision
    invalid: list[dict] = []

    for ann in annotations:
        cat = db.get(BehaviorCategory, ann.category_id) if ann.category_id else None

        if not ann.mouse_ids:
            if ann.mouse_id_status == "valid":
                ann.mouse_id_status = "needs_mouse_ids"
                invalid.append({"annotation_id": ann.id, "reason": "mouse_ids is empty"})
            continue

        if cat is not None:
            count = len(ann.mouse_ids)
            if count < cat.mouse_count_min:
                ann.mouse_id_status = "needs_mouse_ids"
                invalid.append({
                    "annotation_id": ann.id,
                    "reason": f"Category '{cat.name}' requires at least {cat.mouse_count_min} mouse; got {count}",
                })
                continue
            if cat.mouse_count_max is not None and count > cat.mouse_count_max:
                ann.mouse_id_status = "needs_mouse_ids"
                invalid.append({
                    "annotation_id": ann.id,
                    "reason": f"Category '{cat.name}' allows at most {cat.mouse_count_max} mice; got {count}",
                })
                continue

        for tid in ann.mouse_ids:
            track = (
                db.query(CorrectedTrack)
                .filter(
                    CorrectedTrack.detection_import_id == imp.id,
                    CorrectedTrack.display_track_id == tid,
                    CorrectedTrack.active == True,
                )
                .first()
            )
            if track is None:
                ann.mouse_id_status = "needs_mouse_ids"
                invalid.append({
                    "annotation_id": ann.id,
                    "reason": f"Track ID {tid} is not an active corrected track",
                })
                break

            unsuppressed = (
                db.query(RawDetection)
                .join(
                    CorrectedDetectionAssignment,
                    CorrectedDetectionAssignment.raw_detection_id == RawDetection.id,
                )
                .join(
                    CorrectedTrack,
                    CorrectedTrack.id == CorrectedDetectionAssignment.corrected_track_id,
                )
                .outerjoin(
                    SuppressionDetection,
                    SuppressionDetection.raw_detection_id == RawDetection.id,
                )
                .filter(
                    RawDetection.detection_import_id == imp.id,
                    CorrectedTrack.active == True,
                    CorrectedTrack.display_track_id == tid,
                    RawDetection.frame_index >= ann.start_frame,
                    RawDetection.frame_index <= ann.end_frame,
                    SuppressionDetection.raw_detection_id == None,
                    CorrectedDetectionAssignment.identity_revision == identity_rev,
                )
                .count()
            )
            if unsuppressed == 0:
                ann.mouse_id_status = "needs_mouse_ids"
                invalid.append({
                    "annotation_id": ann.id,
                    "reason": f"Track ID {tid} has no unsuppressed detections in frame range [{ann.start_frame}, {ann.end_frame}]",
                })
                break
        else:
            ann.mouse_id_status = "valid"
            ann.detection_import_revision = video.detection_import_revision
            ann.identity_revision = identity_rev

    return invalid, len(invalid) == 0


@router.post(
    "/api/projects/{project_id}/videos/{video_id}/submit",
    response_model=VideoOut,
)
def submit_video(
    project_id: int,
    video_id: int,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> Video:
    membership = access[1]
    if membership.role not in _SUBMIT_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Only owner/admin/annotator can submit",
        )
    video = _get_video_in_project(db, project_id, video_id)
    if video.workflow_status in ("submitted", "approved"):
        raise HTTPException(
            status_code=400,
            detail="Video is already submitted or approved",
        )

    annotation_count = (
        db.query(Annotation).filter(Annotation.video_id == video.id).count()
    )
    if annotation_count == 0:
        raise HTTPException(
            status_code=400,
            detail="At least one annotation is required before submitting",
        )

    if video.detection_import_revision == 0:
        raise HTTPException(
            status_code=400,
            detail="Cannot submit: no detection import exists for this video. Import detection data first.",
        )

    imp = (
        db.query(DetectionImport)
        .filter(DetectionImport.video_id == video.id, DetectionImport.active == True)
        .first()
    )
    if imp is None:
        raise HTTPException(
            status_code=400,
            detail="Cannot submit: no active detection import found.",
        )

    invalid, all_valid = _revalidate_annotations(db, video, imp)
    db.flush()
    if not all_valid:
        db.commit()
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Some annotations have invalid mouse_ids and need revalidation",
                "invalid_annotations": invalid,
            },
        )

    needs_ids_count = (
        db.query(Annotation)
        .filter(
            Annotation.video_id == video.id,
            Annotation.mouse_id_status == "needs_mouse_ids",
        )
        .count()
    )
    if needs_ids_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"{needs_ids_count} annotation(s) still need valid mouse_ids before submission",
        )

    out_of_sync = (
        db.query(Annotation.id)
        .filter(
            Annotation.video_id == video.id,
            Annotation.mouse_id_status == "valid",
            (
                (Annotation.detection_import_revision != video.detection_import_revision)
                | (Annotation.identity_revision != video.identity_revision)
            ),
        )
        .all()
    )
    if out_of_sync:
        ids = [r[0] for r in out_of_sync]
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Some annotations have stale detection/identity revisions",
                "out_of_sync_annotation_ids": ids,
            },
        )

    video.workflow_status = "submitted"
    video.submitted_at = _now()
    video.approved_at = None
    video.approved_by = None
    for ann in db.query(Annotation).filter(Annotation.video_id == video.id).all():
        ann.review_status = "pending"
        ann.reviewer_id = None
    db.commit()
    db.refresh(video)
    return video


@router.get(
    "/api/projects/{project_id}/reviews/queue",
    response_model=list[VideoOut],
)
def review_queue(
    project_id: int,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> list[Video]:
    membership = access[1]
    if membership.role not in _REVIEW_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Only owner/admin/reviewer can view the review queue",
        )
    return (
        db.query(Video)
        .filter(Video.project_id == project_id, Video.workflow_status == "submitted")
        .order_by(Video.submitted_at.desc(), Video.id.desc())
        .all()
    )


@router.get(
    "/api/projects/{project_id}/videos/{video_id}/reviews",
    response_model=list[ReviewOut],
)
def review_history(
    project_id: int,
    video_id: int,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> list[ReviewOut]:
    video = _get_video_in_project(db, project_id, video_id)
    rows = (
        db.query(Review)
        .filter(Review.video_id == video.id)
        .order_by(Review.created_at.asc(), Review.id.asc())
        .all()
    )
    return [_to_review_out(r) for r in rows]


@router.post(
    "/api/projects/{project_id}/videos/{video_id}/review",
    response_model=ReviewOut,
)
def create_review(
    project_id: int,
    video_id: int,
    body: ReviewCreate,
    request: Request,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> ReviewOut:
    membership = access[1]
    if membership.role not in _REVIEW_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Only owner/admin/reviewer can review",
        )
    video = _get_video_in_project(db, project_id, video_id)
    if video.workflow_status != "submitted":
        raise HTTPException(
            status_code=400,
            detail="Only submitted videos can be reviewed",
        )

    review = Review(
        project_id=project_id,
        video_id=video.id,
        reviewer_id=membership.user_id,
        result=body.result,
        comment=body.comment,
        annotation_revision=video.annotation_revision,
        detection_import_revision=video.detection_import_revision,
        identity_revision=video.identity_revision,
    )
    now = _now()
    if body.result == "approved":
        video = db.get(Video, video_id)
        if video is None or video.workflow_status != "submitted":
            raise HTTPException(
                status_code=409,
                detail="Video workflow state changed since review began; cannot approve",
            )
        imp = (
            db.query(DetectionImport)
            .filter(DetectionImport.video_id == video.id, DetectionImport.active == True)
            .first()
        )
        if imp is None:
            raise HTTPException(
                status_code=409, detail="No active detection import; cannot approve"
            )
        invalid, all_valid = _revalidate_annotations(db, video, imp)
        if not all_valid:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Annotations failed re-validation since submit; revisions may have changed",
                    "invalid_annotations": invalid,
                },
            )
        video.workflow_status = "approved"
        video.approved_at = now
        video.approved_by = membership.user_id
    else:
        video.workflow_status = "rejected"
        video.approved_at = None
        video.approved_by = None
    for ann in db.query(Annotation).filter(Annotation.video_id == video.id).all():
        ann.review_status = body.result
        ann.reviewer_id = membership.user_id
        if body.result == "approved":
            ann.detection_import_revision = video.detection_import_revision
            ann.identity_revision = video.identity_revision
    db.add(review)
    db.commit()
    db.refresh(review)

    job = None
    if body.result == "approved":
        try:
            job = enqueue_media_job(db, video)
        except Exception:
            logger.exception("Auto-enqueue media job failed for video %s", video.id)
            job = None
    if job is not None:
        try:
            request.app.state.media_worker.schedule(job.id)
        except Exception:
            logger.exception("Schedule media job %s failed", job.id)
    return _to_review_out(review)
