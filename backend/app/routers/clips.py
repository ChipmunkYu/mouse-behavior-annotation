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
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import project_access
from ..models import Annotation, BehaviorCategory, Clip, User, Video
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
    conds = _base_filters(
        project_id,
        category_id=category_id,
        video_id=video_id,
        annotator_id=annotator_id,
        search=search,
    )
    # 稳定分页排序：start_time 主序 + id 次序
    order = (Annotation.start_time.asc(), Annotation.id.asc())

    # 轻量查询：先 COUNT + 取本页 annotation id（不加载关联列 / 大字段）
    id_query = (
        db.query(Annotation.id)
        .join(Video, Video.id == Annotation.video_id)
        .join(BehaviorCategory, BehaviorCategory.id == Annotation.category_id)
        .filter(*conds)
    )
    total = id_query.count()
    ids = [
        row[0]
        for row in id_query.order_by(*order).offset((page - 1) * page_size).limit(page_size)
    ]

    # 再取关联列：一次 join 到位（Video/类别/标注者/当前修订 Clip），无 N+1
    rows = (
        db.query(
            Annotation.id.label("annotation_id"),
            Annotation.video_id,
            Video.filename.label("video_filename"),
            Annotation.category_id,
            BehaviorCategory.name.label("category_name"),
            Annotation.start_time,
            Annotation.end_time,
            Annotation.start_frame,
            Annotation.end_frame,
            Annotation.confidence,
            Annotation.review_status,
            Annotation.created_at,
            User.username.label("annotator_name"),
            Clip.clip_path,
            Clip.thumbnail_path,
            Clip.status.label("clip_status"),
        )
        .join(Video, Video.id == Annotation.video_id)
        .join(BehaviorCategory, BehaviorCategory.id == Annotation.category_id)
        .join(User, User.id == Annotation.annotator_id)
        .outerjoin(
            Clip,
            and_(
                Clip.annotation_id == Annotation.id,
                Clip.source_revision == Video.annotation_revision,
            ),
        )
        .filter(Annotation.id.in_(ids))
        .order_by(*order)
        .all()
    )
    pages = (total + page_size - 1) // page_size if total else 0
    return ClipPageOut(total=total, pages=pages, items=[_to_item(row) for row in rows])


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
    rows = (
        db.query(
            BehaviorCategory.id.label("category_id"),
            BehaviorCategory.name.label("category_name"),
            func.count(Annotation.id).label("cnt"),
        )
        .join(Annotation, Annotation.category_id == BehaviorCategory.id)
        .join(Video, Video.id == Annotation.video_id)
        .filter(
            Annotation.review_status == APPROVED,
            Video.workflow_status == APPROVED,
            Video.project_id == project_id,
        )
        .group_by(BehaviorCategory.id, BehaviorCategory.name)
        .order_by(BehaviorCategory.sort_order, BehaviorCategory.id)
        .all()
    )
    return [
        ClipCategoryCount(category_id=r.category_id, category_name=r.category_name, count=r.cnt)
        for r in rows
    ]
