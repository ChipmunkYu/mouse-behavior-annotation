"""媒体任务编排（批次 4）：单 worker 领取 / 逐片精确重编码 / 可恢复重试 / 修订隔离。

策略（与 README / 任务契约一致）：
- **单 worker**：`ThreadPoolExecutor(max_workers=1)`；`media_synchronous=True` 时在调用
  线程内同步执行（测试可完全确定性驱动，配合可替换执行器不要求系统 ffmpeg）。
- **领取原子性**：条件 `UPDATE ... SET status='running' WHERE id=? AND status='queued'`
  独占，杜绝两个线程领取同一 queued 任务；领取同时 attempts+1。
- **启动恢复**：`running` 视为中断——`attempts < media_max_attempts` 则重排并 attempts+1，
  否则判 failed（重试上限耗尽，可经 API 手动重试）；启动时统一调度全部 queued 任务。
- **修订隔离**：处理前 / 每片前 / 完成后均校验视频仍 approved 且 revision 与任务
  payload 一致；失效 → 任务 cancelled 并清理**本次运行产出**的实体文件，绝不复活
  已被删除的 Clip 行（worker 从不创建 Clip 行）。
- **部分失败**：失败的 Clip 置 failed 并写入截断错误；Job 置 failed 并记录摘要；
  已成功的片段保留；重试只处理 pending/failed（未 ready）的 Clip，成功片段跳过。
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .media import MediaCommandError, MediaProcessor
from .models import Annotation, BackgroundJob, Clip, Submission, SubmissionAnnotation, Video
from .submission_service import validate_snapshot_integrity, validate_storage_key
from .submission_media_plan import build_submission_media_plan
from .file_identity import FileIdentity, stream_identity

logger = logging.getLogger(__name__)

JOB_TYPE_MEDIA = "media"
# Clip 错误字段与任务摘要的截断上限
ERROR_TRUNCATE_LIMIT = 2000
CLEANUP_INCOMPLETE_PREFIX = "Worker cleanup incomplete: "


class WorkerCleanupIncomplete(RuntimeError):
    """A required worker cleanup failed; the owning job must remain active."""

    def __init__(self, message: str, *, result_path: str | None = None) -> None:
        super().__init__(message)
        self.result_path = result_path


def _cleanup_paths(paths, *, operation: str) -> None:
    errors = []
    for path in paths:
        if path is None:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"{operation} {path}: {exc}")
    if errors:
        raise WorkerCleanupIncomplete("; ".join(errors))


def _cleanup_incomplete_error(exc: BaseException) -> str:
    return _truncate_error(f"{CLEANUP_INCOMPLETE_PREFIX}{exc}")


def _is_cleanup_incomplete(job: BackgroundJob) -> bool:
    return bool(job.error and job.error.startswith(CLEANUP_INCOMPLETE_PREFIX))


def _fault(_stage: str) -> None:
    """Test-only crash seam around filesystem/DB atomicity boundaries."""


def _now() -> datetime:
    return datetime.utcnow()


def media_dedupe_key(video_id: int, revision: int) -> str:
    """媒体任务去重键：同一视频+修订唯一（幂等入队 / 防重复任务）。"""
    return f"media:video:{video_id}:rev:{revision}"


def submission_media_dedupe_key(submission_id: int) -> str:
    return f"media:submission:{submission_id}"


def _truncate_error(text: str, limit: int = ERROR_TRUNCATE_LIMIT) -> str:
    if len(text) > limit:
        return text[:limit] + f"...[truncated, {len(text)} chars]"
    return text


# ---------- 入队（幂等 / 可重试） ----------


def _upsert_media_job(db: Session, video: Video, *, force_requeue: bool = False) -> BackgroundJob:
    """按 dedupe_key 幂等创建/复用媒体任务行。

    - 无任务行 → INSERT（并发竞态由唯一索引兜底，冲突后回退复用已有行）。
    - 已有 queued/running/succeeded → 原样返回（幂等，不重复调度）。
    - 已有 failed/cancelled → 重置回 queued（重试；attempts 清零，保留同一行）。
    """
    dedupe_key = media_dedupe_key(video.id, video.media_revision)
    payload = {
        "video_id": video.id,
        "project_id": video.project_id,
        "revision": video.media_revision,
    }
    try:
        db.execute(
            sqlite_insert(BackgroundJob)
            .values(
                project_id=video.project_id,
                job_type=JOB_TYPE_MEDIA,
                status="queued",
                progress=0,
                dedupe_key=dedupe_key,
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
        )
    except IntegrityError:
        # 极端并发：唯一索引竞态兜底，回滚本次插入后复用已有行
        db.rollback()
    job = db.query(BackgroundJob).filter(BackgroundJob.dedupe_key == dedupe_key).one()
    if job.status in ("failed", "cancelled") or (force_requeue and job.status == "succeeded"):
        job.status = "queued"
        job.progress = 0
        job.error = None
        job.started_at = None
        job.finished_at = None
        job.attempts = 0
    return job


def resolve_input_path(settings, video: Video) -> Path:
    """解析源视频路径：严格限制在配置 videos_dir 内，缺失/越界抛 MediaCommandError。

    独立函数供媒体 worker 与导出补生成共用（批次 6）。
    """
    if not video.storage_path:
        raise MediaCommandError("video has no storage_path; nothing to re-encode")
    videos_dir = settings.videos_dir.resolve()
    raw = Path(video.storage_path)
    path = raw.resolve() if raw.is_absolute() else (videos_dir / raw).resolve()
    if path == videos_dir or not path.is_relative_to(videos_dir):
        raise MediaCommandError(
            "video storage_path escapes the configured videos directory"
        )
    if not path.is_file():
        raise MediaCommandError(f"video file missing on disk: {video.storage_path}")
    return path


def stage_submission_input(settings, submission: Submission, job_id: int) -> Path:
    """Copy/hash one verified open source handle into a job-private staging file."""
    key = validate_storage_key(submission.source_storage_key)
    root = settings.videos_dir.resolve()
    path = (root / key).resolve()
    if path == root or not path.is_relative_to(root) or not path.is_file():
        raise MediaCommandError("immutable submission source is missing or outside videos directory")
    expected = FileIdentity(submission.source_file_size, submission.source_mtime_ns,
                            submission.source_device, submission.source_inode)
    staging = root / f".submission-media-job-{job_id}-{uuid.uuid4().hex}.staging"
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source, staging.open("xb") as target:
            before = stream_identity(source)
            if expected.device and expected.inode and before != expected:
                raise MediaCommandError("immutable submission source file identity mismatch")
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            after = stream_identity(source)
        if before != after:
            raise MediaCommandError("immutable submission source changed while staging")
        if digest.hexdigest() != submission.source_video_sha256:
            raise MediaCommandError("immutable submission source SHA-256 mismatch")
        return staging
    except Exception as exc:
        try:
            _cleanup_paths([staging], operation="remove submission staging")
        except WorkerCleanupIncomplete as cleanup_exc:
            raise cleanup_exc from exc
        raise


def resolve_entity_path(stored: str | None, root_dir: Path) -> Path | None:
    """Resolve a stored media path without allowing access outside its configured root."""
    if not stored:
        return None
    root = root_dir.resolve()
    raw = Path(stored)
    path = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    if path == root or not path.is_relative_to(root):
        return None
    return path


def clip_entities_ready(clip: Clip, settings) -> bool:
    """A ready row is usable only when both safely-resolved entities still exist."""
    clip_path = resolve_entity_path(clip.clip_path, settings.clips_dir)
    thumb_path = resolve_entity_path(clip.thumbnail_path, settings.thumbnails_dir)
    return bool(clip.status == "ready" and clip_path and clip_path.is_file() and thumb_path and thumb_path.is_file())


def render_clip_files(processor, settings, video: Video, annotation: Annotation, clip: Clip) -> list[Path]:
    """渲染单个 Clip 的 mp4 + 缩略图（临时文件 → 原子替换），供媒体 worker 与导出补生成共用。

    - 输入源解析严格限制在 settings.videos_dir 内（越界/缺失 → MediaCommandError）。
    - 输出先写临时 `.part`，成功后原子替换；失败清理半成品并重新抛出。
    - 成功后更新 clip 的 clip_path / thumbnail_path / status=ready / error / generated_at。
    - 返回本次成功落盘的最终文件列表（供失效清理跟踪）。
    """
    input_path = resolve_input_path(settings, video)
    name = f"clip_{clip.annotation_id}_rev{clip.source_revision}"
    render_id = uuid.uuid4().hex
    temp_clip = settings.clips_dir / f".{name}.{render_id}.mp4.part"
    temp_thumb = settings.thumbnails_dir / f".{name}.{render_id}.jpg.part"
    final_clip = settings.clips_dir / f"{name}.mp4"
    final_thumb = settings.thumbnails_dir / f"{name}.jpg"
    settings.clips_dir.mkdir(parents=True, exist_ok=True)
    settings.thumbnails_dir.mkdir(parents=True, exist_ok=True)

    created: list[Path] = []  # 本次调用已落盘的最终文件（失败时清理，避免半成品）
    try:
        if not video.fps or video.fps <= 0:
            raise MediaCommandError("video FPS must be positive for frame-authoritative rendering")
        start = annotation.start_frame / video.fps
        end = (annotation.end_frame + 1) / video.fps
        processor.render_clip(
            input_path=str(input_path),
            start=start,
            end=end,
            output_path=str(temp_clip),
        )
        mid = (start + end) / 2.0  # 缩略图取 inclusive 帧区间的时间中点
        processor.render_thumbnail(
            input_path=str(input_path),
            at=mid,
            output_path=str(temp_thumb),
        )
        # 成功后原子替换（同目录 .part → 最终名）
        os.replace(temp_clip, final_clip)
        created.append(final_clip)
        os.replace(temp_thumb, final_thumb)
        created.append(final_thumb)
        clip.clip_path = f"{name}.mp4"
        clip.thumbnail_path = f"{name}.jpg"
        clip.status = "ready"
        clip.error = None
        clip.generated_at = _now()
        clip.updated_at = _now()
    except Exception as exc:
        try:
            _cleanup_paths([*created, temp_clip, temp_thumb], operation="rollback rendered media")
        except WorkerCleanupIncomplete as cleanup_exc:
            raise cleanup_exc from exc
        raise
    try:
        _cleanup_paths([temp_clip, temp_thumb], operation="remove rendered media temp")
    except WorkerCleanupIncomplete as exc:
        try:
            _cleanup_paths(created, operation="rollback rendered media after temp cleanup failure")
        except WorkerCleanupIncomplete as rollback_exc:
            raise WorkerCleanupIncomplete(f"{exc}; {rollback_exc}") from exc
        raise
    return created


def render_submission_clip_files(processor, settings, submission: Submission,
                                  annotation: SubmissionAnnotation, clip: Clip,
                                  *, input_path: Path) -> list[Path]:
    name = f"clip_{annotation.id}_revsub{annotation.submission_id}"
    render_id = uuid.uuid4().hex
    temp_clip = settings.clips_dir / f".{name}.{render_id}.mp4.part"
    temp_thumb = settings.thumbnails_dir / f".{name}.{render_id}.jpg.part"
    final_clip = settings.clips_dir / f"{name}.mp4"
    final_thumb = settings.thumbnails_dir / f"{name}.jpg"
    settings.clips_dir.mkdir(parents=True, exist_ok=True)
    settings.thumbnails_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    try:
        snapshot = submission.detection_snapshot
        plan = build_submission_media_plan(
            start_time=annotation.start_time, end_time=annotation.end_time,
            start_frame=annotation.start_frame, end_frame=annotation.end_frame,
            fps=snapshot.fps, frame_count=snapshot.frame_count,
            width=snapshot.width, height=snapshot.height, crop_region=annotation.crop_region)
        processor.render_clip(input_path=str(input_path), start=plan.start,
                              end=plan.end, output_path=str(temp_clip), crop=plan.crop)
        processor.render_thumbnail(input_path=str(input_path), at=plan.thumbnail_at,
                                   output_path=str(temp_thumb), crop=plan.crop)
        os.replace(temp_clip, final_clip); created.append(final_clip)
        os.replace(temp_thumb, final_thumb); created.append(final_thumb)
        _fault("submission_files_renamed")
        clip.clip_path, clip.thumbnail_path = final_clip.name, final_thumb.name
        clip.status, clip.error, clip.generated_at, clip.updated_at = "ready", None, _now(), _now()
    except Exception as exc:
        try:
            _cleanup_paths([*created, temp_clip, temp_thumb], operation="rollback Submission media")
        except WorkerCleanupIncomplete as cleanup_exc:
            raise cleanup_exc from exc
        raise
    try:
        _cleanup_paths([temp_clip, temp_thumb], operation="remove Submission media temp")
    except WorkerCleanupIncomplete as exc:
        try:
            _cleanup_paths(created, operation="rollback Submission media after temp cleanup failure")
        except WorkerCleanupIncomplete as rollback_exc:
            raise WorkerCleanupIncomplete(f"{exc}; {rollback_exc}") from exc
        raise
    return created


def claim_and_render_clip(
    db: Session,
    processor,
    settings,
    video_id: int,
    annotation_id: int,
    clip_id: int,
    *,
    wait_seconds: float = 10.0,
    poll_seconds: float = 0.05,
) -> tuple[Path, list[Path]]:
    """Claim one clip in the database, or wait for the current owner to finish it."""
    deadline = time.monotonic() + wait_seconds
    owns_claim = False
    claim_token: datetime | None = None
    created: list[Path] = []
    try:
        while True:
            next_claim_token = _now()
            claimed = (
                db.query(Clip)
                .filter(Clip.id == clip_id, Clip.status.in_(("pending", "failed")))
                .update(
                    {"status": "processing", "error": None, "updated_at": next_claim_token},
                    synchronize_session=False,
                )
            )
            db.commit()
            if claimed == 1:
                owns_claim = True
                claim_token = next_claim_token
                break
            db.expire_all()
            current = db.get(Clip, clip_id)
            if current is None:
                raise MediaCommandError(f"clip {clip_id} no longer exists")
            if clip_entities_ready(current, settings):
                path = resolve_entity_path(current.clip_path, settings.clips_dir)
                assert path is not None
                return path, []
            if current.status == "ready":
                db.query(Clip).filter(Clip.id == clip_id, Clip.status == "ready").update(
                    {
                        "status": "pending",
                        "clip_path": None,
                        "thumbnail_path": None,
                        "error": None,
                        "generated_at": None,
                        "updated_at": _now(),
                    },
                    synchronize_session=False,
                )
                db.commit()
                continue
            if current.status != "processing":
                continue
            if time.monotonic() >= deadline:
                raise MediaCommandError(f"timed out waiting for clip {clip_id} render owner")
            time.sleep(poll_seconds)

        db.expire_all()
        clip = db.get(Clip, clip_id)
        video = db.get(Video, video_id)
        annotation = db.get(Annotation, annotation_id)
        if clip is None or video is None or annotation is None:
            raise MediaCommandError("clip render input no longer exists")
        created = render_clip_files(processor, settings, video, annotation, clip)
        db.commit()
        path = resolve_entity_path(clip.clip_path, settings.clips_dir)
        if path is None or not path.is_file():
            raise MediaCommandError(f"clip file missing after render: {clip.clip_path}")
        return path, created
    except Exception as exc:
        db.rollback()
        try:
            _cleanup_paths(created, operation="rollback claimed media")
        except WorkerCleanupIncomplete as cleanup_exc:
            raise cleanup_exc from exc
        if owns_claim and claim_token is not None:
            db.query(Clip).filter(
                Clip.id == clip_id,
                Clip.status == "processing",
                Clip.updated_at == claim_token,
            ).update(
                {"status": "failed", "error": _truncate_error(str(exc)), "updated_at": _now()},
                synchronize_session=False,
            )
            db.commit()
        raise


def claim_and_render_submission_clip(
    db: Session, processor, settings, submission_id: int, annotation_id: int, clip_id: int,
    *, input_path: Path, wait_seconds: float = 10.0, poll_seconds: float = 0.05,
) -> tuple[Path, list[Path]]:
    """CAS claim/wait primitive shared by Submission media and project export workers."""
    deadline = time.monotonic() + wait_seconds
    owns_claim = False
    claim_token: datetime | None = None
    created: list[Path] = []
    try:
        while True:
            next_token = _now()
            claimed = db.query(Clip).filter(
                Clip.id == clip_id, Clip.status.in_(("pending", "failed"))
            ).update({"status": "processing", "error": None, "updated_at": next_token},
                     synchronize_session=False)
            db.commit()
            if claimed == 1:
                owns_claim, claim_token = True, next_token
                break
            db.expire_all(); current = db.get(Clip, clip_id)
            if current is None:
                raise MediaCommandError(f"clip {clip_id} no longer exists")
            if clip_entities_ready(current, settings):
                path = resolve_entity_path(current.clip_path, settings.clips_dir)
                assert path is not None
                return path, []
            if current.status == "ready":
                db.query(Clip).filter(Clip.id == clip_id, Clip.status == "ready").update({
                    "status": "pending", "clip_path": None, "thumbnail_path": None,
                    "error": None, "generated_at": None, "updated_at": _now()},
                    synchronize_session=False)
                db.commit(); continue
            if current.status != "processing":
                continue
            if time.monotonic() >= deadline:
                raise MediaCommandError(f"timed out waiting for clip {clip_id} render owner")
            time.sleep(poll_seconds)
        db.expire_all()
        clip, submission, annotation = (db.get(Clip, clip_id), db.get(Submission, submission_id),
                                        db.get(SubmissionAnnotation, annotation_id))
        if clip is None or submission is None or annotation is None:
            raise MediaCommandError("Submission clip render input no longer exists")
        created = render_submission_clip_files(processor, settings, submission, annotation, clip,
                                               input_path=input_path)
        db.commit()
        path = resolve_entity_path(clip.clip_path, settings.clips_dir)
        if path is None or not path.is_file():
            raise MediaCommandError(f"clip file missing after render: {clip.clip_path}")
        return path, created
    except Exception as exc:
        db.rollback()
        try:
            _cleanup_paths(created, operation="rollback claimed Submission media")
        except WorkerCleanupIncomplete as cleanup_exc:
            raise cleanup_exc from exc
        if owns_claim and claim_token is not None:
            db.query(Clip).filter(Clip.id == clip_id, Clip.status == "processing",
                                  Clip.updated_at == claim_token).update(
                {"status": "failed", "error": _truncate_error(str(exc)), "updated_at": _now()},
                synchronize_session=False)
            db.commit()
        raise


def ensure_pending_clips(db: Session, video: Video) -> int:
    """为视频当前修订的全部标注幂等创建 pending Clip 行（唯一约束兜底并发重复）。"""
    stmt = sqlite_insert(Clip)
    created = 0
    for ann in db.query(Annotation).filter(Annotation.video_id == video.id).all():
        result = db.execute(
            stmt.values(
                project_id=video.project_id,
                annotation_id=ann.id,
                source_revision=video.media_revision,
                media_revision=video.media_revision,
                status="pending",
            ).on_conflict_do_nothing(index_elements=["annotation_id", "source_revision"])
        )
        created += result.rowcount or 0
    return created


def reset_missing_ready_clips(db: Session, video: Video, settings) -> bool:
    """Atomically make ready rows with missing/unsafe entities eligible for regeneration."""
    changed = False
    clips = (
        db.query(Clip)
        .join(Annotation, Annotation.id == Clip.annotation_id)
        .filter(
            Annotation.video_id == video.id,
            Clip.source_revision == video.media_revision,
            Clip.status == "ready",
        )
        .all()
    )
    for clip in clips:
        if clip_entities_ready(clip, settings):
            continue
        updated = (
            db.query(Clip)
            .filter(Clip.id == clip.id, Clip.status == "ready")
            .update(
                {
                    "status": "pending",
                    "clip_path": None,
                    "thumbnail_path": None,
                    "error": None,
                    "generated_at": None,
                    "updated_at": _now(),
                },
                synchronize_session=False,
            )
        )
        changed = changed or updated == 1
    return changed


def reset_interrupted_job_clips(db: Session, job: BackgroundJob) -> int:
    """CAS-reset processing clips owned by one interrupted, requeued startup job.

    This is startup recovery, not timeout-based claim stealing. Callers must finish
    recovery for both worker types before either worker starts scheduling jobs.
    """
    payload = job.payload or {}
    candidates: list[Clip] = []
    if job.job_type == JOB_TYPE_MEDIA:
        submission_id = payload.get("submission_id")
        if submission_id:
            annotation_ids = payload.get("submission_annotation_ids") or []
            candidates = db.query(Clip).filter(
                Clip.submission_annotation_id.in_(annotation_ids), Clip.status == "processing"
            ).all()
        else:
            video_id = payload.get("video_id")
            revision = payload.get("revision")
            video = db.get(Video, video_id) if video_id else None
            if video is not None and video.media_revision == revision:
                candidates = (
                    db.query(Clip)
                    .join(Annotation, Annotation.id == Clip.annotation_id)
                    .filter(
                        Annotation.video_id == video.id,
                        Clip.source_revision == revision,
                        Clip.status == "processing",
                    )
                    .all()
                )
    elif job.job_type == "export":
        submission_annotation_ids = set(payload.get("submission_annotation_ids") or [])
        if submission_annotation_ids:
            # Clip has no claim owner/token. Startup recovery is therefore deliberately
            # bounded to the immutable refs frozen in this interrupted export job.
            candidates = db.query(Clip).filter(
                Clip.submission_annotation_id.in_(submission_annotation_ids),
                Clip.status == "processing",
            ).all()
        else:
            # Legacy mutable-authority export payload compatibility.
            annotation_ids = set(payload.get("annotation_ids") or [])
            revisions = {
                int(video_id): revision
                for video_id, revision in (payload.get("video_revisions") or {}).items()
            }
        if not submission_annotation_ids and annotation_ids and revisions:
            rows = (
                db.query(Clip, Video)
                .join(Annotation, Annotation.id == Clip.annotation_id)
                .join(Video, Video.id == Annotation.video_id)
                .filter(
                    Annotation.id.in_(annotation_ids),
                    Clip.status == "processing",
                    Clip.source_revision == Video.media_revision,
                )
                .all()
            )
            candidates = [
                clip
                for clip, video in rows
                if revisions.get(video.id) == video.annotation_revision
            ]

    reset = 0
    for clip in candidates:
        reset += (
            db.query(Clip)
            .filter(
                Clip.id == clip.id,
                Clip.status == "processing",
                Clip.updated_at == clip.updated_at,
            )
            .update(
                {
                    "status": "pending",
                    "clip_path": None,
                    "thumbnail_path": None,
                    "error": None,
                    "generated_at": None,
                    "updated_at": _now(),
                },
                synchronize_session=False,
            )
        )
    return reset


def enqueue_media_job(db: Session, video: Video, settings=None) -> BackgroundJob | None:
    """审核通过后自动入队（幂等）：创建 pending Clips + (重)入队媒体任务并提交。

    在 review 提交成功（已 commit）后调用；若视频已非 approved（提交后到入队前的
    并发失效窗口）返回 None，不创建任何行。提交失败/异常由调用方记录日志。
    """
    # 复查：视频仍 approved（评审提交后的并发失效竞态窗口）
    if video.workflow_status != "approved":
        return None
    ensure_pending_clips(db, video)
    repaired = reset_missing_ready_clips(db, video, settings) if settings is not None else False
    job = _upsert_media_job(db, video, force_requeue=repaired)
    db.commit()
    db.refresh(job)
    return job


def enqueue_submission_media(db: Session, submission: Submission) -> BackgroundJob:
    """Create immutable-authority Clip and queued job rows; caller owns transaction."""
    annotation_ids = [row[0] for row in db.query(SubmissionAnnotation.id).filter_by(
        submission_id=submission.id).order_by(SubmissionAnnotation.id)]
    for annotation_id in annotation_ids:
        db.execute(sqlite_insert(Clip).values(
            submission_annotation_id=annotation_id, status="pending", media_revision=1,
        ).on_conflict_do_nothing(
            index_elements=["submission_annotation_id"],
            index_where=Clip.submission_annotation_id.is_not(None),
        ))
    key = submission_media_dedupe_key(submission.id)
    db.execute(sqlite_insert(BackgroundJob).values(
        project_id=submission.video.project_id, job_type=JOB_TYPE_MEDIA, status="queued",
        progress=0, dedupe_key=key,
        payload={"submission_id": submission.id, "submission_annotation_ids": annotation_ids},
    ).on_conflict_do_nothing(index_elements=["dedupe_key"]))
    job = db.query(BackgroundJob).filter_by(dedupe_key=key).one()
    if job.status in {"failed", "cancelled"}:
        job.status, job.progress, job.error, job.attempts = "queued", 0, None, 0
    return job


# ---------- worker ----------


class MediaWorker:
    """单任务媒体执行器：串行处理媒体任务，可注入执行器与同步/后台调度。"""

    def __init__(self, processor: MediaProcessor, session_factory, settings) -> None:
        self.processor = processor
        self.session_factory = session_factory
        self.settings = settings
        self.synchronous = settings.media_synchronous
        self._executor: ThreadPoolExecutor | None = None
        if not self.synchronous:
            self._executor = ThreadPoolExecutor(max_workers=1)
        self._futures: set = set()
        self.before_terminal_hook = None

    # ---------- 生命周期 ----------

    def start(self, *, recover: bool = True) -> None:
        """启动恢复：running 视为中断（重排或判失败）；随后调度全部 queued 媒体任务。"""
        if recover:
            self._recover_interrupted()
        with self.session_factory() as db:
            job_ids = [
                row[0]
                for row in db.query(BackgroundJob.id)
                .filter(
                    BackgroundJob.status == "queued",
                    BackgroundJob.job_type == JOB_TYPE_MEDIA,
                )
                .all()
            ]
        for job_id in job_ids:
            self.schedule(job_id)

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def schedule(self, job_id: int) -> None:
        """调度一个任务。同步模式：在当前线程内立即执行（测试确定性）。"""
        if self.synchronous:
            self._run_job(job_id)
            return
        future = self._executor.submit(self._run_job, job_id)
        self._futures.add(future)
        future.add_done_callback(self._futures.discard)

    def _recover_interrupted(self) -> None:
        """中断恢复策略：running → (attempts < 上限 ? 重排 : 判失败)。"""
        max_attempts = self.settings.media_max_attempts
        with self.session_factory() as db:
            rows = (
                db.query(BackgroundJob)
                .filter(
                    BackgroundJob.status == "running",
                    BackgroundJob.job_type == JOB_TYPE_MEDIA,
                )
                .all()
            )
            for job in rows:
                if _is_cleanup_incomplete(job):
                    continue
                # A processing Clip has no durable owner token. Always release claims
                # from this interrupted job, including when the job itself is exhausted.
                reset_interrupted_job_clips(db, job)
                if job.attempts >= max_attempts:
                    job.status = "failed"
                    job.error = (
                        f"Interrupted at startup after {job.attempts} attempts "
                        "(retry limit reached); re-submit the job to retry"
                    )
                    job.finished_at = _now()
                else:
                    job.status = "queued"
                    job.started_at = None
                    job.error = "Interrupted; requeued at startup"
            db.commit()

    # ---------- 任务执行 ----------

    def _run_job(self, job_id: int) -> None:
        try:
            self._run_job_impl(job_id)
        except WorkerCleanupIncomplete as exc:
            logger.error("Media job %s cleanup incomplete: %s", job_id, exc)
            self._record_cleanup_incomplete(job_id, exc)
        except Exception:  # noqa: BLE001  worker 线程绝不外泄异常
            logger.exception("Media job %s crashed unexpectedly", job_id)
            try:
                with self.session_factory() as db:
                    job = db.get(BackgroundJob, job_id)
                    if job is not None and job.status == "running":
                        if self.before_terminal_hook:
                            self.before_terminal_hook()
                        job.status = "failed"
                        job.error = "Media worker crashed unexpectedly (see server log)"
                        job.finished_at = _now()
                        db.commit()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to record crash for media job %s", job_id)

    def _record_cleanup_incomplete(self, job_id: int, exc: BaseException) -> None:
        try:
            with self.session_factory() as db:
                job = db.get(BackgroundJob, job_id)
                if job is not None and job.status == "running":
                    job.error = _cleanup_incomplete_error(exc)
                    job.finished_at = None
                    db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record incomplete cleanup for media job %s", job_id)

    def _commit_terminal(self, db: Session, job: BackgroundJob, status: str, *, error=None) -> None:
        if self.before_terminal_hook:
            self.before_terminal_hook()
        job.status = status
        job.error = error
        job.finished_at = _now()
        db.commit()

    def _run_job_impl(self, job_id: int) -> None:
        with self.session_factory() as db:
            job = db.get(BackgroundJob, job_id)
            if job is None:
                return
            # 原子领取：仅 queued → running（attempts+1）；rowcount != 1 说明已被领取/完成
            claimed = db.query(BackgroundJob).filter(
                BackgroundJob.id == job.id, BackgroundJob.status == "queued"
            ).update(
                {
                    "status": "running",
                    "started_at": _now(),
                    "attempts": BackgroundJob.attempts + 1,
                },
                synchronize_session=False,
            )
            db.commit()
            if claimed != 1:
                return
            db.expire_all()
            job = db.get(BackgroundJob, job_id)
            if job is None:
                return
            self._process_job(db, job)

    # ---------- 单任务处理 ----------

    def _invalidation_reason(self, db: Session, video_id: int, revision: int) -> str | None:
        """返回失效原因；视频仍 approved 且修订一致返回 None。"""
        video = db.get(Video, video_id)
        if video is None:
            return "video no longer exists"
        if video.workflow_status != "approved":
            return f"video workflow is {video.workflow_status!r}, not 'approved'"
        if video.media_revision != revision:
            return (
                f"revision mismatch (job revision {revision}, "
                f"video revision {video.media_revision})"
            )
        return None

    def _clips_for_revision(self, db: Session, video_id: int, revision: int) -> list[Clip]:
        return (
            db.query(Clip)
            .join(Annotation, Annotation.id == Clip.annotation_id)
            .filter(Annotation.video_id == video_id, Clip.source_revision == revision)
            .order_by(Clip.id)
            .all()
        )

    def _resolve_input(self, video: Video) -> Path:
        """解析源视频路径（委托模块级 `resolve_input_path`，媒体 worker 与导出共用）。"""
        return resolve_input_path(self.settings, video)

    def _process_job(self, db: Session, job: BackgroundJob) -> None:
        payload = job.payload or {}
        if payload.get("submission_id"):
            self._process_submission_job(db, job)
            return
        video_id = payload.get("video_id")
        revision = payload.get("revision")
        if not video_id or not revision:
            raise MediaCommandError("media job payload missing video_id/revision")
        if payload.get("project_id") != job.project_id:
            raise MediaCommandError("media job payload project_id does not match job project_id")
        video = db.get(Video, video_id)
        if video is None or video.project_id != job.project_id:
            raise MediaCommandError("media job video does not belong to job project")
        settings = self.settings

        # ---- 处理前检查 ----
        reason = self._invalidation_reason(db, video_id, revision)
        if reason is not None:
            self._cancel_invalidated(db, job, reason)
            return

        clips = self._clips_for_revision(db, video_id, revision)
        total = len(clips)
        if total == 0:
            job.progress = 100
            self._commit_terminal(db, job, "succeeded")
            return

        produced: list[Path] = []  # 本次运行成功产出的最终文件（失效时清理）
        failures: list[str] = []
        for clip in clips:
            clip = db.get(Clip, clip.id)  # 重取：并发失效可能已删除该行
            if clip is None:
                continue
            if clip_entities_ready(clip, settings):
                continue
            # ---- 每片前检查 ----
            reason = self._invalidation_reason(db, video_id, revision)
            if reason is not None:
                self._cleanup_produced(produced, settings)
                self._cancel_invalidated(db, job, reason)
                return
            video = db.get(Video, video_id)
            try:
                created = self._process_clip(db, video, clip)
                produced.extend(created)
            except WorkerCleanupIncomplete:
                db.rollback()
                raise
            except Exception as exc:  # noqa: BLE001  单片段失败 → 该片 failed，任务 failed
                db.rollback()
                failures.append(str(exc))
                break
            # 进度更新（ready 数 / 总片数）
            ready = (
                db.query(Clip)
                .join(Annotation, Annotation.id == Clip.annotation_id)
                .filter(
                    Annotation.video_id == video_id,
                    Clip.source_revision == revision,
                    Clip.status == "ready",
                )
                .count()
            )
            job = db.get(BackgroundJob, job.id)
            if job is not None:
                job.progress = int(ready * 100 / total) if total else 100
                db.commit()

        if failures:
            job = db.get(BackgroundJob, job.id)
            if job is not None:
                error = _truncate_error(
                    f"Clip generation failed: {len(failures)} clip(s) failed"
                    f" (first error: {failures[0]})"
                )
                self._commit_terminal(db, job, "failed", error=error)
            return

        # ---- 完成后复查：失效则取消并清理本次产物 ----
        reason = self._invalidation_reason(db, video_id, revision)
        if reason is not None:
            self._cleanup_produced(produced, settings)
            self._cancel_invalidated(db, job, reason)
            return

        job.progress = 100
        self._commit_terminal(db, job, "succeeded")

    def _process_submission_job(self, db: Session, job: BackgroundJob) -> None:
        payload = job.payload or {}
        submission = db.get(Submission, payload.get("submission_id"))
        expected_ids = payload.get("submission_annotation_ids") or []
        if (not isinstance(expected_ids, list) or not expected_ids
                or any(isinstance(value, bool) or not isinstance(value, int) for value in expected_ids)
                or expected_ids != sorted(set(expected_ids))):
            raise MediaCommandError("media job immutable annotation payload is invalid or duplicated")
        if submission is None or submission.status not in {"approved", "superseded"}:
            raise MediaCommandError("media job submission is missing or not approved")
        if submission.video.project_id != job.project_id:
            raise MediaCommandError("media job submission does not belong to job project")
        validate_snapshot_integrity(db, submission.detection_snapshot)
        annotations = db.query(SubmissionAnnotation).filter(
            SubmissionAnnotation.submission_id == submission.id,
            SubmissionAnnotation.id.in_(expected_ids),
        ).order_by(SubmissionAnnotation.id).all()
        if [item.id for item in annotations] != expected_ids:
            raise MediaCommandError("media job immutable annotation set is inconsistent")
        pairs = db.query(SubmissionAnnotation, Clip).join(
            Clip, Clip.submission_annotation_id == SubmissionAnnotation.id
        ).filter(SubmissionAnnotation.submission_id == submission.id,
                 SubmissionAnnotation.id.in_(expected_ids)).order_by(SubmissionAnnotation.id).all()
        if [annotation.id for annotation, _clip in pairs] != expected_ids:
            raise MediaCommandError("media job Clip set is incomplete")
        failures = []
        staging = None
        try:
            if any(not clip_entities_ready(clip, self.settings) for _annotation, clip in pairs):
                staging = stage_submission_input(self.settings, submission, job.id)
                _fault("submission_source_staged")
            for index, (annotation, clip) in enumerate(pairs, 1):
                if clip_entities_ready(clip, self.settings):
                    continue
                try:
                    claim_and_render_submission_clip(
                        db, self.processor, self.settings, submission.id, annotation.id, clip.id,
                        input_path=staging,
                    )
                except WorkerCleanupIncomplete:
                    db.rollback()
                    raise
                except Exception as exc:
                    db.rollback(); failures.append(str(exc)); break
                job = db.get(BackgroundJob, job.id)
                job.progress = int(index * 100 / len(pairs)) if pairs else 100
                db.commit()
        except WorkerCleanupIncomplete:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            first_pending = next((clip for _annotation, clip in pairs
                                  if not clip_entities_ready(clip, self.settings)), None)
            if first_pending is not None:
                clip = db.get(Clip, first_pending.id)
                clip.status, clip.error, clip.updated_at = "failed", _truncate_error(str(exc)), _now()
                db.commit()
            failures.append(str(exc))
        finally:
            if staging is not None:
                _cleanup_paths([staging], operation="remove Submission staging")
        job = db.get(BackgroundJob, job.id)
        if failures:
            self._commit_terminal(db, job, "failed", error=_truncate_error(failures[0]))
        else:
            job.progress = 100
            self._commit_terminal(db, job, "succeeded")

    def _process_clip(self, db: Session, video: Video, clip: Clip) -> list[Path]:
        """渲染单个 Clip（委托模块级 `render_clip_files`）：临时文件 → 原子替换。

        路径均限制在 clips_dir / thumbnails_dir 内，DB 存相对名。
        失败由 `render_clip_files` 清理半成品并抛错，调用方把该 Clip 置 failed。
        """
        return claim_and_render_clip(
            db, self.processor, self.settings, video.id, clip.annotation_id, clip.id
        )[1]

    def _cleanup_produced(self, files: list[Path], settings) -> None:
        """失效时严格清理本次运行产出的实体文件。"""
        _cleanup_paths(files, operation="remove invalidated produced media")

    def _cancel_invalidated(self, db: Session, job: BackgroundJob, reason: str) -> None:
        self._commit_terminal(db, job, "cancelled", error=f"Cancelled: {reason}")
