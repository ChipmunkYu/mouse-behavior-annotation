"""标注接口：增删改查 + 审核工作流联动 + 统一事件 JSON 导出 + 参与小鼠 ID（v0.6）。

审核工作流联动（批次 3）：
- 标注写入（创建/修改/删除）仅 owner/admin/annotator 角色可执行；reviewer 只审核不可改标注。
- 创建标注固定 `review_status=pending`；用户直接写 review_status 一律 422（创建/更新均拒绝）。
- 每次实际标注内容写入都使 `annotation_revision +1`；媒体区间/crop 实际变化时另使
  `media_revision +1`。非 draft 视频同时回到 draft 并执行审核/Clip 失效清理；draft
  视频不重复清理，但修订号仍推进。
- 实体文件删除策略（单机原型，见 `_cleanup_files`）：DB 事务先行提交，再删除实体文件；
  删除失败/越界路径绝不无声——写入 `data_dir/cleanup-issues.log`（JSONL）并记入应用日志，
  越界路径一律不删除。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from ..config import Settings
from ..cleanup_io import append_cleanup_issues, remove_checked, safe_path
from ..database import get_db
from ..deps import project_access
from ..effective_detections import has_effective_detection
from ..models import (
    Annotation,
    BehaviorCategory,
    Clip,
    DetectionImport,
    Video,
)
from ..permissions import MANAGER_ROLES, require_editor as require_edit_permission
from ..schemas import AnnotationCreate, AnnotationOut, AnnotationUpdate
from ..video_write_gate import video_write_gate

router = APIRouter(tags=["annotations"])

VALID_CONFIDENCE = {"certain", "uncertain", "occluded"}
_OWNER_ADMIN = MANAGER_ROLES
# PATCH 中属于"实际用户可编辑字段"的字段；任一出现即视为修改尝试
EDITABLE_FIELDS = (
    "category_id",
    "start_time",
    "end_time",
    "start_frame",
    "end_frame",
    "confidence",
    "crop_region",
    "mouse_ids",
    "detection_import_revision",
    "identity_revision",
)


def _legacy_event_record(annotation: Annotation, clip_file: str | None = None) -> dict:
    """Serialize the legacy single-video export contract.

    This compatibility representation intentionally stays at the legacy route boundary; the
    Submission-authority project ZIP has its own independent four-file contract.
    """
    return {
        "annotation_id": annotation.id,
        "video_id": f"video_{annotation.video_id}",
        "clip_file": clip_file,
        "start_time": annotation.start_time,
        "end_time": annotation.end_time,
        "start_frame": annotation.start_frame,
        "end_frame": annotation.end_frame,
        "behavior": annotation.category.name if annotation.category else None,
        "mouse_ids": annotation.mouse_ids or [],
        "detection_import_revision": annotation.detection_import_revision,
        "identity_revision": annotation.identity_revision,
        "crop_region": annotation.crop_region,
        "confidence": annotation.confidence,
        "annotator": annotation.annotator.username if annotation.annotator else None,
        "reviewer": annotation.reviewer.username if annotation.reviewer else None,
        "review_status": annotation.review_status,
    }


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


def _get_active_import(db: Session, video_id: int) -> DetectionImport | None:
    return (
        db.query(DetectionImport)
        .filter(DetectionImport.video_id == video_id, DetectionImport.active == True)
        .first()
    )


def _has_unsuppressed_detections(
    db: Session,
    imp_id: int,
    display_track_id: int,
    start_frame: int,
    end_frame: int,
    identity_revision: int,
) -> bool:
    return has_effective_detection(
        db, imp_id, display_track_id, start_frame, end_frame
    )


def _validate_mouse_ids(
    db: Session,
    imp: DetectionImport,
    mouse_ids: list[int],
    start_frame: int,
    end_frame: int,
    category: BehaviorCategory,
    identity_revision: int,
) -> None:
    normalized = sorted(set(mouse_ids))
    count = len(normalized)
    if count < category.mouse_count_min:
        raise HTTPException(
            status_code=400,
            detail=f"Category '{category.name}' requires at least {category.mouse_count_min} mouse; got {count}",
        )
    if category.mouse_count_max is not None and count > category.mouse_count_max:
        raise HTTPException(
            status_code=400,
            detail=f"Category '{category.name}' allows at most {category.mouse_count_max} mice; got {count}",
        )

    for tid in normalized:
        if not _has_unsuppressed_detections(db, imp.id, tid, start_frame, end_frame, identity_revision):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Track ID {tid} is not an active corrected track or has no unsuppressed "
                    f"detections in frame range [{start_frame}, {end_frame}]"
                ),
            )


def _get_annotation_in_video(db: Session, video_id: int, annotation_id: int) -> Annotation:
    annotation = db.get(Annotation, annotation_id)
    if annotation is None or annotation.video_id != video_id:
        raise HTTPException(status_code=404, detail="Annotation not found in this video")
    return annotation


def _require_editor(membership) -> None:
    require_edit_permission(membership)


class _InvalidationPlan:
    """失效清理计划：实体文件删除 + 异常（越界路径，只记录不删除）。"""

    __slots__ = ("files", "issues")

    def __init__(self) -> None:
        self.files: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []


def _resolve_stored_file(
    stored: str | None, root_dir: Path, data_dir: Path
) -> tuple[Path | None, str | None]:
    """解析 Clip 实体文件路径并校验边界。

    返回 (resolved, reason)：
    - `stored` 为空 → (None, None)：无实体文件，忽略。
    - 解析后严格位于 root_dir 内且不是 root 本身 → (path, None)：允许删除。
    - 越界（含绝对路径逃逸、`../` 穿越、等于根目录）→ (None, "out-of-bounds")：
      绝不删除，仅作为异常记录（见 `_record_cleanup_issue`）。
    """
    if not stored:
        return None, None
    return safe_path(stored, root_dir, data_dir)


def _invalidate_video(db: Session, video: Video, settings: Settings, *, increment_media: bool = True) -> _InvalidationPlan:
    """标注内容变更的修订推进与非 draft 工作流失效处理。

    - 使视频回到 draft；annotation_revision +1；清空 submitted/approved 字段。
    - 若 `increment_media=True`（时间/帧/crop_region 变化）：递增 media_revision，
      删除该视频所有 Clip 记录（批次 4 才生成 Clip，本批不生成）。
    - 若 `increment_media=False`（类别/mouse_ids/检测导入/身份变化）：
      仅递增 annotation_revision，不删除现有 Clip（它们仍然有效）。
    - 返回实体文件清理计划：位于 clips_dir/thumbnails_dir 内的文件待删除；
      越界路径进入 `issues` 只记录不删除。
    - Review 历史保留（不删除任何 Review 行）。
    已处于 draft 时不重复执行工作流/Clip 清理，但仍推进相应修订号。
    """
    plan = _InvalidationPlan()
    was_draft = video.workflow_status == "draft"
    video.annotation_revision += 1
    if increment_media:
        video.media_revision += 1
    if was_draft:
        return plan
    if increment_media:
        clips = (
            db.query(Clip)
            .join(Annotation, Annotation.id == Clip.annotation_id)
            .filter(Annotation.video_id == video.id)
            .all()
        )
        for clip in clips:
            for stored, root, media_kind in (
                (clip.clip_path, settings.clips_dir, "clip"),
                (clip.thumbnail_path, settings.thumbnails_dir, "thumbnail"),
            ):
                path, reason = _resolve_stored_file(stored, root, settings.data_dir)
                if path is not None:
                    plan.files.append(
                        {
                            "path": path,
                            "annotation_id": clip.annotation_id,
                            "revision": clip.source_revision,
                            "media_kind": media_kind,
                            "root": root.name,
                        }
                    )
                elif reason is not None:
                    plan.issues.append(
                        {
                            "kind": "out-of-bounds",
                            "path": stored,
                            "clip_id": clip.id,
                            "annotation_id": clip.annotation_id,
                            "revision": clip.source_revision,
                            "media_kind": media_kind,
                            "video_id": video.id,
                            "stored": stored,
                            "root": root.name,
                            "message": "stored path resolved outside the allowed root; file NOT deleted",
                        }
                    )
            db.delete(clip)
    video.workflow_status = "draft"
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
        append_cleanup_issues(log_path, [entry])
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
    for item in plan.files:
        path = item["path"]
        root = settings.clips_dir if item["media_kind"] == "clip" else settings.thumbnails_dir
        deleted, reason = remove_checked(
            path, root_dir=root, data_dir=settings.data_dir
        )
        if reason is not None:
            entry = {
                "kind": "delete-failed",
                "path": str(path),
                "error": reason,
                **{key: value for key, value in item.items() if key != "path"},
            }
            _record_cleanup_issue(settings.cleanup_log, entry)
            logger.warning("Failed to remove orphan file %s: %s", path, reason)


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
        mouse_ids=annotation.mouse_ids or [],
        mouse_id_status=annotation.mouse_id_status,
        detection_import_revision=annotation.detection_import_revision,
        identity_revision=annotation.identity_revision,
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
    observed = _get_video_in_project(db, project_id, video_id)
    observed_import = _get_active_import(db, video_id)
    with video_write_gate(
        db, project_id=project_id, video_id=video_id,
        expected_active_import_id=observed_import.id if observed_import else None,
        expected_detection_revision=observed.detection_import_revision,
        expected_edit_version=observed_import.edit_version if observed_import else None,
        expected_annotation_revision=observed.annotation_revision,
    ) as state:
        video = state.video
        category = _get_category_in_project(db, project_id, body.category_id)
        if not category.is_active:
            raise HTTPException(status_code=400, detail="Category is disabled")
        if body.confidence not in VALID_CONFIDENCE:
            raise HTTPException(status_code=400, detail=f"confidence must be one of {sorted(VALID_CONFIDENCE)}")
        _validate_interval(body.start_time, body.end_time, body.start_frame, body.end_frame)

        imp = state.detection_import
        mouse_ids: list[int] = []
        mouse_id_status = "needs_mouse_ids"
        di_rev = imp.revision if imp else 0
        id_rev = video.identity_revision
        if body.mouse_ids is not None:
            if imp is None:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot specify mouse_ids without an active detection import",
                )
            deduped = sorted(set(body.mouse_ids))
            _validate_mouse_ids(db, imp, deduped, body.start_frame, body.end_frame, category, id_rev)
            mouse_ids = deduped
            mouse_id_status = "valid"

        plan = _invalidate_video(db, video, request.app.state.settings, increment_media=True)
        annotation = Annotation(
            video_id=video_id, annotator_id=access[1].user_id, category_id=category.id,
            start_time=body.start_time, end_time=body.end_time,
            start_frame=body.start_frame, end_frame=body.end_frame,
            confidence=body.confidence, review_status="pending", crop_region=body.crop_region,
            mouse_ids=mouse_ids, mouse_id_status=mouse_id_status,
            detection_import_revision=di_rev, identity_revision=id_rev,
        )
        db.add(annotation)
        db.commit()
        db.refresh(annotation)
        result = _to_out(annotation)
    _cleanup_files(plan, request.app.state.settings)
    return result


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
        ready_clips = [clip for clip in ann.clips if clip.status == "ready" and clip.clip_path]
        clip_file = max(ready_clips, key=lambda clip: clip.source_revision).clip_path if ready_clips else None
        events.append(_legacy_event_record(ann, clip_file=clip_file))
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
    observed = _get_video_in_project(db, project_id, video_id)
    observed_import = _get_active_import(db, video_id)
    with video_write_gate(
        db, project_id=project_id, video_id=video_id,
        expected_active_import_id=observed_import.id if observed_import else None,
        expected_detection_revision=observed.detection_import_revision,
        expected_edit_version=observed_import.edit_version if observed_import else None,
        expected_annotation_revision=observed.annotation_revision,
    ) as state:
        video = state.video
        annotation = _get_annotation_in_video(db, video_id, annotation_id)
        if not _can_modify(access[1].role, annotation, access[1].user_id):
            raise HTTPException(
                status_code=403,
                detail="Only the annotator or project owner/admin can modify this annotation",
            )
        if (body.detection_import_revision is not None
                and body.detection_import_revision != annotation.detection_import_revision):
            raise HTTPException(status_code=409, detail=(
                f"detection_import_revision mismatch: expected {annotation.detection_import_revision}, "
                f"got {body.detection_import_revision}"
            ))
        if body.identity_revision is not None and body.identity_revision != annotation.identity_revision:
            raise HTTPException(status_code=409, detail=(
                f"identity_revision mismatch: expected {annotation.identity_revision}, got {body.identity_revision}"
            ))
        if body.category_id is not None:
            category = _get_category_in_project(db, project_id, body.category_id)
            if not category.is_active:
                raise HTTPException(status_code=400, detail="Category is disabled")
        if body.confidence is not None and body.confidence not in VALID_CONFIDENCE:
            raise HTTPException(status_code=400, detail=f"confidence must be one of {sorted(VALID_CONFIDENCE)}")

        changed_fields = {
            field for field in ("category_id", "start_time", "end_time", "start_frame",
                                "end_frame", "confidence", "crop_region")
            if getattr(body, field) is not None
            and getattr(body, field) != getattr(annotation, field)
        }
        normalized_mouse_ids = (sorted(set(body.mouse_ids)) if body.mouse_ids is not None else None)
        if normalized_mouse_ids is not None and normalized_mouse_ids != (annotation.mouse_ids or []):
            changed_fields.add("mouse_ids")
        media_changed = bool(changed_fields.intersection(
            {"start_time", "end_time", "start_frame", "end_frame", "crop_region"}))
        plan = (_invalidate_video(db, video, request.app.state.settings, increment_media=media_changed)
                if changed_fields else _InvalidationPlan())
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
        _validate_interval(annotation.start_time, annotation.end_time,
                           annotation.start_frame, annotation.end_frame)

        if body.mouse_ids is not None:
            imp = state.detection_import
            if imp is None:
                raise HTTPException(status_code=400,
                                    detail="Cannot specify mouse_ids without an active detection import")
            cat = (db.get(BehaviorCategory, body.category_id)
                   if body.category_id is not None else annotation.category)
            deduped = normalized_mouse_ids
            _validate_mouse_ids(db, imp, deduped, annotation.start_frame,
                                annotation.end_frame, cat, video.identity_revision)
            annotation.mouse_ids = deduped
            annotation.mouse_id_status = "valid"
            annotation.detection_import_revision = imp.revision
            annotation.identity_revision = video.identity_revision
        elif ((body.category_id is not None or body.start_frame is not None or body.end_frame is not None)
              and annotation.mouse_ids):
            imp = state.detection_import
            if imp is not None:
                cat = (db.get(BehaviorCategory, body.category_id)
                       if body.category_id is not None else annotation.category)
                try:
                    _validate_mouse_ids(db, imp, annotation.mouse_ids, annotation.start_frame,
                                        annotation.end_frame, cat, video.identity_revision)
                except HTTPException:
                    annotation.mouse_id_status = "needs_mouse_ids"
        db.commit()
        db.refresh(annotation)
        result = _to_out(annotation)
    _cleanup_files(plan, request.app.state.settings)
    return result


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
    observed = _get_video_in_project(db, project_id, video_id)
    observed_import = _get_active_import(db, video_id)
    with video_write_gate(
        db, project_id=project_id, video_id=video_id,
        expected_active_import_id=observed_import.id if observed_import else None,
        expected_detection_revision=observed.detection_import_revision,
        expected_edit_version=observed_import.edit_version if observed_import else None,
        expected_annotation_revision=observed.annotation_revision,
    ) as state:
        video = state.video
        annotation = _get_annotation_in_video(db, video_id, annotation_id)
        if not _can_modify(access[1].role, annotation, access[1].user_id):
            raise HTTPException(
                status_code=403,
                detail="Only the annotator or project owner/admin can delete this annotation",
            )
        plan = _invalidate_video(db, video, request.app.state.settings, increment_media=True)
        db.delete(annotation)
        db.commit()
    _cleanup_files(plan, request.app.state.settings)
