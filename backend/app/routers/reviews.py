"""审核工作流接口（批次 3）：提交 / 审核队列 / 审核历史 / 审核裁决。

流程：draft → submitted → approved/rejected（rejected 可重新提交）。
- submit：仅 owner/admin/annotator；至少 1 条标注；仅 draft/rejected 可提交；
  设置 submitted/submitted_at，清空 approved 字段；本修订所有标注 review_status=pending、reviewer_id=null。
- queue：仅 owner/admin/reviewer；返回 submitted 视频。
- review：仅 owner/admin/reviewer；仅 submitted；追加 Review(annotation_revision=当前修订)。
  approved → 视频 approved/approved_at/approved_by，标注 approved/reviewer；
  rejected → 视频 rejected，标注 rejected/reviewer，approved 字段清空。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import project_access
from ..models import Annotation, Review, Video
from ..schemas import ReviewCreate, ReviewOut, VideoOut

router = APIRouter(tags=["reviews"])

# 提交角色：annotator 可提交不可审核
_SUBMIT_ROLES = {"owner", "admin", "annotator"}
# 审核角色：reviewer 可审核不可改标注
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
        created_at=review.created_at,
        reviewer=review.reviewer.username if review.reviewer else None,
    )


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
    """项目成员可读该视频的完整审核历史（含所有修订）。"""
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
        annotation_revision=video.annotation_revision,  # 记录裁决时点的修订号
    )
    now = _now()
    if body.result == "approved":
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
    db.add(review)
    db.commit()
    db.refresh(review)
    return _to_review_out(review)
