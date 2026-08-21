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
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, literal, or_, select, union_all
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


def _to_item(row) -> ClipItem:
    ready = row.clip_status == "ready"
    return ClipItem(
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
        clip_path=row.clip_path if ready else None,
        thumbnail_path=row.thumbnail_path if ready else None,
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
            SubmissionAnnotation.id.label("annotation_id"), Submission.video_id,
            Submission.source_video_filename.label("video_filename"),
            SubmissionAnnotation.category_id, SubmissionAnnotation.category_name,
            SubmissionAnnotation.start_time, SubmissionAnnotation.end_time,
            SubmissionAnnotation.start_frame, SubmissionAnnotation.end_frame,
            SubmissionAnnotation.confidence,
            literal("approved").label("review_status"),
            Submission.submitted_at.label("created_at"),
            User.username.label("annotator_name"),
            Clip.clip_path,
            Clip.thumbnail_path,
            Clip.status.label("clip_status"),
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
    legacy_query = select(
        Annotation.id.label("annotation_id"), Annotation.video_id, Video.filename.label("video_filename"),
        Annotation.category_id, BehaviorCategory.name.label("category_name"), Annotation.start_time,
        Annotation.end_time, Annotation.start_frame, Annotation.end_frame, Annotation.confidence,
        Annotation.review_status, Annotation.created_at, User.username.label("annotator_name"),
        Clip.clip_path, Clip.thumbnail_path, Clip.status.label("clip_status"),
        BehaviorCategory.group.label("category_group"),
        BehaviorCategory.participant_mode.label("category_participant_mode"),
        BehaviorCategory.role_definitions.label("role_definitions"),
        Annotation.participant_roles.label("participant_roles"), Annotation.mouse_ids,
    ).select_from(Annotation).join(Video).join(BehaviorCategory).outerjoin(User, User.id == Annotation.annotator_id).outerjoin(
        Clip, and_(Clip.annotation_id == Annotation.id, Clip.source_revision == Video.media_revision)
    ).where(*legacy_conds)
    mixed = union_all(new_query, legacy_query).subquery("mixed_clip_authority")
    total = db.execute(select(func.count()).select_from(mixed)).scalar_one()
    rows = db.execute(select(mixed).order_by(mixed.c.start_time, mixed.c.annotation_id)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    items = [_to_item(row) for row in rows]
    pages = (total + page_size - 1) // page_size if total else 0
    return ClipPageOut(total=total, pages=pages, items=items)


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
