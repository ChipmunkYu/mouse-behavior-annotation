"""标注接口：增删改查 + 审核工作流联动 + 统一事件 JSON 导出。

审核工作流联动（批次 3）：
- 标注写入（创建/修改/删除）仅 owner/admin/annotator 角色可执行；reviewer 只审核不可改标注。
- 创建标注固定 `review_status=pending`；用户直接写 review_status 一律 422（创建/更新均拒绝）。
- 视频处于 submitted/approved/rejected 时，任何标注写入都先使视频回到 draft：
  `annotation_revision +1`、清空 submitted/approved 字段，并删除该视频所有 Clip 记录及
  对应实体 clip/thumbnail 文件，Review 历史保留。已处于 draft 时不再递增修订号。
- 实体文件删除策略（单机原型，见 `_cleanup_files`）：DB 事务先行提交，再删除实体文件；
  删除失败/越界路径绝不无声——写入 `data_dir/cleanup-issues.log`（JSONL）并记入应用日志，
  越界路径一律不删除。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from ..config import Settings
from ..database import get_db
from ..deps import project_access
from ..models import Annotation, BehaviorCategory, Clip, Video
from ..schemas import AnnotationCreate, AnnotationOut, AnnotationUpdate

router = APIRouter(tags=["annotations"])

VALID_CONFIDENCE = {"certain", "uncertain", "occluded"}
_OWNER_ADMIN = {"owner", "admin"}
# 标注写入角色：reviewer 只审核不可改标注
_EDITOR_ROLES = {"owner", "admin", "annotator"}
# PATCH 中属于“实际用户可编辑字段”的字段；任一出现即视为修改尝试
EDITABLE_FIELDS = (
    "category_id",
    "start_time",
    "end_time",
    "start_frame",
    "end_frame",
    "confidence",
    "crop_region",
)


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


def _require_editor(membership) -> None:
    """标注创建/修改/删除仅 owner/admin/annotator 角色；reviewer 被拒绝。"""
    if membership.role not in _EDITOR_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Only owner/admin/annotator can modify annotations",
        )


class _InvalidationPlan:
    """失效清理计划：实体文件删除 + 异常（越界路径，只记录不删除）。"""

    __slots__ = ("files", "issues")

    def __init__(self) -> None:
        self.files: list[Path] = []
        self.issues: list[dict[str, Any]] = []


def _resolve_stored_file(stored: str | None, root_dir: Path) -> tuple[Path | None, str | None]:
    """解析 Clip 实体文件路径并校验边界。

    返回 (resolved, reason)：
    - `stored` 为空 → (None, None)：无实体文件，忽略。
    - 解析后严格位于 root_dir 内且不是 root 本身 → (path, None)：允许删除。
    - 越界（含绝对路径逃逸、`../` 穿越、等于根目录）→ (None, "out-of-bounds")：
      绝不删除，仅作为异常记录（见 `_record_cleanup_issue`）。
    """
    if not stored:
        return None, None
    root = root_dir.resolve()
    raw = Path(stored)
    path = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    if path == root or not path.is_relative_to(root):
        return None, "out-of-bounds"
    return path, None


def _invalidate_video(db: Session, video: Video, settings: Settings) -> _InvalidationPlan:
    """视频非 draft 时的失效处理（与标注写入同一事务，由调用方统一 commit）。

    - 使视频回到 draft；annotation_revision +1；清空 submitted/approved 字段。
    - 删除该视频所有 Clip 记录（批次 4 才生成 Clip，本批不生成）。
    - 返回实体文件清理计划：位于 clips_dir/thumbnails_dir 内的文件待删除；
      越界路径进入 `issues` 只记录不删除。
    - Review 历史保留（不删除任何 Review 行）。
    已处于 draft 时不做任何变更（修订号不重复递增）。
    """
    plan = _InvalidationPlan()
    if video.workflow_status == "draft":
        return plan
    clips = (
        db.query(Clip)
        .join(Annotation, Annotation.id == Clip.annotation_id)
        .filter(Annotation.video_id == video.id)
        .all()
    )
    for clip in clips:
        for stored, root in (
            (clip.clip_path, settings.clips_dir),
            (clip.thumbnail_path, settings.thumbnails_dir),
        ):
            path, reason = _resolve_stored_file(stored, root)
            if path is not None:
                plan.files.append(path)
            elif reason is not None:
                plan.issues.append(
                    {
                        "kind": "out-of-bounds",
                        "clip_id": clip.id,
                        "video_id": video.id,
                        "stored": stored,
                        "root": str(root),
                        "message": "stored path resolved outside the allowed root; file NOT deleted",
                    }
                )
        db.delete(clip)
    video.workflow_status = "draft"
    video.annotation_revision += 1
    video.submitted_at = None
    video.approved_at = None
    video.approved_by = None
    return plan


def _record_cleanup_issue(log_path: Path, entry: dict[str, Any]) -> None:
    """把一条清理异常追加到 JSONL 日志（可观测；批次 4 清理任务可据此补偿删除）。

    写日志本身失败仅记录到应用日志，不阻断业务请求。
    """
    entry.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        logger.exception("Failed to write cleanup issue log: %s", log_path)


def _cleanup_files(plan: _InvalidationPlan, settings: Settings) -> None:
    """失效提交成功后清理实体文件；失败不阻断请求，但绝不无声。

    顺序策略（单机原型）：DB 事务先行提交（Clip 行删除 + 视频回 draft），随后删除实体文件。
    若先删文件后提交 DB，事务失败会留下指向已删文件的悬空 Clip 行；DB-first 则保证
    数据库永不引用已删除文件——文件删除失败残留的仅是无害孤儿文件。
    失败/越界均写入 `data_dir/cleanup-issues.log`（JSONL）并记入应用日志，避免无声孤儿，
    供批次 4 清理任务补偿删除；越界路径绝不删除。
    """
    for issue in plan.issues:
        _record_cleanup_issue(settings.cleanup_log, issue)
        logger.warning("Cleanup skipped out-of-bounds path: %s", issue)
    for path in plan.files:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            entry = {"kind": "delete-failed", "path": str(path), "error": str(exc)}
            _record_cleanup_issue(settings.cleanup_log, entry)
            logger.warning("Failed to remove orphan file %s: %s", path, exc)


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
    request: Request,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> AnnotationOut:
    _require_editor(access[1])
    video = _get_video_in_project(db, project_id, video_id)

    category = _get_category_in_project(db, project_id, body.category_id)
    if not category.is_active:
        raise HTTPException(status_code=400, detail="Category is disabled")
    if body.confidence not in VALID_CONFIDENCE:
        raise HTTPException(status_code=400, detail=f"confidence must be one of {sorted(VALID_CONFIDENCE)}")
    _validate_interval(body.start_time, body.end_time, body.start_frame, body.end_frame)

    # 非 draft 视频新增标注：先失效回 draft（修订+1、清 submitted/approved、删 Clip）
    plan = _invalidate_video(db, video, request.app.state.settings)

    annotation = Annotation(
        video_id=video_id,
        annotator_id=access[1].user_id,  # 标注者为当前登录用户（必须是项目成员，已由依赖保证）
        category_id=category.id,
        start_time=body.start_time,
        end_time=body.end_time,
        start_frame=body.start_frame,
        end_frame=body.end_frame,
        confidence=body.confidence,
        review_status="pending",  # 创建固定 pending；审核状态只能走审核 API
        crop_region=body.crop_region,
    )
    db.add(annotation)
    db.commit()
    _cleanup_files(plan, request.app.state.settings)
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
                "reviewer": ann.reviewer.username if ann.reviewer else None,
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
    request: Request,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> AnnotationOut:
    _require_editor(access[1])
    # 视频必须属于路径中的项目（防止跨项目通过其它成员关系路径修改标注）
    video = _get_video_in_project(db, project_id, video_id)
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
    if body.confidence is not None and body.confidence not in VALID_CONFIDENCE:
        raise HTTPException(status_code=400, detail=f"confidence must be one of {sorted(VALID_CONFIDENCE)}")

    # review_status 已在 Pydantic 层拒绝直接写入；实际用户可编辑字段任一出现即修改尝试，
    # 非 draft 时先失效回 draft（修订+1、清 submitted/approved、删 Clip）
    has_editable_change = any(getattr(body, field) is not None for field in EDITABLE_FIELDS)
    plan = _invalidate_video(db, video, request.app.state.settings) if has_editable_change else _InvalidationPlan()

    if body.category_id is not None:
        annotation.category_id = body.category_id
    if body.confidence is not None:
        annotation.confidence = body.confidence
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
    _cleanup_files(plan, request.app.state.settings)
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
    request: Request,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> None:
    _require_editor(access[1])
    # 视频必须属于路径中的项目（与 update 一致）
    video = _get_video_in_project(db, project_id, video_id)
    annotation = _get_annotation_in_video(db, video_id, annotation_id)
    if not _can_modify(access[1].role, annotation, access[1].user_id):
        raise HTTPException(
            status_code=403,
            detail="Only the annotator or project owner/admin can delete this annotation",
        )
    # 失效与标注删除同一事务提交；提交成功后清理实体文件（失败/越界记日志，不阻断）
    plan = _invalidate_video(db, video, request.app.state.settings)
    db.delete(annotation)
    db.commit()
    _cleanup_files(plan, request.app.state.settings)
