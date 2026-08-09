"""全项目分类导出（批次 6）：补生成缺失片段 → 按分组/类别组织 → ZIP + annotations.json。

设计（与 README / 需求文档 §10.6 一致）：
- **入队排他 + 保留历史**：项目存在 queued/running 导出时拒绝新任务；每次成功入队都
  新建 `BackgroundJob`，使用唯一 dedupe key，历史结果与过期信息不会被重跑覆盖。
- **补生成**：只对「标注 review_status=approved 且视频当前 workflow_status=approved」的
  标注生效；当前修订（`Clip.source_revision == video.annotation_revision`）Clip 缺失或
  非 ready 时，直接复用媒体渲染管线（`render_clip_files`，临时文件 → 原子替换）同步补生成，
  不要求额外媒体队列；任一补生成失败 → 任务 failed（绝不产出不完整 ZIP），已成功片段保留。
- **打包**：按 `分组/类别` 建立临时目录，已 ready 的 Clip 以硬链接优先、复制回退放入，
  片段文件名 `{视频名前缀}_{标注者}_{起}s-{止}s_{类别}.mp4`（非法字符清洗 + 冲突去重）；
  同时写出需求文档 §2.3 事件格式的 `annotations.json`；`shutil.make_archive` 生成 ZIP
  并原子替换到 exports_dir，成功后写 `result_path`（相对名）与 `expires_at`（7 天保留）。
- **执行器**：轻量单线程 runner，与 MediaWorker 同构（原子领取 / 同步或后台调度 / 启动
  恢复）；测试经 `MEDIA_SYNCHRONOUS=true` 同步驱动，不要求本机 ffmpeg。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .media import MediaCommandError, format_time
from .media_jobs import (
    _truncate_error,
    claim_and_render_clip,
    clip_entities_ready,
    reset_interrupted_job_clips,
    resolve_entity_path,
)
from .models import Annotation, BackgroundJob, BehaviorCategory, Clip, DetectionImport, Project, Video
from .routers.detection_imports import generate_corrected_tracks

logger = logging.getLogger(__name__)

JOB_TYPE_EXPORT = "export"
APPROVED = "approved"
# 片段文件名中禁止的字符（Windows / 跨平台解压安全）
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _now() -> datetime:
    return datetime.utcnow()


def export_dedupe_key(project_id: int) -> str:
    """Fixed key used as the database-enforced active-export mutex."""
    return f"export:project:{project_id}:active"


def _release_export_key(job: BackgroundJob) -> None:
    job.dedupe_key = f"export:project:{job.project_id}:history:{job.id}"


def latest_export_job(db: Session, project_id: int) -> BackgroundJob | None:
    return (
        db.query(BackgroundJob)
        .filter(BackgroundJob.job_type == JOB_TYPE_EXPORT, BackgroundJob.project_id == project_id)
        .order_by(BackgroundJob.id.desc())
        .first()
    )


def enqueue_export_job(
    db: Session, project: Project, category_ids: list[int] | None
) -> BackgroundJob | None:
    """Atomically create the sole active export and snapshot its approved input set."""
    rows = approved_rows(db, project.id, category_ids)
    annotation_ids = [annotation.id for annotation, _video, _clip in rows]
    video_revisions = {
        str(video.id): video.annotation_revision for _annotation, video, _clip in rows
    }
    result = db.execute(
        sqlite_insert(BackgroundJob)
        .values(
            project_id=project.id,
            job_type=JOB_TYPE_EXPORT,
            status="queued",
            progress=0,
            dedupe_key=export_dedupe_key(project.id),
            payload={
                "project_id": project.id,
                "category_ids": category_ids or [],
                "annotation_ids": annotation_ids,
                "video_revisions": video_revisions,
            },
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
    )
    db.commit()
    if result.rowcount != 1:
        return None
    job = db.query(BackgroundJob).filter(
        BackgroundJob.dedupe_key == export_dedupe_key(project.id)
    ).one()
    db.refresh(job)
    return job


def approved_rows(
    db: Session,
    project_id: int,
    category_ids: list[int] | None,
    annotation_ids: list[int] | None = None,
) -> list[tuple[Annotation, Video, Clip | None]]:
    """项目内「标注 approved 且视频当前 approved」的标注 + 当前修订 Clip（外连接）。

    与批次 5 片段库入库条件一致：失效回 draft 后残留的 approved 标注一并排除。
    """
    conds = [
        Annotation.review_status == APPROVED,
        Annotation.mouse_id_status == "valid",
        Video.workflow_status == APPROVED,
        Video.project_id == project_id,
        BehaviorCategory.project_id == project_id,
    ]
    if category_ids:
        conds.append(Annotation.category_id.in_(category_ids))
    if annotation_ids is not None:
        conds.append(Annotation.id.in_(annotation_ids))
    return (
        db.query(Annotation, Video, Clip)
        .join(Video, Video.id == Annotation.video_id)
        .join(BehaviorCategory, BehaviorCategory.id == Annotation.category_id)
        .outerjoin(
            Clip,
            and_(
                Clip.annotation_id == Annotation.id,
                Clip.source_revision == Video.media_revision,
            ),
        )
        .filter(*conds)
        .order_by(Annotation.start_time, Annotation.id)
        .all()
    )


def _resolve_within(stored: str | None, root_dir: Path) -> tuple[Path | None, str | None]:
    """解析 Clip/导出文件路径并校验边界；越界返回 (None, "out-of-bounds")。"""
    if not stored:
        return None, "missing"
    root = root_dir.resolve()
    raw = Path(stored)
    path = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    if path == root or not path.is_relative_to(root):
        return None, "out-of-bounds"
    return path, None


def _sanitize_part(value: str) -> str:
    """文件名部件清洗：去非法字符 + 首尾空白/点，空串回退占位。"""
    cleaned = _INVALID_FILENAME_CHARS.sub("_", value).strip().strip(" .")
    return cleaned or "untitled"


def _clip_export_name(annotation: Annotation, used_names: set[str]) -> str:
    """ZIP 内片段文件名：`{视频名前缀}_{标注者}_{起}s-{止}s_{类别}.mp4`（冲突追加后缀）。"""
    video_prefix = _sanitize_part(Path(annotation.video.filename).stem) if annotation.video else "video"
    annotator = _sanitize_part(annotation.annotator.username) if annotation.annotator else "unknown"
    category = _sanitize_part(annotation.category.name) if annotation.category else "category"
    start = format_time(annotation.start_time)
    end = format_time(annotation.end_time)
    name = f"{video_prefix}_{annotator}_{start}s-{end}s_{category}.mp4"
    if name in used_names:
        name = f"{video_prefix}_{annotator}_{start}s-{end}s_{category}_a{annotation.id}.mp4"
    base = name[: -len(".mp4")]
    counter = 2
    while name in used_names:
        name = f"{base}_{counter}.mp4"
        counter += 1
    used_names.add(name)
    return name


def _event_record(annotation: Annotation, clip_file: str | None = None) -> dict:
    """需求文档 §12 统一事件格式（含 clip_file / mouse_ids / revisions）。"""
    record = {
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
    return record


class ExportWorker:
    """轻量导出 runner（批次 6）：单线程串行执行导出任务。

    与 MediaWorker 同构：原子领取（queued→running）、同步/后台调度、启动恢复；
    补生成缺失 Clip 直接复用同一渲染管线，不要求额外媒体队列。
    """

    def __init__(self, processor, session_factory, settings) -> None:
        self.processor = processor
        self.session_factory = session_factory
        self.settings = settings
        self.synchronous = settings.media_synchronous
        self._executor: ThreadPoolExecutor | None = None
        if not self.synchronous:
            self._executor = ThreadPoolExecutor(max_workers=1)
        self._futures: set = set()
        self.before_publish_hook = None

    # ---------- 生命周期 ----------

    def start(self, *, recover: bool = True) -> None:
        """启动恢复：running 视为中断（重排或判失败）；随后调度全部 queued 导出任务。"""
        if recover:
            self._recover_interrupted()
        with self.session_factory() as db:
            job_ids = [
                row[0]
                for row in db.query(BackgroundJob.id)
                .filter(
                    BackgroundJob.status == "queued",
                    BackgroundJob.job_type == JOB_TYPE_EXPORT,
                )
                .all()
            ]
        for job_id in job_ids:
            self.schedule(job_id)

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def schedule(self, job_id: int) -> None:
        """调度一个导出任务。同步模式：在当前线程内立即执行（测试确定性）。"""
        if self.synchronous:
            self._run_job(job_id)
            return
        future = self._executor.submit(self._run_job, job_id)
        self._futures.add(future)
        future.add_done_callback(self._futures.discard)

    def _recover_interrupted(self) -> None:
        max_attempts = self.settings.media_max_attempts
        with self.session_factory() as db:
            rows = (
                db.query(BackgroundJob)
                .filter(
                    BackgroundJob.status == "running",
                    BackgroundJob.job_type == JOB_TYPE_EXPORT,
                )
                .all()
            )
            for job in rows:
                if job.attempts >= max_attempts:
                    job.status = "failed"
                    job.error = (
                        f"Interrupted at startup after {job.attempts} attempts "
                        "(retry limit reached); re-submit the job to retry"
                    )
                    job.finished_at = _now()
                    _release_export_key(job)
                else:
                    job.status = "queued"
                    job.attempts += 1
                    job.started_at = None
                    job.error = "Interrupted; requeued at startup"
                    reset_interrupted_job_clips(db, job)
            db.commit()

    # ---------- 任务执行 ----------

    def _run_job(self, job_id: int) -> None:
        try:
            self._run_job_impl(job_id)
        except Exception:  # noqa: BLE001  worker 线程绝不外泄异常
            logger.exception("Export job %s crashed unexpectedly", job_id)
            try:
                with self.session_factory() as db:
                    job = db.get(BackgroundJob, job_id)
                    if job is not None and job.status == "running":
                        job.status = "failed"
                        job.error = "Export worker crashed unexpectedly (see server log)"
                        job.finished_at = _now()
                        _release_export_key(job)
                        db.commit()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to record crash for export job %s", job_id)

    def _run_job_impl(self, job_id: int) -> None:
        with self.session_factory() as db:
            job = db.get(BackgroundJob, job_id)
            if job is None:
                return
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

    def _fail(self, db: Session, job: BackgroundJob, message: str) -> None:
        job.status = "failed"
        job.error = _truncate_error(message)
        job.finished_at = _now()
        _release_export_key(job)
        db.commit()

    # ---------- 单任务处理 ----------

    def _process_job(self, db: Session, job: BackgroundJob) -> None:
        payload = job.payload or {}
        project_id = job.project_id
        category_ids = payload.get("category_ids") or []
        if project_id is None or payload.get("project_id") != project_id:
            self._fail(db, job, "Export job payload project_id does not match job project_id")
            return
        project = db.get(Project, project_id)
        if project is None:
            self._fail(db, job, "Project no longer exists")
            return
        try:
            self._build_export(db, job, project, list(category_ids))
        except Exception as exc:  # noqa: BLE001  单任务失败 → 任务 failed，绝不产出不完整 ZIP
            db.rollback()
            self._fail(db, job, f"Export failed: {exc}")
            return

    def _build_export(
        self, db: Session, job: BackgroundJob, project: Project, category_ids: list[int]
    ) -> None:
        """Build a candidate archive, then validate and publish it under the DB write lock."""
        settings = self.settings
        payload = job.payload or {}
        annotation_ids = list(payload.get("annotation_ids") or [])
        video_revisions = {
            int(video_id): revision
            for video_id, revision in (payload.get("video_revisions") or {}).items()
        }
        self._validate_categories(db, project.id, category_ids)
        rows = approved_rows(db, project.id, category_ids, annotation_ids)
        if {annotation.id for annotation, _video, _clip in rows} != set(annotation_ids):
            raise MediaCommandError("Export input approval changed after enqueue")
        total = len(rows)
        ready_items: list[tuple[Annotation, Path]] = []
        failures: list[str] = []

        for annotation, video, clip in rows:
            self._validate_item(
                db, project.id, annotation.id, video.id, video_revisions, require_ready=False
            )
            path = None
            if clip is not None and clip_entities_ready(clip, settings):
                path = resolve_entity_path(clip.clip_path, settings.clips_dir)
            if path is None:
                try:
                    path = self._fill_clip(db, annotation, video, clip)
                except Exception as exc:  # noqa: BLE001  单片段补生成失败 → 记录，任务失败
                    db.rollback()
                    failures.append(
                        f"annotation {annotation.id} ({_event_record(annotation)['behavior']}): {exc}"
                    )
                    continue
            self._validate_item(
                db, project.id, annotation.id, video.id, video_revisions, require_ready=True
            )
            if path is not None:
                ready_items.append((annotation, path))
            # 进度：补生成阶段 5..90（按已就绪占比）
            job = db.get(BackgroundJob, job.id)
            if job is not None:
                done = len(ready_items) + len(failures)
                job.progress = int(done * 90 / total) if total else 90
                db.commit()

        if failures:
            raise MediaCommandError(
                f"Clip fill failed for {len(failures)} annotation(s): "
                + "; ".join(failures[:3])
            )

        self._package(
            db, job, project, ready_items, settings, category_ids, annotation_ids, video_revisions
        )

    def _fill_clip(self, db: Session, annotation: Annotation, video: Video, clip: Clip | None) -> Path:
        """补生成缺失/非 ready 的当前修订 Clip；成功返回实体文件路径。"""
        settings = self.settings
        if clip is None:
            clip = Clip(
                project_id=video.project_id,
                annotation_id=annotation.id,
                source_revision=video.media_revision,
                media_revision=video.media_revision,
                status="pending",
            )
            db.add(clip)
            db.commit()
            db.refresh(clip)
        elif clip.status == "ready" and not clip_entities_ready(clip, settings):
            db.query(Clip).filter(Clip.id == clip.id, Clip.status == "ready").update(
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
        path, _created = claim_and_render_clip(
            db, self.processor, settings, video.id, annotation.id, clip.id
        )
        return path

    def _validate_categories(self, db: Session, project_id: int, category_ids: list[int]) -> None:
        if not category_ids:
            return
        found = {
            row[0]
            for row in db.query(BehaviorCategory.id)
            .filter(
                BehaviorCategory.project_id == project_id,
                BehaviorCategory.id.in_(category_ids),
            )
            .all()
        }
        if found != set(category_ids):
            raise MediaCommandError("Export category does not belong to job project")

    def _validate_item(
        self,
        db: Session,
        project_id: int,
        annotation_id: int,
        video_id: int,
        video_revisions: dict[int, int],
        *,
        require_ready: bool,
    ) -> None:
        db.expire_all()
        row = (
            db.query(Annotation, Video, BehaviorCategory, Clip)
            .join(Video, Video.id == Annotation.video_id)
            .join(BehaviorCategory, BehaviorCategory.id == Annotation.category_id)
            .outerjoin(
                Clip,
                and_(
                    Clip.annotation_id == Annotation.id,
                    Clip.source_revision == Video.media_revision,
                ),
            )
            .filter(Annotation.id == annotation_id, Video.id == video_id)
            .first()
        )
        expected_revision = video_revisions.get(video_id)
        if row is None:
            raise MediaCommandError(f"annotation {annotation_id} no longer exists")
        annotation, video, category, clip = row
        if (
            annotation.review_status != APPROVED
            or video.workflow_status != APPROVED
            or expected_revision is None
            or video.annotation_revision != expected_revision
            or video.project_id != project_id
            or category.project_id != project_id
        ):
            raise MediaCommandError(f"annotation {annotation_id} approval or revision changed")
        if require_ready and (clip is None or not clip_entities_ready(clip, self.settings)):
            raise MediaCommandError(f"annotation {annotation_id} current clip is not ready")

    def _validate_snapshot(
        self,
        db: Session,
        project_id: int,
        category_ids: list[int],
        annotation_ids: list[int],
        video_revisions: dict[int, int],
    ) -> None:
        self._validate_categories(db, project_id, category_ids)
        for annotation_id in annotation_ids:
            annotation = db.get(Annotation, annotation_id)
            if annotation is None:
                raise MediaCommandError(f"annotation {annotation_id} no longer exists")
            self._validate_item(
                db,
                project_id,
                annotation_id,
                annotation.video_id,
                video_revisions,
                require_ready=True,
            )

    @staticmethod
    def _validate_zip_integrity(zip_path: Path, events: list[dict], mp4_paths: list[str]) -> None:
        """验证 ZIP 内 annotations.json 与 MP4 文件双向一一对应。

        失败则抛出 MediaCommandError，不发布 ZIP。
        """
        with zipfile.ZipFile(zip_path, "r") as zf:
            all_names = zf.namelist()
            mp4_names = sorted(
                name for name in all_names
                if name.startswith("clips/") and name.endswith(".mp4")
            )
            clip_files = sorted(
                event.get("clip_file") or "" for event in events
            )

        if len(events) != len(mp4_names):
            raise MediaCommandError(
                f"ZIP integrity: {len(events)} event records but {len(mp4_names)} MP4 files"
            )

        if clip_files != mp4_names:
            raise MediaCommandError(
                f"ZIP integrity: clip_file entries do not match MP4 paths in archive"
            )

        if len(mp4_paths) != len(mp4_names):
            raise MediaCommandError(
                f"ZIP integrity: {len(mp4_paths)} tracked clip paths but {len(mp4_names)} MP4 files"
            )

        if set(mp4_paths) != set(mp4_names):
            raise MediaCommandError(
                "ZIP integrity: tracked clip paths do not match actual archive paths"
            )

        for cf in clip_files:
            if not cf:
                raise MediaCommandError("ZIP integrity: event record has empty clip_file")
            if ".." in cf:
                raise MediaCommandError(f"ZIP integrity: clip_file contains '..': {cf}")
            if cf.startswith("/") or "\\" in cf:
                raise MediaCommandError(
                    f"ZIP integrity: clip_file is absolute or contains backslash: {cf}"
                )

    def _package(
        self,
        db: Session,
        job: BackgroundJob,
        project: Project,
        ready_items: list[tuple[Annotation, Path]],
        settings,
        category_ids: list[int],
        annotation_ids: list[int],
        video_revisions: dict[int, int],
    ) -> None:
        """按 分组/类别 组织临时目录 + 复制/硬链接 Clip + annotations.json → ZIP。"""
        staging = settings.exports_dir / f".export_{project.id}_{job.id}.staging"
        temp_zip = settings.exports_dir / f".export_{project.id}_{job.id}.tmp.zip"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        used_names: set[str] = set()
        events: list[dict] = []
        mp4_paths: list[str] = []
        zip_src: Path | None = None
        try:
            for annotation, clip_path in ready_items:
                group = _sanitize_part(annotation.category.group) if annotation.category else "未分类"
                category = (
                    _sanitize_part(annotation.category.name) if annotation.category else "未分类"
                )
                target_dir = staging / "clips" / group / category
                target_dir.mkdir(parents=True, exist_ok=True)
                clip_name = _clip_export_name(annotation, used_names)
                target = target_dir / clip_name
                try:
                    os.link(clip_path, target)
                except OSError:
                    shutil.copy2(clip_path, target)
                clip_file = f"clips/{group}/{category}/{clip_name}"
                mp4_paths.append(clip_file)
                events.append(_event_record(annotation, clip_file=clip_file))
            events.sort(key=lambda e: (e["start_time"], e["video_id"]))
            with (staging / "annotations.json").open("w", encoding="utf-8") as fh:
                json.dump(events, fh, ensure_ascii=False, indent=2)

            video_groups: dict[int, set[tuple[int, int]]] = {}
            for ann, _clip_path in ready_items:
                di_rev = ann.detection_import_revision
                id_rev = ann.identity_revision
                video_groups.setdefault(ann.video_id, set()).add((di_rev, id_rev))

            ct_manifest_entries: list[dict] = []
            ct_staging = staging / "corrected_tracks"
            ct_staging.mkdir(parents=True, exist_ok=True)
            for vid, rev_pairs in video_groups.items():
                video = db.get(Video, vid)
                if video is None:
                    continue
                imp = (
                    db.query(DetectionImport)
                    .filter(DetectionImport.video_id == vid, DetectionImport.active == True)
                    .first()
                )
                if imp is None:
                    continue
                for di_rev, id_rev in rev_pairs:
                    result = generate_corrected_tracks(
                        db, video, imp, id_rev, self.settings.detection_imports_dir
                    )
                    if result is None or not result.get("tracks_corrected"):
                        continue
                    vid_dir = ct_staging / f"video_{vid}" / f"import_{di_rev}" / f"identity_{id_rev}"
                    vid_dir.mkdir(parents=True, exist_ok=True)
                    tracks_path = vid_dir / "tracks.corrected.jsonl"
                    tracks_path.write_text(result["tracks_corrected_text"], encoding="utf-8")
                    manifest_entry = result["manifest"]
                    manifest_entry["file_paths"] = [
                        f"corrected_tracks/video_{vid}/import_{di_rev}/identity_{id_rev}/tracks.corrected.jsonl",
                    ]
                    ct_manifest_entries.append(manifest_entry)

            if ct_manifest_entries:
                with (ct_staging / "manifest.json").open("w", encoding="utf-8") as fh:
                    json.dump(ct_manifest_entries, fh, ensure_ascii=False, indent=2)

            shutil.make_archive(str(staging), "zip", root_dir=str(staging))
            zip_src = Path(str(staging) + ".zip")
            zip_src.rename(temp_zip)

            self._validate_zip_integrity(temp_zip, events, mp4_paths)

            self._validate_snapshot(
                db, project.id, category_ids, annotation_ids, video_revisions
            )
            if self.before_publish_hook is not None:
                self.before_publish_hook()

            zip_name = f"export_project_{project.id}_{job.id}.zip"
            final_zip = settings.exports_dir / zip_name

            db.rollback()
            db.execute(text("BEGIN IMMEDIATE"))
            try:
                self._validate_snapshot(
                    db, project.id, category_ids, annotation_ids, video_revisions
                )
                current_job = db.get(BackgroundJob, job.id)
                if current_job is None or current_job.status != "running":
                    raise MediaCommandError("Export job is no longer running")
                os.replace(temp_zip, final_zip)
                current_job.status = "succeeded"
                current_job.progress = 100
                current_job.result_path = zip_name
                current_job.finished_at = _now()
                current_job.expires_at = _now() + timedelta(
                    days=settings.export_retention_days
                )
                _release_export_key(current_job)
                db.commit()
            except Exception:
                db.rollback()
                final_zip.unlink(missing_ok=True)
                raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            temp_zip.unlink(missing_ok=True)
            if zip_src is not None:
                zip_src.unlink(missing_ok=True)
