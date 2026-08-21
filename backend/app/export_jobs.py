"""Atomic project ZIP export from immutable approved Submission authority."""
from __future__ import annotations

import json
import logging
import os
import shutil
import secrets
import zipfile
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
from .media_jobs import (_truncate_error, claim_and_render_submission_clip, clip_entities_ready,
                         reset_interrupted_job_clips, resolve_entity_path, stage_submission_input)
from .models import (BackgroundJob, BehaviorCategory, Clip, DetectionSnapshotState, Project, RawDetection,
                     Submission, SubmissionAnnotation, Video)
from .submission_media_plan import build_submission_media_plan
from .submission_service import validate_snapshot_integrity

logger = logging.getLogger(__name__)
JOB_TYPE_EXPORT = "export"


def _now() -> datetime:
    return datetime.utcnow()


def export_dedupe_key(project_id: int) -> str:
    return f"export:project:{project_id}:active"


def _release_export_key(job: BackgroundJob) -> None:
    job.dedupe_key = f"export:project:{job.project_id}:history:{job.id}"


def latest_export_job(db: Session, project_id: int) -> BackgroundJob | None:
    return db.query(BackgroundJob).filter_by(job_type=JOB_TYPE_EXPORT, project_id=project_id).order_by(
        BackgroundJob.id.desc()).first()


def approved_rows(db: Session, project_id: int, category_ids: list[int] | None,
                  submission_annotation_ids: list[int] | None = None):
    """Read only immutable current approved Submission copies (status API/enqueue only)."""
    query = (db.query(SubmissionAnnotation, Submission, Clip)
             .join(Submission, Submission.id == SubmissionAnnotation.submission_id)
             .outerjoin(Clip, Clip.submission_annotation_id == SubmissionAnnotation.id)
             .join(Video, Video.id == Submission.video_id)
             .filter(Video.project_id == project_id, Submission.status == "approved"))
    if category_ids:
        query = query.filter(SubmissionAnnotation.category_id.in_(category_ids))
    if submission_annotation_ids is not None:
        query = query.filter(SubmissionAnnotation.id.in_(submission_annotation_ids))
    return query.order_by(SubmissionAnnotation.start_frame, SubmissionAnnotation.id).all()


def enqueue_export_job(db: Session, project: Project, category_ids: list[int] | None) -> BackgroundJob | None:
    requested = sorted(set(category_ids or []))
    rows = approved_rows(db, project.id, requested or None)
    # Empty means all concrete categories actually represented by approved immutable copies now.
    represented = {annotation.category_id for annotation, _submission, _clip in rows}
    frozen_categories = requested if requested else sorted(represented)
    rows = [row for row in rows if row[0].category_id in represented]
    if not rows:
        raise ValueError("No approved clips are eligible for export")
    used_categories: set[str] = set()
    category_directories = {}
    category_tokens = {str(category_id): secrets.token_hex(16) for category_id in frozen_categories}
    for category_id in frozen_categories:
        category = db.get(BehaviorCategory, category_id)
        base = safe_part(category.name, fallback="category", limit=80)
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
    refs = [{"submission_id": submission.id, "submission_annotation_id": annotation.id,
             "snapshot_id": submission.detection_snapshot_id,
             "source_media_revision": submission.source_media_revision,
             "source_sha256": submission.source_video_sha256,
             "source_file_size": submission.source_file_size, "source_mtime_ns": submission.source_mtime_ns,
             "source_device": submission.source_device, "source_inode": submission.source_inode,
             "raw_digest": submission.detection_snapshot.raw_digest,
             "state_digest": submission.detection_snapshot.state_digest,
             "metadata_digest": submission.detection_snapshot.metadata_digest,
             "opaque_token": secrets.token_hex(16)}
            for annotation, submission, _clip in rows]
    result = db.execute(sqlite_insert(BackgroundJob).values(
        project_id=project.id, job_type=JOB_TYPE_EXPORT, status="queued", progress=0,
        dedupe_key=export_dedupe_key(project.id), payload={
            "contract_version": 1, "project_id": project.id, "category_ids": frozen_categories,
            "category_directories": category_directories, "category_tokens": category_tokens,
            "submission_ids": submission_ids, "submission_annotation_ids": annotation_ids,
            "refs": refs,
        }).on_conflict_do_nothing(index_elements=["dedupe_key"]))
    db.commit()
    if result.rowcount != 1:
        return None
    return db.query(BackgroundJob).filter_by(dedupe_key=export_dedupe_key(project.id)).one()


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
        self.before_publish_hook = None

    def start(self, *, recover: bool = True) -> None:
        if recover:
            self._recover_interrupted()
        with self.session_factory() as db:
            ids = [row[0] for row in db.query(BackgroundJob.id).filter_by(
                status="queued", job_type=JOB_TYPE_EXPORT).all()]
        for job_id in ids:
            self.schedule(job_id)

    def shutdown(self) -> None:
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def schedule(self, job_id: int) -> None:
        if self.synchronous:
            self._run_job(job_id)
        else:
            future = self._executor.submit(self._run_job, job_id)
            self._futures.add(future); future.add_done_callback(self._futures.discard)

    def _recover_interrupted(self) -> None:
        with self.session_factory() as db:
            for job in db.query(BackgroundJob).filter_by(status="running", job_type=JOB_TYPE_EXPORT):
                if job.attempts >= self.settings.media_max_attempts:
                    job.status, job.error, job.finished_at = "failed", "Interrupted; retry limit reached", _now()
                    _release_export_key(job)
                    reset_interrupted_job_clips(db, job)
                else:
                    job.status, job.started_at, job.error = "queued", None, "Interrupted; requeued"
                    reset_interrupted_job_clips(db, job)
            db.commit()

    def _run_job(self, job_id: int) -> None:
        try:
            with self.session_factory() as db:
                claimed = db.query(BackgroundJob).filter_by(id=job_id, status="queued").update(
                    {"status": "running", "started_at": _now(),
                     "attempts": BackgroundJob.attempts + 1}, synchronize_session=False)
                db.commit()
                if not claimed:
                    return
                job = db.get(BackgroundJob, job_id)
                try:
                    self._build(db, job)
                except Exception as exc:
                    db.rollback(); job = db.get(BackgroundJob, job_id)
                    if job and job.status == "running":
                        job.status, job.error, job.finished_at = "failed", _truncate_error(f"Export failed: {exc}"), _now()
                        _release_export_key(job); db.commit()
        except Exception:
            logger.exception("Export job %s crashed", job_id)

    def _frozen_rows(self, db: Session, job: BackgroundJob, *, require_ready: bool):
        payload = job.payload or {}
        if payload.get("contract_version") != 1 or payload.get("project_id") != job.project_id:
            raise MediaCommandError("invalid export payload")
        ids = payload.get("submission_annotation_ids")
        refs = payload.get("refs")
        if not isinstance(ids, list) or ids != list(dict.fromkeys(ids)) or len(refs or []) != len(ids):
            raise MediaCommandError("invalid immutable export reference set")
        rows = (db.query(SubmissionAnnotation, Submission, Clip)
                .join(Submission, Submission.id == SubmissionAnnotation.submission_id)
                .outerjoin(Clip, Clip.submission_annotation_id == SubmissionAnnotation.id)
                .join(Video, Video.id == Submission.video_id)
                .filter(Video.project_id == job.project_id, SubmissionAnnotation.id.in_(ids))
                .order_by(SubmissionAnnotation.id).all()) if ids else []
        by_id = {annotation.id: (annotation, submission, clip) for annotation, submission, clip in rows}
        if set(by_id) != set(ids):
            raise MediaCommandError("frozen SubmissionAnnotation no longer exists")
        refs_by_id = {ref.get("submission_annotation_id"): ref for ref in refs}
        for annotation_id in ids:
            annotation, submission, clip = by_id[annotation_id]; ref = refs_by_id.get(annotation_id, {})
            # Superseded is allowed: it was approved at enqueue and immutable; never reselect current.
            if submission.status not in {"approved", "superseded"}:
                raise MediaCommandError("frozen Submission is no longer approved/superseded")
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
        return [by_id[item_id] for item_id in ids]

    def _ensure_clip(self, db, job, annotation, submission, clip, staged_source: Path) -> Path:
        if clip is None:
            clip = Clip(submission_annotation_id=annotation.id, media_revision=1, status="pending")
            db.add(clip); db.commit(); db.refresh(clip)
        if not clip_entities_ready(clip, self.settings):
            return claim_and_render_submission_clip(db, self.processor, self.settings, submission.id,
                                                    annotation.id, clip.id, input_path=staged_source)[0]
        path = resolve_entity_path(clip.clip_path, self.settings.clips_dir)
        if path is None or not path.is_file():
            raise MediaCommandError("ready Submission Clip path is unsafe or missing")
        return path

    def _write_tracks(self, db, annotation, submission, plan, path: Path):
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
            start_frame=annotation.start_frame, end_frame=annotation.end_frame,
            snapshot_id=snapshot.id).order_by(RawDetection.frame_index, RawDetection.frame_detection_index,
                                               RawDetection.id).yield_per(500)
        iterator = iter(query); current = next(iterator, None)
        with path.open("w", encoding="utf-8") as fh:
            fh.write("[")
            for relative in range(annotation.end_frame - annotation.start_frame + 1):
                absolute = annotation.start_frame + relative; detections = []
                while current is not None and current[0].frame_index == absolute:
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
        return TracksSummary(annotation.end_frame - annotation.start_frame + 1, frozenset(valid_ids))

    def _write_item(self, db, annotation, submission, clip_path: Path, target: Path) -> None:
        snapshot = submission.detection_snapshot
        plan = build_submission_media_plan(start_time=annotation.start_time, end_time=annotation.end_time,
            start_frame=annotation.start_frame, end_frame=annotation.end_frame, fps=snapshot.fps,
            frame_count=snapshot.frame_count, width=snapshot.width, height=snapshot.height,
            crop_region=annotation.crop_region)
        target.mkdir(parents=True)
        try: os.link(clip_path, target / "clip.mp4")
        except OSError: shutil.copy2(clip_path, target / "clip.mp4")
        expected = {"fps": snapshot.fps, "width": plan.output_width, "height": plan.output_height,
                    "frame_count": annotation.end_frame - annotation.start_frame + 1}
        probe = self.processor.probe_clip(str(target / "clip.mp4"), expected=expected)
        tracks_summary = self._write_tracks(db, annotation, submission, plan, target / "tracks.json")
        participants = []
        if annotation.category_participant_mode == "role_based":
            assignments = annotation.participant_roles_snapshot or {}
            participants = [
                {"role_key": definition["key"], "role_name": definition["name"],
                 "track_ids": assignments.get(definition["key"], [])}
                for definition in sorted(annotation.role_definitions_snapshot or [],
                                         key=lambda item: item["role_sort_order"])
            ]
        annotation_doc = {"behavior": annotation.category_name, "mouse_ids": annotation.mouse_ids,
            "participants": participants,
            "confidence": annotation.confidence,
            "frame_range": {"start": 0, "end": expected["frame_count"] - 1},
            "time_range": {"start": 0.0, "end": expected["frame_count"] / snapshot.fps}}
        metadata = {"schema_version": "1.0", "clip": {"filename": "clip.mp4", **expected},
            "tracks": {"frame_origin": 0, "time_origin": 0.0, "coordinate_system": "clip_pixels"},
            "pose": {"keypoint_names": snapshot.keypoint_names, "skeleton_edges": snapshot.skeleton_edges},
            "source": {"video_filename": submission.source_video_filename,
                "start_frame": annotation.start_frame, "end_frame": annotation.end_frame,
                "start_time": plan.start, "end_time": plan.end,
                "crop_region": ({"x": plan.crop[0], "y": plan.crop[1], "w": plan.crop[2], "h": plan.crop[3]}
                                if plan.crop else {"x": 0, "y": 0, "w": snapshot.width, "h": snapshot.height})}}
        for name, value in (("annotation.json", annotation_doc), ("metadata.json", metadata)):
            with (target / name).open("w", encoding="utf-8") as fh:
                json.dump(value, fh, ensure_ascii=False, indent=2)
        validate_clip_directory(target, probe, tracks_summary)

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
        shutil.rmtree(staging, ignore_errors=True); temp_zip.unlink(missing_ok=True)
        staging.mkdir(parents=True, exist_ok=True)
        staged_sources = {}; expected_dirs = set(); used = set(); final = None
        try:
            for index, (annotation, submission, clip) in enumerate(rows, 1):
                source = staged_sources.get(submission.id)
                if source is None:
                    source = stage_submission_input(self.settings, submission, job.id)
                    staged_sources[submission.id] = source
                clip_path = self._ensure_clip(db, job, annotation, submission, clip, source)
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
                self._write_item(db, annotation, submission, clip_path, staging / relative)
                current = db.get(BackgroundJob, job.id); current.progress = int(index * 90 / len(rows)) if rows else 90
                db.commit()
            with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(staging.rglob("*")):
                    if path.is_file(): archive.write(path, path.relative_to(staging).as_posix())
            self._validate_zip(temp_zip, expected_dirs)
            if self.before_publish_hook: self.before_publish_hook()
            db.rollback(); db.execute(text("BEGIN IMMEDIATE"))
            current = db.get(BackgroundJob, job.id)
            if current is None or current.status != "running" or current.payload != frozen_payload:
                raise MediaCommandError("export job claim/payload changed before publish")
            locked_rows = self._frozen_rows(db, current, require_ready=False)
            if any(clip is None or clip.status != "ready" or not clip.clip_path or not clip.thumbnail_path
                   for _annotation, _submission, clip in locked_rows):
                raise MediaCommandError("frozen Submission Clip is not ready")
            final_name = f"export_project_{job.project_id}_{job.id}.zip"
            final = self.settings.exports_dir / final_name
            os.replace(temp_zip, final)
            current.status, current.progress, current.result_path = "succeeded", 100, final_name
            current.finished_at = _now(); current.expires_at = _now() + timedelta(days=self.settings.export_retention_days)
            _release_export_key(current); db.commit()
        except Exception:
            db.rollback()
            if final is not None:
                final.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True); temp_zip.unlink(missing_ok=True)
            for source in staged_sources.values(): source.unlink(missing_ok=True)
