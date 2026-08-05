"""标注接口：增删改查 + 统一事件 JSON 导出。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import project_access
from ..models import Annotation, BehaviorCategory, Video
from ..schemas import AnnotationCreate, AnnotationOut, AnnotationUpdate

router = APIRouter(tags=["annotations"])

VALID_CONFIDENCE = {"certain", "uncertain", "occluded"}
VALID_REVIEW_STATUS = {"pending", "approved", "rejected"}
_OWNER_ADMIN = {"owner", "admin"}


def _validate_interval(
    start_time: float | None,
    end_time: float | None,
    start_frame: int | None,
    end_frame: int | None,
) -> None:
    if start_time is not None and start_time < 0:
        raise HTTPException(status_code=400, detail="start_time must be >= 0")
    if start_frame is not None and start_frame < 0:
        raise HTTPException(status_code=400, detail="start_frame must be >= 0")
    if end_time is not None and start_time is not None and end_time <= start_time:
        raise HTTPException(status_code=400, detail="end_time must be greater than start_time")
    if end_frame is not None and start_frame is not None and end_frame <= start_frame:
        raise HTTPException(status_code=400, detail="end_frame must be greater than start_frame")


def _get_video_in_project(db: Session, project_id: int, video_id: int) -> Video:
    video = db.get(Video, video_id)
    if video is None or video.project_id != project_id:
        raise HTTPException(status_code=404, detail="Video not found in this project")
    return video


def _get_category_in_project(db: Session, project_id: int, category_id: int) -> BehaviorCategory:
    category = db.get(BehaviorCategory, category_id)
    if category is None or category.project_id != project_id:
        raise HTTPException(status_code=400, detail="Category does not belong to this project")
    return category


def _get_annotation_in_video(db: Session, video_id: int, annotation_id: int) -> Annotation:
    annotation = db.get(Annotation, annotation_id)
    if annotation is None or annotation.video_id != video_id:
        raise HTTPException(status_code=404, detail="Annotation not found in this video")
    return annotation


def _to_out(annotation: Annotation) -> AnnotationOut:
    return AnnotationOut(
        id=annotation.id,
        video_id=annotation.video_id,
        annotator_id=annotation.annotator_id,
        category_id=annotation.category_id,
        start_time=annotation.start_time,
        end_time=annotation.end_time,
        start_frame=annotation.start_frame,
        end_frame=annotation.end_frame,
        confidence=annotation.confidence,
        review_status=annotation.review_status,
        crop_region=annotation.crop_region,
        created_at=annotation.created_at,
        updated_at=annotation.updated_at,
        annotator=annotation.annotator.username if annotation.annotator else None,
        category_name=annotation.category.name if annotation.category else None,
    )


def _can_modify(membership_role: str, annotation: Annotation, user_id: int) -> bool:
    return annotation.annotator_id == user_id or membership_role in _OWNER_ADMIN


@router.get(
    "/api/projects/{project_id}/videos/{video_id}/annotations",
    response_model=list[AnnotationOut],
)
def list_annotations(
    project_id: int,
    video_id: int,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> list[AnnotationOut]:
    _get_video_in_project(db, project_id, video_id)
    rows = (
        db.query(Annotation)
        .filter(Annotation.video_id == video_id)
        .order_by(Annotation.start_time)
        .all()
    )
    return [_to_out(r) for r in rows]


@router.post(
    "/api/projects/{project_id}/videos/{video_id}/annotations",
    response_model=AnnotationOut,
    status_code=201,
)
def create_annotation(
    project_id: int,
    video_id: int,
    body: AnnotationCreate,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> AnnotationOut:
    _get_video_in_project(db, project_id, video_id)

    category = _get_category_in_project(db, project_id, body.category_id)
    if not category.is_active:
        raise HTTPException(status_code=400, detail="Category is disabled")
    if body.confidence not in VALID_CONFIDENCE:
        raise HTTPException(status_code=400, detail=f"confidence must be one of {sorted(VALID_CONFIDENCE)}")
    if body.review_status not in VALID_REVIEW_STATUS:
        raise HTTPException(
            status_code=400, detail=f"review_status must be one of {sorted(VALID_REVIEW_STATUS)}"
        )
    _validate_interval(body.start_time, body.end_time, body.start_frame, body.end_frame)

    annotation = Annotation(
        video_id=video_id,
        annotator_id=access[1].user_id,  # 标注者为当前登录用户（必须是项目成员，已由依赖保证）
        category_id=category.id,
        start_time=body.start_time,
        end_time=body.end_time,
        start_frame=body.start_frame,
        end_frame=body.end_frame,
        confidence=body.confidence,
        review_status=body.review_status,
        crop_region=body.crop_region,
    )
    db.add(annotation)
    db.commit()
    db.refresh(annotation)
    return _to_out(annotation)


@router.get("/api/projects/{project_id}/videos/{video_id}/annotations/export")
def export_annotations(
    project_id: int,
    video_id: int,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> list[dict]:
    """导出符合需求文档的统一事件格式 JSON 列表。"""
    video = _get_video_in_project(db, project_id, video_id)
    rows = (
        db.query(Annotation)
        .filter(Annotation.video_id == video_id)
        .order_by(Annotation.start_time)
        .all()
    )
    events: list[dict] = []
    for ann in rows:
        events.append(
            {
                "video_id": f"video_{video.id}",
                "start_time": ann.start_time,
                "end_time": ann.end_time,
                "start_frame": ann.start_frame,
                "end_frame": ann.end_frame,
                "behavior": ann.category.name if ann.category else None,
                "crop_region": ann.crop_region,
                "confidence": ann.confidence,
                "annotator": ann.annotator.username if ann.annotator else None,
                "reviewer": None,  # P1 审核流程未实现
                "review_status": ann.review_status,
            }
        )
    return events


@router.patch(
    "/api/projects/{project_id}/videos/{video_id}/annotations/{annotation_id}",
    response_model=AnnotationOut,
)
def update_annotation(
    project_id: int,
    video_id: int,
    annotation_id: int,
    body: AnnotationUpdate,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> AnnotationOut:
    # 视频必须属于路径中的项目（防止跨项目通过其它成员关系路径修改标注）
    _get_video_in_project(db, project_id, video_id)
    annotation = _get_annotation_in_video(db, video_id, annotation_id)
    if not _can_modify(access[1].role, annotation, access[1].user_id):
        raise HTTPException(
            status_code=403,
            detail="Only the annotator or project owner/admin can modify this annotation",
        )

    if body.category_id is not None:
        category = _get_category_in_project(db, project_id, body.category_id)
        if not category.is_active:
            raise HTTPException(status_code=400, detail="Category is disabled")
        annotation.category_id = category.id
    if body.confidence is not None:
        if body.confidence not in VALID_CONFIDENCE:
            raise HTTPException(status_code=400, detail=f"confidence must be one of {sorted(VALID_CONFIDENCE)}")
        annotation.confidence = body.confidence
    if body.review_status is not None:
        if body.review_status not in VALID_REVIEW_STATUS:
            raise HTTPException(
                status_code=400, detail=f"review_status must be one of {sorted(VALID_REVIEW_STATUS)}"
            )
        annotation.review_status = body.review_status
    if body.crop_region is not None:
        annotation.crop_region = body.crop_region

    for field in ("start_time", "end_time", "start_frame", "end_frame"):
        value = getattr(body, field)
        if value is not None:
            setattr(annotation, field, value)

    _validate_interval(
        annotation.start_time,
        annotation.end_time,
        annotation.start_frame,
        annotation.end_frame,
    )

    db.commit()
    db.refresh(annotation)
    return _to_out(annotation)


@router.delete(
    "/api/projects/{project_id}/videos/{video_id}/annotations/{annotation_id}",
    status_code=204,
)
def delete_annotation(
    project_id: int,
    video_id: int,
    annotation_id: int,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> None:
    # 视频必须属于路径中的项目（与 update 一致）
    _get_video_in_project(db, project_id, video_id)
    annotation = _get_annotation_in_video(db, video_id, annotation_id)
    if not _can_modify(access[1].role, annotation, access[1].user_id):
        raise HTTPException(
            status_code=403,
            detail="Only the annotator or project owner/admin can delete this annotation",
        )
    db.delete(annotation)
    db.commit()
