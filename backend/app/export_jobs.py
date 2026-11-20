"""Atomic project ZIP export from published submission-bound Clip assets."""
from __future__ import annotations

import json
import errno
import hashlib
import logging
import os
import shutil
import secrets
import uuid
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .effective_detections import effective_detection_query
from .export_contract import (FILES, safe_part, transform_detection, validate_clip_directory,
                              validate_track_frame)
from .media import MediaCommandError
from .media_jobs import (WorkerCleanupIncomplete, _cleanup_incomplete_error,
                         _cleanup_paths, _is_cleanup_incomplete,
                         _truncate_error,
                         claim_and_render_submission_clip, clip_entities_ready,
                         reset_interrupted_job_clips, resolve_entity_path, stage_submission_input)
from .models import (BackgroundJob, Clip, DetectionSnapshotState, Project, RawDetection,
                     Submission, SubmissionAnnotation, Video)
from .submission_media_plan import build_submission_media_plan
from .submission_service import validate_snapshot_integrity
from .heuristic_judgment import published_source_condition
from . import context_cache as cache

logger = logging.getLogger(__name__)
JOB_TYPE_EXPORT = "export"


def _now() -> datetime:
    return datetime.utcnow()


def export_dedupe_key(project_id: int) -> str:
    return f"export:project:{project_id}:active"


def _release_export_key(job: BackgroundJob) -> None:
    job.dedupe_key = f"export:project:{job.project_id}:history:{job.id}"


def latest_export_job(db: Session, project_id: int, source="manual", *, succeeded=False) -> BackgroundJob | None:
    query = db.query(BackgroundJob).filter_by(job_type=JOB_TYPE_EXPORT, project_id=project_id)
    if succeeded:
        query = query.filter_by(status="succeeded")
    return next((job for job in query.order_by(BackgroundJob.id.desc())
                 if (job.payload or {}).get("source", "manual") == source), None)


def asset_rows(db: Session, project_id: int, category_ids: list[int] | None,
               submission_annotation_ids: list[int] | None = None, *, source="manual"):
    """Read published submission-bound Clip assets (status API/enqueue only).

    Authority is the immutable ``Clip`` row bound to its ``SubmissionAnnotation``;
    ``Submission.status`` is intentionally not filtered, so the approved subset of
    a terminal ``rejected``/``superseded`` attempt remains exportable.
    """
    query = (db.query(SubmissionAnnotation, Submission, Clip)
             .join(Submission, Submission.id == SubmissionAnnotation.submission_id)
             .join(Clip, Clip.submission_annotation_id == SubmissionAnnotation.id)
             .join(Video, Video.id == Submission.video_id)
             .filter(Video.project_id == project_id, Video.workflow_status != "archived",
                     Clip.retired_at.is_(None)))
    query = query.filter(published_source_condition(source))
    if category_ids:
        query = query.filter(SubmissionAnnotation.category_id.in_(category_ids))
    if submission_annotation_ids is not None:
        query = query.filter(SubmissionAnnotation.id.in_(submission_annotation_ids))
    return query.order_by(SubmissionAnnotation.start_frame, SubmissionAnnotation.id).all()


def enqueue_export_job(db: Session, project: Project, category_ids: list[int] | None, *, source="manual") -> BackgroundJob | None:
    requested = sorted(set(category_ids or []))
    rows = asset_rows(db, project.id, requested or None, source=source)
    # Empty means all concrete categories actually represented by published assets now.
    represented = {annotation.category_id for annotation, _submission, _clip in rows}
    frozen_categories = requested if requested else sorted(represented)
    rows = [row for row in rows if row[0].category_id in represented]
    if not rows:
        raise ValueError("No published clip assets are eligible for export")
    used_categories: set[str] = set()
    category_directories = {}
    category_tokens = {str(category_id): secrets.token_hex(16) for category_id in frozen_categories}
    snapshot_names = {}
    for annotation, _submission, _clip in rows:
        snapshot_names.setdefault(annotation.category_id, annotation.category_name)
    for category_id in frozen_categories:
        base = safe_part(snapshot_names.get(category_id), fallback="category", limit=80)
        name = base; suffix = 0; token = category_tokens[str(category_id)]
        while name.casefold() in used_categories:
            suffix += 1
            tail = token[:12] if suffix == 1 else f"{token[:12]}_{suffix}"
            # Reserve the suffix outside the colliding sanitizer result.  Calling the
            # same sanitizer with the same final limit can otherwise never make progress
            # for distinct names that normalize to one portable component.
            stem = safe_part(base, fallback="category", limit=80 - len(tail) - 1)
            name = f"{stem}_{tail}"
        used_categories.add(name.casefold()); category_directories[str(category_id)] = name
    submission_ids = sorted({submission.id for _annotation, submission, _clip in rows})
    annotation_ids = [annotation.id for annotation, _submission, _clip in rows]
    batch = cache.context_batch(db, rows)
    refs = [{"submission_id": submission.id, "submission_annotation_id": annotation.id,
             "snapshot_id": submission.detection_snapshot_id,
             "source_media_revision": submission.source_media_revision,
             "source_sha256": submission.source_video_sha256,
             "source_file_size": submission.source_file_size, "source_mtime_ns": submission.source_mtime_ns,
             "source_device": submission.source_device, "source_inode": submission.source_inode,
             "raw_digest": submission.detection_snapshot.raw_digest,
             "state_digest": submission.detection_snapshot.state_digest,
             "metadata_digest": submission.detection_snapshot.metadata_digest,
             "opaque_token": secrets.token_hex(16),
             "context": cache.frozen_context(db, annotation, submission, clip, batch),
             "distance_scale": cache.distance_scale(submission)}
            for annotation, submission, clip in rows]
    result = db.execute(sqlite_insert(BackgroundJob).values(
        project_id=project.id, job_type=JOB_TYPE_EXPORT, status="queued", progress=0,
        dedupe_key=export_dedupe_key(project.id), payload={
            "contract_version": 1, "project_id": project.id, "category_ids": frozen_categories,
            "source": source,
            "preparation_version": 1,
            "category_directories": category_directories, "category_tokens": category_tokens,
            "submission_ids": submission_ids, "submission_annotation_ids": annotation_ids,
            "refs": refs,
        }).on_conflict_do_nothing(index_elements=["dedupe_key"]))
    db.commit()
    if result.rowcount != 1:
        return None
    return db.query(BackgroundJob).filter_by(dedupe_key=export_dedupe_key(project.id)).one()


class ExportScheduleError(RuntimeError):
    """The newly-created export could not be handed to its worker."""


def schedule_export_job(db: Session, worker, job: BackgroundJob) -> None:
    """Schedule one created job, atomically failing it if handoff itself raises.

    The guarded update prevents a late scheduling exception from changing a job
    that another worker has already claimed, or from releasing another export's
    active dedupe key.
    """
    job_id, project_id = job.id, job.project_id
    active_key = export_dedupe_key(project_id)
    try:
        worker.schedule(job_id)
    except Exception as exc:
        db.rollback()
        failed = db.query(BackgroundJob).filter(
            BackgroundJob.id == job_id,
            BackgroundJob.project_id == project_id,
            BackgroundJob.job_type == JOB_TYPE_EXPORT,
            BackgroundJob.status == "queued",
            BackgroundJob.dedupe_key == active_key,
        ).update({
            "status": "failed",
            "error": _truncate_error(f"Export scheduling failed: {exc}"),
            "finished_at": _now(),
            "dedupe_key": f"export:project:{project_id}:history:{job_id}",
        }, synchronize_session=False)
        db.commit()
        if failed == 1:
            logger.exception("Scheduling export job %s failed; queued job marked failed", job_id)
        else:
            logger.exception("Scheduling export job %s failed after it left queued state", job_id)
        raise ExportScheduleError("Export worker is temporarily unavailable") from exc


def _resolve_within(stored: str | None, root_dir: Path):
    if not stored:
        return None, "missing"
    root, raw = root_dir.resolve(), Path(stored)
    path = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    if path == root or not path.is_relative_to(root):
        return None, "out-of-bounds"
    return path, None


def clip_directory_name(annotation: SubmissionAnnotation, submission: Submission) -> str:
    category = safe_part(annotation.category_name, fallback="category", limit=40)
    video = safe_part(Path(submission.source_video_filename).stem, fallback="video", limit=40)
    return safe_part(f"{category}_{video}_{annotation.start_frame:06d}-{annotation.end_frame:06d}", limit=120)


class ExportWorker:
    def __init__(self, processor, session_factory, settings) -> None:
        self.processor, self.session_factory, self.settings = processor, session_factory, settings
        self.synchronous = settings.media_synchronous
        self._executor = None if self.synchronous else ThreadPoolExecutor(max_workers=1)
        self._futures = set()
        self._drain_lock = threading.RLock()
        self._draining = False
        self._stopping = threading.Event()
        # Owned exclusively by the drain, never by the first (possibly terminal) job.
        self._prewarm_source = None
        self._prewarm_source_identity = None
        self.before_publish_hook = None
        self.before_terminal_hook = None

    def start(self, *, recover: bool = True) -> None:
        if recover:
            self._recover_interrupted()
        cache.evict_cache(self.settings)
        self.wake()

    def shutdown(self) -> None:
        self._stopping.set()
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def schedule(self, job_id: int) -> None:
        self.wake()

    def wake(self) -> None:
        # One drain future, not one future per queued row. Re-query priority after
        # every context entry so a bulk prewarm can never FIFO-block an export.
        with self._drain_lock:
            if self._draining:
                return
            self._draining = True
            try:
                if self.synchronous:
                    self._drain()
                else:
                    future = self._executor.submit(self._drain)
                    self._futures.add(future)
                    future.add_done_callback(self._futures.discard)
            except BaseException:
                self._draining = False
                raise

    def _drain(self):
        retry_delay = 0.05
        released = False
        try:
            while not self._stopping.is_set():
                # Scheduling and the final empty check share a lock, closing the
                # commit/wakeup race without keeping a transaction while rendering.
                with self._drain_lock:
                    with self.session_factory() as db:
                        job = db.query(BackgroundJob).filter(
                            BackgroundJob.status == "queued",
                            BackgroundJob.job_type.in_([JOB_TYPE_EXPORT, cache.JOB_TYPE_PREWARM])
                        ).order_by((BackgroundJob.job_type == JOB_TYPE_EXPORT).desc(), BackgroundJob.id).first()
                        job_id = job.id if job else None
                        if job is None or job.job_type == JOB_TYPE_EXPORT:
                            self._release_prewarm_source()
                    if job_id is None:
                        self._release_prewarm_source()
                        self._draining = False
                        released = True
                        return
                if self._run_job(job_id) is False:
                    if self.synchronous:
                        with self._drain_lock:
                            self._release_prewarm_source()
                            self._draining = False
                            released = True
                        return
                    # Keep this drain alive: schedules during a failed claim are
                    # consumed on its next query, with capped, interruptible backoff.
                    self._stopping.wait(retry_delay)
                    retry_delay = min(2.0, retry_delay * 2)
                else:
                    retry_delay = 0.05
        finally:
            if not released:
                with self._drain_lock:
                    try:
                        self._release_prewarm_source()
                    finally:
                        self._draining = False

    def _release_prewarm_source(self):
        _cleanup_paths([self._prewarm_source], operation="remove drain prewarm source")
        self._prewarm_source = self._prewarm_source_identity = None

    def _stage_prewarm_source(self, submission, job_id):
        identity = cache.source_group_identity(submission)
        if identity != self._prewarm_source_identity:
            self._release_prewarm_source()
        if self._prewarm_source is None:
            self._prewarm_source = stage_submission_input(self.settings, submission, job_id,
                reserve_check=lambda path, amount: cache.check_space(self.settings, path, amount))
            self._prewarm_source_identity = identity
        else:
            # Each reference must still name the very same immutable source FD.
            # The private copy's SHA was checked on admission; do not rehash it.
            from .file_identity import FileIdentity, stream_identity
            from .submission_service import validate_storage_key
            root = self.settings.videos_dir.resolve()
            path = (root / validate_storage_key(submission.source_storage_key)).resolve()
            if path == root or not path.is_relative_to(root):
                raise MediaCommandError("immutable submission source is outside videos directory")
            expected = FileIdentity(submission.source_file_size, submission.source_mtime_ns,
                                    submission.source_device, submission.source_inode)
            with path.open("rb") as stream:
                if stream_identity(stream) != expected:
                    raise MediaCommandError("immutable submission source file identity mismatch")
        return self._prewarm_source

    def _recover_interrupted(self) -> None:
        with self.session_factory() as db:
            for job in db.query(BackgroundJob).filter(BackgroundJob.status == "running",
                    BackgroundJob.job_type.in_([JOB_TYPE_EXPORT, cache.JOB_TYPE_PREWARM])):
                if _is_cleanup_incomplete(job):
                    continue
                if job.attempts >= self.settings.media_max_attempts:
                    job.status, job.error, job.finished_at = "failed", "Interrupted; retry limit reached", _now()
                    if job.job_type == JOB_TYPE_EXPORT:
                        _release_export_key(job)
                        reset_interrupted_job_clips(db, job)
                else:
                    job.status, job.started_at, job.error = "queued", None, "Interrupted; requeued"
                    job.progress = 0
                    if job.job_type == JOB_TYPE_EXPORT:
                        reset_interrupted_job_clips(db, job)
            db.commit()

    def _run_job(self, job_id: int) -> None:
        try:
            with self.session_factory() as db:
                claimed = db.query(BackgroundJob).filter(BackgroundJob.id == job_id,
                    BackgroundJob.status == "queued", BackgroundJob.job_type.in_(
                        [JOB_TYPE_EXPORT, cache.JOB_TYPE_PREWARM])).update(
                    {"status": "running", "started_at": _now(), "error": None,
                     "attempts": BackgroundJob.attempts + 1}, synchronize_session=False)
                db.commit()
                if not claimed:
                    return
                job = db.get(BackgroundJob, job_id)
                try:
                    cache.cleanup_job_scratch(self.settings, job_id)
                    _cleanup_paths(self.settings.videos_dir.glob(f".submission-media-job-{job_id}-*.staging"),
                                   operation="remove interrupted source staging")
                    if job.job_type == cache.JOB_TYPE_PREWARM:
                        self._prewarm(db, job)
                    else:
                        self._build(db, job)
                except WorkerCleanupIncomplete as exc:
                    db.rollback()
                    self._record_cleanup_incomplete(job_id, exc)
                except Exception as exc:
                    self._release_prewarm_source()
                    logger.exception("Export job %s failed: %s", job_id, exc)
                    db.rollback(); job = db.get(BackgroundJob, job_id)
                    if job and job.status == "running":
                        if self.before_terminal_hook:
                            self.before_terminal_hook()
                        prefix = "storage_insufficient: " if isinstance(exc, OSError) and exc.errno == errno.ENOSPC else "Export failed: "
                        job.status, job.error, job.finished_at = "failed", _truncate_error(f"{prefix}{exc}"), _now()
                        if job.job_type == JOB_TYPE_EXPORT:
                            _release_export_key(job)
                        db.commit()
        except Exception:
            logger.exception("Export job %s crashed", job_id)
            return False

    def _record_cleanup_incomplete(self, job_id: int, exc: BaseException) -> None:
        try:
            with self.session_factory() as db:
                job = db.get(BackgroundJob, job_id)
                if job is not None and job.status == "running":
                    job.error = _cleanup_incomplete_error(exc)
                    if getattr(exc, "result_path", None):
                        job.result_path = exc.result_path
                    job.finished_at = None
                    db.commit()
        except Exception:
            logger.exception("Failed to record incomplete cleanup for export job %s", job_id)

    @staticmethod
    def _cleanup_artifacts(staging: Path, temp_zip: Path, sources, *, final: Path | None = None) -> None:
        errors = []
        if staging.exists():
            try:
                shutil.rmtree(staging)
            except OSError as exc:
                errors.append(f"remove export staging {staging}: {exc}")
        try:
            _cleanup_paths([temp_zip, *sources, final], operation="remove export artifact")
        except WorkerCleanupIncomplete as exc:
            errors.append(str(exc))
        if errors:
            raise WorkerCleanupIncomplete("; ".join(errors))

    def _frozen_rows(self, db: Session, job: BackgroundJob, *, require_ready: bool):
        payload = job.payload or {}
        source_scope = payload.get("source", "manual")
        if source_scope not in {"manual", "heuristic", "all"}:
            raise MediaCommandError("invalid export source")
        if payload.get("contract_version") != 1 or payload.get("project_id") != job.project_id:
            raise MediaCommandError("invalid export payload")
        ids = payload.get("submission_annotation_ids")
        refs = payload.get("refs")
        if not isinstance(ids, list) or ids != list(dict.fromkeys(ids)) or len(refs or []) != len(ids):
            raise MediaCommandError("invalid immutable export reference set")
        # Inner join the published asset: a snapshot without its own Clip is not a
        # published fact and must fail rather than be invented here. Submission
        # status is not re-gated; the frozen immutable reference set is authoritative.
        rows = (db.query(SubmissionAnnotation, Submission, Clip)
                .join(Submission, Submission.id == SubmissionAnnotation.submission_id)
                .join(Clip, Clip.submission_annotation_id == SubmissionAnnotation.id)
                .join(Video, Video.id == Submission.video_id)
                .filter(Video.project_id == job.project_id, Video.workflow_status != "archived",
                        SubmissionAnnotation.id.in_(ids),
                        Clip.retired_at.is_(None))
                .order_by(SubmissionAnnotation.id).all()) if ids else []
        by_id = {annotation.id: (annotation, submission, clip) for annotation, submission, clip in rows}
        if set(by_id) != set(ids):
            raise MediaCommandError("frozen SubmissionAnnotation no longer exists")
        allowed = {a.id for a, _s, _c in asset_rows(db, job.project_id, payload.get("category_ids"), ids, source=source_scope)}
        if allowed != set(ids):
            raise MediaCommandError("export source or final authority changed")
        refs_by_id = {ref.get("submission_annotation_id"): ref for ref in refs}
        batch = cache.context_batch(db, rows)
        for annotation_id in ids:
            annotation, submission, clip = by_id[annotation_id]; ref = refs_by_id.get(annotation_id, {})
            if (ref.get("submission_id") != submission.id or ref.get("snapshot_id") != submission.detection_snapshot_id
                    or ref.get("source_media_revision") != submission.source_media_revision
                    or ref.get("source_sha256") != submission.source_video_sha256
                    or ref.get("source_file_size") != submission.source_file_size
                    or ref.get("source_mtime_ns") != submission.source_mtime_ns
                    or ref.get("source_device") != submission.source_device
                    or ref.get("source_inode") != submission.source_inode
                    or ref.get("raw_digest") != submission.detection_snapshot.raw_digest
                    or ref.get("state_digest") != submission.detection_snapshot.state_digest
                    or ref.get("metadata_digest") != submission.detection_snapshot.metadata_digest
                    or not isinstance(ref.get("opaque_token"), str) or len(ref["opaque_token"]) < 24):
                raise MediaCommandError("immutable export reference mismatch")
            if require_ready and (clip is None or not clip_entities_ready(clip, self.settings)):
                raise MediaCommandError("frozen Submission Clip is not ready")
            if ref.get("context") is not None and ref["context"] != cache.frozen_context(db, annotation, submission, clip, batch):
                raise MediaCommandError("immutable context reference mismatch")
        return [by_id[item_id] for item_id in ids]

    def _ensure_clip(self, db, job, annotation, submission, clip, staged_source: Path) -> Path:
        # A published asset must already exist as a Clip row. Never invent one here:
        # doing so would fabricate a publication fact for a snapshot the publish
        # transaction did not release. Only retry/repair the existing asset entity.
        if clip is None:
            raise MediaCommandError("published asset Clip is missing; refusing to invent one")
        if not clip_entities_ready(clip, self.settings):
            snapshot = submission.detection_snapshot
            cache.check_space(self.settings, self.settings.clips_dir,
                max(1024 * 1024, (annotation.end_frame - annotation.start_frame + 1) *
                    snapshot.width * snapshot.height // 8))
            return claim_and_render_submission_clip(db, self.processor, self.settings, submission.id,
                                                    annotation.id, clip.id, input_path=staged_source)[0]
        path = resolve_entity_path(clip.clip_path, self.settings.clips_dir)
        if path is None or not path.is_file():
            raise MediaCommandError("ready Submission Clip path is unsafe or missing")
        return path

    def _write_tracks_range(self, db, annotation, submission, plan, path: Path,
                            start_frame: int, end_frame: int):
        snapshot = submission.detection_snapshot
        valid_ids = {int(row[0]) for row in db.query(func.coalesce(
            DetectionSnapshotState.display_track_id, RawDetection.raw_track_id))
            .select_from(RawDetection)
            .outerjoin(DetectionSnapshotState,
                       (DetectionSnapshotState.raw_detection_id == RawDetection.id) &
                       (DetectionSnapshotState.snapshot_id == snapshot.id))
            .filter(RawDetection.detection_import_id == snapshot.detection_import_id,
                    func.coalesce(DetectionSnapshotState.suppressed, False) == False).distinct()}
        query = effective_detection_query(db, snapshot.detection_import_id,
            start_frame=start_frame, end_frame=end_frame,
            snapshot_id=snapshot.id).order_by(RawDetection.frame_index, RawDetection.frame_detection_index,
                                               RawDetection.id).yield_per(500)
        iterator = iter(query); current = next(iterator, None)
        with path.open("w", encoding="utf-8") as fh:
            fh.write("[")
            for relative in range(end_frame - start_frame + 1):
                absolute = start_frame + relative; detections = []
                while current is not None and current[0].frame_index == absolute:
                    if submission.video.source == "heuristic":
                        raw = current[0]
                        item = dict(track_id=current.display_track_id,
                                    box=[raw.box[k] for k in ("x1", "y1", "x2", "y2")],
                                    keypoints=[[p[k] for k in ("x_px", "y_px", "confidence")] for p in raw.keypoints],
                                    detection_confidence=raw.detection_confidence, class_id=raw.class_id)
                    else:
                        item = transform_detection(current[0], current.display_track_id, plan.crop,
                                                   plan.output_width, plan.output_height)
                    if item is not None: detections.append(item)
                    current = next(iterator, None)
                if relative: fh.write(",")
                frame = {"frame": relative, "time": relative / snapshot.fps,
                         "detections": detections}
                validate_track_frame(frame, expected_frame=relative, fps=snapshot.fps,
                                     width=plan.output_width, height=plan.output_height)
                json.dump(frame, fh, ensure_ascii=False, separators=(",", ":"))
            fh.write("]")
        from .export_contract import TracksSummary
        return TracksSummary(end_frame - start_frame + 1, frozenset(valid_ids))

    @staticmethod
    def _export_range(db, annotation, submission) -> tuple[int, int]:
        return cache.context_range(db, annotation, submission)

    def _write_item(self, db, annotation, submission, staged_source: Path, target: Path, *, clip_path: Path | None = None,
                    context=None, distance=...) -> None:
        snapshot = submission.detection_snapshot
        video = db.get(Video, submission.video_id)
        heuristic = video.source == "heuristic"
        start_frame, end_frame = (context["range"] if context else
            ((0, snapshot.frame_count - 1) if heuristic else self._export_range(db, annotation, submission)))
        plan = build_submission_media_plan(start_time=start_frame / snapshot.fps,
            end_time=(end_frame + 1) / snapshot.fps,
            start_frame=start_frame, end_frame=end_frame, fps=snapshot.fps,
            frame_count=snapshot.frame_count, width=snapshot.width, height=snapshot.height,
            crop_region=annotation.crop_region)
        target.mkdir(parents=True)
        cache.check_space(self.settings, target, max(1024 * 1024, plan.frame_count * plan.output_width * plan.output_height // 8))
        temp_clip = target / f".clip_{annotation.id}_revexport_{uuid.uuid4().hex}.mp4.part"
        try:
            if heuristic:
                from .media_jobs import verify_heuristic_media
                verify_heuristic_media(self.processor, video, submission, annotation, staged_source)
                if clip_path is None:
                    raise MediaCommandError("verified heuristic Clip is required")
                verify_heuristic_media(self.processor, video, submission, annotation, clip_path)
                shutil.copyfile(clip_path, temp_clip)
            else:
                self.processor.render_clip(input_path=str(staged_source), start_frame=start_frame,
                                           frames=plan.frame_count, output_path=str(temp_clip),
                                           fps=snapshot.fps, crop=plan.crop)
            os.replace(temp_clip, target / "clip.mp4")
        finally:
            temp_clip.unlink(missing_ok=True)
        expected = {"fps": snapshot.fps, "width": plan.output_width, "height": plan.output_height,
                    "frame_count": end_frame - start_frame + 1}
        probe = self.processor.probe_clip(str(target / "clip.mp4"), expected=expected)
        tracks_summary = self._write_tracks_range(db, annotation, submission, plan, target / "tracks.json",
                                                  start_frame, end_frame)
        participants = []
        if annotation.category_participant_mode == "role_based":
            assignments = annotation.participant_roles_snapshot or {}
            participants = [
                {"role_key": definition["key"], "role_name": definition["name"],
                 "track_ids": assignments.get(definition["key"], [])}
                for definition in sorted(annotation.role_definitions_snapshot or [],
                                         key=lambda item: item["role_sort_order"])
            ]
        relative_start = annotation.start_frame - start_frame
        relative_end = annotation.end_frame - start_frame
        annotation_doc = {"behavior": annotation.category_name, "mouse_ids": annotation.mouse_ids,
            "participants": participants,
            "confidence": annotation.confidence,
            "frame_range": {"start": relative_start, "end": relative_end},
            "time_range": {"start": relative_start / snapshot.fps,
                           "end": (relative_end + 1) / snapshot.fps}}
        metadata = {"schema_version": "2.0", "clip": {"filename": "clip.mp4", **expected},
            "tracks": {"frame_origin": 0, "time_origin": 0.0, "coordinate_system": "clip_pixels"},
            "pose": {"keypoint_names": snapshot.keypoint_names, "skeleton_edges": snapshot.skeleton_edges},
            "source": {"video_filename": submission.source_video_filename,
                "clip": {"start_frame": start_frame, "end_frame": end_frame,
                         "start_time": plan.start, "end_time": plan.end},
                "annotation": {"start_frame": annotation.start_frame, "end_frame": annotation.end_frame,
                               "start_time": annotation.start_time, "end_time": annotation.end_time},
                "crop_region": ({"x": plan.crop[0], "y": plan.crop[1], "w": plan.crop[2], "h": plan.crop[3]}
                                 if plan.crop else {"x": 0, "y": 0, "w": snapshot.width, "h": snapshot.height})}}
        calibration = db.get(Video, submission.video_id).distance_calibration
        if heuristic:
            metadata["source"] = video.heuristic_input["source"]
        if calibration is not None:
            metadata["distance_scale"] = {
                "cm_per_pixel": calibration["cm_per_pixel"], "unit": "cm"
            }
        elif heuristic and video.heuristic_input.get("distance_scale") is not None:
            metadata["distance_scale"] = video.heuristic_input["distance_scale"]
        if distance is not ...:
            metadata.pop("distance_scale", None)
            if distance is not None:
                metadata["distance_scale"] = distance
        for name, value in (("annotation.json", annotation_doc), ("metadata.json", metadata)):
            with (target / name).open("w", encoding="utf-8") as fh:
                json.dump(value, fh, ensure_ascii=False, indent=2)
        validate_clip_directory(target, probe, tracks_summary)

    def _extend_legacy_payload(self, db, job):
        if all("context" in ref and "distance_scale" in ref for ref in (job.payload or {}).get("refs", [])):
            return
        old = json.loads(json.dumps(job.payload))
        rows = self._frozen_rows(db, job, require_ready=False)
        new = json.loads(json.dumps(old))
        new["preparation_version"] = 1
        by_id = {a.id: (a, s, c) for a, s, c in rows}
        batch = cache.context_batch(db, rows)
        for ref in new["refs"]:
            a, s, c = by_id[ref["submission_annotation_id"]]
            ref.setdefault("context", cache.frozen_context(db, a, s, c, batch))
            ref.setdefault("distance_scale", cache.distance_scale(s))
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        db.expire_all()
        current = db.get(BackgroundJob, job.id)
        if current.status != "running" or current.payload != old:
            raise MediaCommandError("legacy export claim/payload changed")
        current.payload = new
        db.commit()

    def _verify_hot_source(self, submission):
        from .submission_service import validate_storage_key
        from .file_identity import FileIdentity, stream_identity
        root = self.settings.videos_dir.resolve()
        path = (root / validate_storage_key(submission.source_storage_key)).resolve()
        if path == root or not path.is_relative_to(root):
            raise MediaCommandError("immutable submission source is outside videos directory")
        expected = FileIdentity(submission.source_file_size, submission.source_mtime_ns,
                                submission.source_device, submission.source_inode)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            before = stream_identity(stream)
            if expected.device and expected.inode and before != expected:
                raise MediaCommandError("immutable submission source file identity mismatch")
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            if stream_identity(stream) != before:
                raise MediaCommandError("immutable submission source changed while hashing")
        if digest.hexdigest() != submission.source_video_sha256:
            raise MediaCommandError("immutable submission source SHA-256 mismatch")

    def _prewarm(self, db, job):
        job_id = job.id
        payload = json.loads(json.dumps(job.payload))
        if not self.settings.context_prewarm_enabled or not self.settings.context_cache_max_bytes:
            self._release_prewarm_source()
            job.status, job.progress, job.finished_at = "succeeded", 99, _now()
            db.commit()
            return
        row = next((r for r in asset_rows(db, job.project_id, None, source="all")
                    if r[0].id == payload["submission_annotation_id"]), None)
        if row is None:
            self._release_prewarm_source()
            job.status, job.finished_at = "cancelled", _now()
            db.commit()
            return
        annotation, submission, clip = row
        context = cache.frozen_context(db, annotation, submission, clip)
        key = cache.cache_key(self.settings, context)
        if context != payload["context"] or key != payload["key"]:
            raise MediaCommandError("prewarm immutable context changed")
        validate_snapshot_integrity(db, submission.detection_snapshot)
        source = None
        try:
            if self._prewarm_source_identity != cache.source_group_identity(submission):
                self._release_prewarm_source()
            elif self._prewarm_source is not None:
                self._stage_prewarm_source(submission, job.id)
            if cache.lookup(self.settings, submission.video_id, key) is None:
                source = self._stage_prewarm_source(submission, job.id)
                # Preparation owns context material, not the independent exact Clip
                # claim. Avoid timing out behind a concurrently running media job.
                clip_path = source if submission.video.source == "heuristic" else None
                with cache.material(self.settings, submission.video_id, key, job.id,
                        lambda target: self._write_item(db, annotation, submission, source, target,
                            clip_path=clip_path, context=context, distance=None)):
                    pass
            resident = cache.lookup(self.settings, submission.video_id, key) is not None
            db.rollback()
            db.execute(text("BEGIN IMMEDIATE"))
            job = db.get(BackgroundJob, job_id)
            if job is None or job.status != "running" or job.payload != payload:
                raise MediaCommandError("prewarm job claim/payload changed")
            job.status, job.progress, job.finished_at = "succeeded", 100 if resident else 99, _now()
            db.commit()
        except BaseException:
            self._release_prewarm_source()
            raise
        finally:
            if not self._draining or self._stopping.is_set():
                self._release_prewarm_source()

    @staticmethod
    def _validate_zip(path: Path, expected_dirs: set[str]) -> None:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            files = {name for name in archive.namelist() if not name.endswith("/")}
        if bad:
            raise MediaCommandError(f"ZIP CRC failure: {bad}")
        actual_dirs = {str(Path(name).parent).replace("\\", "/") for name in files}
        if actual_dirs != expected_dirs or any({Path(name).name for name in files
                if str(Path(name).parent).replace("\\", "/") == directory} != FILES
                for directory in expected_dirs):
            raise MediaCommandError("ZIP must contain exactly four files per independent clip directory")

    def _build(self, db: Session, job: BackgroundJob) -> None:
        cache.evict_cache(self.settings)
        self._extend_legacy_payload(db, job)
        frozen_payload = json.loads(json.dumps(job.payload))
        rows = self._frozen_rows(db, job, require_ready=False)
        validated_snapshots = set()
        for _annotation, submission, _clip in rows:
            if submission.detection_snapshot_id not in validated_snapshots:
                validate_snapshot_integrity(db, submission.detection_snapshot)
                validated_snapshots.add(submission.detection_snapshot_id)
        db.rollback()
        staging = self.settings.exports_dir / f".export-{job.id}.staging"
        temp_zip = self.settings.exports_dir / f".export-{job.id}.tmp.zip"
        staged_sources = {}; expected_dirs = set(); used = set(); final = None
        published = False
        try:
            self._cleanup_artifacts(staging, temp_zip, [])
            staging.mkdir(parents=True, exist_ok=True)
            groups = {}
            # Freeze ZIP names in original order before grouping sources.
            for index, (annotation, submission, clip) in enumerate(rows, 1):
                category_count = len((job.payload or {}).get("category_ids") or [])
                parent = (job.payload.get("category_directories", {}).get(str(annotation.category_id), "")
                          if category_count > 1 else "")
                base = clip_directory_name(annotation, submission); name = base
                relative = f"{parent}/{name}".strip("/")
                suffix = 0
                token = job.payload["refs"][index - 1]["opaque_token"]
                while relative.casefold() in used:
                    suffix += 1
                    tail = token[:12] if suffix == 1 else f"{token[:12]}_{suffix}"
                    name = safe_part(f"{base}_{tail}", limit=120)
                    relative = f"{parent}/{name}".strip("/")
                used.add(relative.casefold()); expected_dirs.add(relative)
                group_key = cache.source_group_identity(submission)
                groups.setdefault(group_key, []).append((annotation, submission, clip, relative,
                    frozen_payload["refs"][index - 1]))
            with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                completed = 0
                for group_key, entries in groups.items():
                    # Only cold groups need a full private source copy. Hot groups
                    # still verify the immutable source SHA/FD identity once per job.
                    needs_source = any(not clip_entities_ready(c, self.settings) or
                        cache.lookup(self.settings, s.video_id, cache.cache_key(self.settings, ref["context"])) is None
                        for a, s, c, relative, ref in entries)
                    source = None
                    if needs_source:
                        source = stage_submission_input(self.settings, entries[0][1], job.id,
                            reserve_check=lambda path, amount: cache.check_space(self.settings, path, amount))
                        staged_sources[group_key] = source
                    else:
                        self._verify_hot_source(entries[0][1])
                    for annotation, submission, clip, relative, ref in entries:
                        key = cache.cache_key(self.settings, ref["context"])
                        # An entry may have been evicted while a prior entry was
                        # admitted. Stage lazily if the group was initially all-hot.
                        if source is None and cache.lookup(self.settings, submission.video_id, key) is None:
                            source = stage_submission_input(self.settings, submission, job.id,
                                reserve_check=lambda path, amount: cache.check_space(self.settings, path, amount))
                            staged_sources[group_key] = source
                        clip_path = self._ensure_clip(db, job, annotation, submission, clip, source)
                        def build_item(target):
                            nonlocal source
                            if source is None:
                                source = stage_submission_input(self.settings, submission, job.id,
                                    reserve_check=lambda path, amount: cache.check_space(self.settings, path, amount))
                                staged_sources[group_key] = source
                            self._write_item(db, annotation, submission, source, target,
                                clip_path=clip_path, context=ref["context"], distance=None)
                        with cache.material(self.settings, submission.video_id, key, job.id,
                                build_item) as item:
                            for name in sorted(FILES):
                                arcname = f"{relative}/{name}"
                                if name == "metadata.json":
                                    metadata = json.loads((item / name).read_text(encoding="utf-8"))
                                    if ref["distance_scale"] is not None:
                                        metadata["distance_scale"] = ref["distance_scale"]
                                    data = json.dumps(metadata, ensure_ascii=False, indent=2).encode()
                                    cache.check_space(self.settings, temp_zip.parent, len(data) + 4096)
                                    archive.writestr(arcname, data)
                                else:
                                    with (item / name).open("rb") as src, archive.open(arcname, "w", force_zip64=True) as dest:
                                        for chunk in iter(lambda: src.read(1024 * 1024), b""):
                                            cache.check_space(self.settings, temp_zip.parent, len(chunk) + 4096)
                                            dest.write(chunk)
                        completed += 1
                        current = db.get(BackgroundJob, job.id)
                        current.progress = max(1, int(completed * 89 / len(rows)))
                        db.commit()
                    _cleanup_paths([source], operation="remove export source group")
                    staged_sources.pop(group_key, None)
                cache.check_space(self.settings, temp_zip.parent, len(expected_dirs) * 4096)
            current = db.get(BackgroundJob, job.id); current.progress = 90; db.commit()
            self._validate_zip(temp_zip, expected_dirs)
            if self.before_publish_hook: self.before_publish_hook()
            # Transaction A is intentionally short: validate the claim, atomically publish,
            # and persist the published path while the job remains running.
            db.rollback(); db.execute(text("BEGIN IMMEDIATE"))
            current = db.get(BackgroundJob, job.id)
            if current is None or current.status != "running" or current.payload != frozen_payload:
                raise MediaCommandError("export job claim/payload changed before publish")
            locked_rows = self._frozen_rows(db, current, require_ready=False)
            if any(clip is None or clip.status != "ready" or not clip.clip_path or not clip.thumbnail_path
                   for _annotation, _submission, clip in locked_rows):
                raise MediaCommandError("frozen Submission Clip is not ready")
            final_name = f"export_project_{job.project_id}_{job.id}.zip"
            current.progress = 94
            final = self.settings.exports_dir / final_name
            os.replace(temp_zip, final)
            current.result_path = final_name
            current.progress = 97
            db.commit()
            published = True

            # Large filesystem cleanup must never hold SQLite's write lock.
            self._cleanup_artifacts(staging, temp_zip, staged_sources.values())

            # Transaction B fresh-reads and CAS-validates the same running claim/payload.
            db.expire_all(); db.execute(text("BEGIN IMMEDIATE"))
            current = db.get(BackgroundJob, job.id)
            if (current is None or current.status != "running" or current.payload != frozen_payload
                    or current.result_path != final_name):
                db.rollback()
                raise WorkerCleanupIncomplete("published export claim/payload changed before terminal commit")
            if self.before_terminal_hook:
                self.before_terminal_hook()
            current.status, current.progress = "succeeded", 100
            current.finished_at = _now(); current.expires_at = _now() + timedelta(days=self.settings.export_retention_days)
            _release_export_key(current); db.commit()
        except WorkerCleanupIncomplete as exc:
            db.rollback()
            if published:
                # Transaction A made this path authoritative. Preserve both the file and
                # result_path for deterministic administrator cleanup/recovery.
                raise
            try:
                self._cleanup_artifacts(staging, temp_zip, staged_sources.values(), final=final)
            except WorkerCleanupIncomplete as cleanup_exc:
                result_path = final.name if final is not None and final.exists() else None
                raise WorkerCleanupIncomplete(f"{exc}; {cleanup_exc}", result_path=result_path) from exc
            raise
        except Exception as exc:
            db.rollback()
            if published:
                # Once transaction A commits, deleting the final would contradict result_path.
                raise WorkerCleanupIncomplete(f"post-publish export failure: {exc}") from exc
            try:
                self._cleanup_artifacts(staging, temp_zip, staged_sources.values(), final=final)
            except WorkerCleanupIncomplete as cleanup_exc:
                result_path = final.name if final is not None and final.exists() else None
                raise WorkerCleanupIncomplete(str(cleanup_exc), result_path=result_path) from exc
            raise
