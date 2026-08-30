"""生产跨视频片段库（批次 5）：跨视频聚合审核通过标注与对应 ready 的 Clip。

契约（对应 backend/README.md「批次 5：生产跨视频片段库」）：
- 只返回「标注 `review_status=approved` 且所属视频当前 `workflow_status=approved`」的
  标注；失效回 draft 后仍残留的 approved 标注一并排除，杜绝库内出现已失效片段。
- 每项携带当前修订（`Clip.source_revision == video.annotation_revision`）对应 Clip 的
  相对路径；Clip 缺失或非 ready（pending/processing/failed）时 `clip_path` /
  `thumbnail_path` 为 null。
- 排序 `start_time` + `id`（稳定分页）；分页默认 20、上限 100（page<1 或 page_size
  超界 → 422）。
- 筛选：`category_id` / `video_id` / `annotator_id` / `search`（类别名或视频文件名，
  大小写不敏感）；`review_status` 仅接受 `approved`（其余 422）。
- COUNT 与分页先用轻量 id 查询，随后才取关联列（无 N+1，也不加载 crop_region 等重列）。
- 不批量加载视频流：本接口只返回元数据与相对路径，客户端自行请求单条 blob。
"""
from __future__ import annotations

from math import ceil
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import and_, case, func, literal, or_, select, union_all
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import project_access
from ..models import Annotation, BehaviorCategory, Clip, Submission, SubmissionAnnotation, User, Video
from ..schemas import ClipCategoryCount, ClipItem, ClipPageOut

router = APIRouter(tags=["clips"])

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
APPROVED = "approved"


def _base_filters(
    project_id: int,
    *,
    category_id: Optional[int],
    video_id: Optional[int],
    annotator_id: Optional[int],
    search: Optional[str],
) -> list:
    """片段库基础过滤：审核通过标注 + 视频当前 approved + 项目隔离 + 可选筛选。"""
    conds = [
        Annotation.review_status == APPROVED,
        Video.workflow_status == APPROVED,
        Video.project_id == project_id,
    ]
    if category_id is not None:
        conds.append(Annotation.category_id == category_id)
    if video_id is not None:
        conds.append(Annotation.video_id == video_id)
    if annotator_id is not None:
        conds.append(Annotation.annotator_id == annotator_id)
    if search:
        conds.append(
            or_(
                BehaviorCategory.name.ilike(f"%{search}%"),
                Video.filename.ilike(f"%{search}%"),
            )
        )
    return conds


def _to_item(row, settings) -> ClipItem:
    media_status = row.media_status
    if media_status == "ready" and (
        _resolve_entity(row.clip_path, settings.clips_dir) is None
        or _resolve_entity(row.thumbnail_path, settings.thumbnails_dir) is None
    ):
        media_status = "pending"
    return ClipItem(
        item_key=f"{row.authority_type}:{row.annotation_id}",
        clip_id=row.clip_id,
        annotation_id=row.annotation_id,
        video_id=row.video_id,
        video_filename=row.video_filename,
        category_id=row.category_id,
        category_name=row.category_name,
        start_time=row.start_time,
        end_time=row.end_time,
        start_frame=row.start_frame,
        end_frame=row.end_frame,
        confidence=row.confidence,
        media_status=media_status,
        annotator_name=row.annotator_name,
        review_status=row.review_status,
        created_at=row.created_at,
        category_group=row.category_group,
        category_participant_mode=row.category_participant_mode,
        role_definitions=row.role_definitions or [],
        participant_roles=row.participant_roles or {},
        mouse_ids=row.mouse_ids or [],
    )


@router.get("/api/projects/{project_id}/clips", response_model=ClipPageOut)
def list_clips(
    project_id: int,
    request: Request,
    category_id: Optional[int] = None,
    video_id: Optional[int] = None,
    annotator_id: Optional[int] = None,
    review_status: Literal["approved"] = Query(APPROVED),
    search: Optional[str] = Query(default=None, max_length=128),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> ClipPageOut:
    """项目成员分页浏览跨视频审核通过片段库（仅成员；不校验 active）。"""
    authority_video_ids = select(Submission.video_id).distinct()
    conds = [Submission.status == APPROVED, Video.project_id == project_id]
    if category_id is not None: conds.append(SubmissionAnnotation.category_id == category_id)
    if video_id is not None: conds.append(Submission.video_id == video_id)
    if annotator_id is not None: conds.append(Submission.submitted_by == annotator_id)
    if search: conds.append(or_(SubmissionAnnotation.category_name.ilike(f"%{search}%"), Submission.source_video_filename.ilike(f"%{search}%")))
    new_query = (
        select(
            literal("submission").label("authority_type"),
            SubmissionAnnotation.id.label("annotation_id"), Submission.video_id,
            Submission.source_video_filename.label("video_filename"),
            SubmissionAnnotation.category_id, SubmissionAnnotation.category_name,
            SubmissionAnnotation.start_time, SubmissionAnnotation.end_time,
            SubmissionAnnotation.start_frame, SubmissionAnnotation.end_frame,
            SubmissionAnnotation.confidence,
            literal("approved").label("review_status"),
            Submission.submitted_at.label("created_at"),
            User.username.label("annotator_name"),
            Clip.id.label("clip_id"),
            case((Clip.id.is_(None), "pending"), else_=Clip.status).label("media_status"),
            Clip.clip_path, Clip.thumbnail_path,
            SubmissionAnnotation.category_group,
            SubmissionAnnotation.category_participant_mode,
            SubmissionAnnotation.role_definitions_snapshot.label("role_definitions"),
            SubmissionAnnotation.participant_roles_snapshot.label("participant_roles"),
            SubmissionAnnotation.mouse_ids,
        )
        .join(Submission, Submission.id == SubmissionAnnotation.submission_id)
        .join(Video, Video.id == Submission.video_id)
        .outerjoin(User, User.id == Submission.submitted_by)
        .outerjoin(Clip, Clip.submission_annotation_id == SubmissionAnnotation.id)
        .where(*conds)
    )
    legacy_conds = _base_filters(project_id, category_id=category_id, video_id=video_id,
                                 annotator_id=annotator_id, search=search)
    legacy_conds.append(~Annotation.video_id.in_(authority_video_ids))
    latest_legacy_clip_id = select(func.max(Clip.id)).where(
        Clip.annotation_id == Annotation.id
    ).correlate(Annotation).scalar_subquery()
    legacy_query = select(
        literal("legacy").label("authority_type"),
        Annotation.id.label("annotation_id"), Annotation.video_id, Video.filename.label("video_filename"),
        Annotation.category_id, BehaviorCategory.name.label("category_name"), Annotation.start_time,
        Annotation.end_time, Annotation.start_frame, Annotation.end_frame, Annotation.confidence,
        Annotation.review_status, Annotation.created_at, User.username.label("annotator_name"),
        Clip.id.label("clip_id"),
        case(
            (Clip.id.is_(None), "pending"),
            (Clip.source_revision == Video.media_revision, Clip.status),
            else_="stale",
        ).label("media_status"),
        Clip.clip_path, Clip.thumbnail_path,
        BehaviorCategory.group.label("category_group"),
        BehaviorCategory.participant_mode.label("category_participant_mode"),
        BehaviorCategory.role_definitions.label("role_definitions"),
        Annotation.participant_roles.label("participant_roles"), Annotation.mouse_ids,
    ).select_from(Annotation).join(Video).join(BehaviorCategory).outerjoin(User, User.id == Annotation.annotator_id).outerjoin(
        Clip, Clip.id == latest_legacy_clip_id
    ).where(*legacy_conds)
    mixed = union_all(new_query, legacy_query).subquery("mixed_clip_authority")
    total = db.execute(select(func.count()).select_from(mixed)).scalar_one()
    rows = db.execute(select(mixed).order_by(mixed.c.start_time, mixed.c.annotation_id)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    items = [_to_item(row, request.app.state.settings) for row in rows]
    pages = (total + page_size - 1) // page_size if total else 0
    return ClipPageOut(total=total, pages=pages, items=items)


def _resolve_entity(raw: str | None, root: Path) -> Path | None:
    if not raw:
        return None
    try:
        base = root.resolve()
        candidate = Path(raw)
        candidate = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
        valid = candidate.is_relative_to(base) and candidate.is_file()
    except (OSError, RuntimeError, ValueError):
        return None
    if not valid:
        return None
    return candidate


_resolve_thumbnail = _resolve_entity


@router.get("/api/projects/{project_id}/clips/{clip_id}/thumbnail")
def get_clip_thumbnail(
    project_id: int,
    clip_id: int,
    request: Request,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Serve a ready thumbnail by Clip identity without exposing its disk name."""
    clip = db.get(Clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    if clip.status != "ready":
        raise HTTPException(status_code=404, detail="Thumbnail not ready")
    if clip.annotation_id is not None:
        current = db.query(Clip.id).join(Annotation, Annotation.id == Clip.annotation_id).join(
            Video, Video.id == Annotation.video_id
        ).filter(
            Clip.id == clip.id,
            Video.project_id == project_id,
            Clip.source_revision == Video.media_revision,
            Annotation.review_status == APPROVED,
            Video.workflow_status == APPROVED,
        ).first()
    else:
        current = db.query(Clip.id).join(
            SubmissionAnnotation, SubmissionAnnotation.id == Clip.submission_annotation_id
        ).join(Submission, Submission.id == SubmissionAnnotation.submission_id).join(
            Video, Video.id == Submission.video_id
        ).filter(
            Clip.id == clip.id, Submission.status == APPROVED,
            Video.project_id == project_id,
        ).first()
    if current is None:
        raise HTTPException(status_code=404, detail="Thumbnail not ready")
    settings = request.app.state.settings
    path = _resolve_thumbnail(clip.thumbnail_path, settings.thumbnails_dir)
    if path is None or _resolve_entity(clip.clip_path, settings.clips_dir) is None:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(
        path,
        media_type="image/jpeg",
        filename="thumbnail.jpg",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get(
    "/api/projects/{project_id}/clips/categories",
    response_model=list[ClipCategoryCount],
)
def clip_categories(
    project_id: int,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> list[ClipCategoryCount]:
    """审核通过片段的类别统计（分类筛选 chip）：仅含计数 > 0 的类别，按 sort_order 排序。"""
    new_rows = (
        db.query(
            SubmissionAnnotation.category_id, SubmissionAnnotation.category_name,
            func.count(SubmissionAnnotation.id).label("cnt"),
        )
        .join(Submission, Submission.id == SubmissionAnnotation.submission_id)
        .join(Video, Video.id == Submission.video_id)
        .filter(Submission.status == APPROVED, Video.project_id == project_id)
        .group_by(SubmissionAnnotation.category_id, SubmissionAnnotation.category_name)
        .order_by(SubmissionAnnotation.category_id)
        .all()
    )
    authority_video_ids = db.query(Submission.video_id).distinct()
    legacy_rows = db.query(
        BehaviorCategory.id.label("category_id"), BehaviorCategory.name.label("category_name"),
        func.count(Annotation.id).label("cnt"),
    ).join(Annotation).join(Video).filter(
        Annotation.review_status == APPROVED, Video.workflow_status == APPROVED,
        Video.project_id == project_id, ~Video.id.in_(authority_video_ids),
    ).group_by(BehaviorCategory.id, BehaviorCategory.name).all()
    counts: dict[tuple[int, str], int] = {}
    for row in [*new_rows, *legacy_rows]:
        key = (row.category_id, row.category_name)
        counts[key] = counts.get(key, 0) + row.cnt
    return [ClipCategoryCount(category_id=key[0], category_name=key[1], count=count)
            for key, count in sorted(counts.items())]
