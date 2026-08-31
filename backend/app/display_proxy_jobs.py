"""Explicit enqueue and single-owner worker lane for candidate display proxies."""
from __future__ import annotations

import errno
import hashlib
import logging
import math
import os
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .display_proxy_processor import (DISPLAY_PROXY_PROFILE_VERSION, DisplayProxyError,
                                      sanitize_error)
from .display_proxy_observability import error_category, log_display_event
from .models import BackgroundJob, Video
from .process_lock import ProcessLock

JOB_TYPE_DISPLAY_PROXY = "display_proxy"
DISPLAY_PROXY_ESTIMATED_SIZE_RATIO = 0.30
DISPLAY_PROXY_DISK_SPACE_ERROR = "display proxy disk space unavailable"


class DisplayProxyOwnershipLost(RuntimeError):
    pass


class DisplayProxyDiskSpaceError(DisplayProxyError):
    """Stable, path-free terminal category for reserve and ENOSPC failures."""


class DisplayProcessor(Protocol):
    def render(self, *, input_path: str, output_path: str) -> None: ...


def _now() -> datetime:
    return datetime.utcnow()


def display_proxy_dedupe_key(video_id: int, source_sha256: str,
                             profile: str = DISPLAY_PROXY_PROFILE_VERSION) -> str:
    return f"display-proxy:video:{video_id}:source:{source_sha256}:profile:{profile}"


def _valid_sha(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _valid_payload(payload: object) -> bool:
    return (isinstance(payload, dict)
            and set(payload) == {"video_id", "project_id", "source_sha256", "profile_version"}
            and isinstance(payload["video_id"], int) and not isinstance(payload["video_id"], bool)
            and payload["video_id"] > 0
            and isinstance(payload["project_id"], int) and not isinstance(payload["project_id"], bool)
            and payload["project_id"] > 0
            and _valid_sha(payload["source_sha256"])
            and payload["profile_version"] == DISPLAY_PROXY_PROFILE_VERSION)


def display_proxy_names(payload: object) -> tuple[str, str] | None:
    """Return the only P1 temp/final names accepted for a frozen job payload."""
    if not _valid_payload(payload):
        return None
    stem = (f"video-{payload['video_id']}-{payload['source_sha256'][:16]}-"
            f"{payload['profile_version']}")
    final = f"{stem}.mp4"
    return f".{final}.part", final


def enqueue_display_proxy(db: Session, video: Video) -> BackgroundJob:
    """Explicit internal capability; callers must first establish stable source identity."""
    if not video.id or not _valid_sha(video.source_sha256):
        raise ValueError("display proxy requires an existing video with stable source_sha256")
    if not video.storage_path:
        raise ValueError("display proxy requires source storage_path")
    profile = DISPLAY_PROXY_PROFILE_VERSION
    key = display_proxy_dedupe_key(video.id, video.source_sha256, profile)
    payload = {"video_id": video.id, "project_id": video.project_id,
               "source_sha256": video.source_sha256, "profile_version": profile}
    result = db.execute(sqlite_insert(BackgroundJob).values(
        project_id=video.project_id, job_type=JOB_TYPE_DISPLAY_PROXY, status="queued",
        progress=0, attempts=0, dedupe_key=key, payload=payload,
    ).on_conflict_do_nothing(index_elements=["dedupe_key"]))
    inserted = result.rowcount == 1
    job = db.query(BackgroundJob).filter(BackgroundJob.dedupe_key == key).one()
    requeued = not inserted and job.status in {"failed", "cancelled"}
    if requeued:
        job.status, job.progress, job.attempts = "queued", 0, 0
        job.result_path = job.error = job.started_at = job.finished_at = job.run_token = None
    if inserted or requeued:
        video.display_status = "pending"
        video.display_path = video.display_error = video.display_profile_version = None
        video.display_source_sha256 = video.display_generated_at = None
    db.flush()
    if inserted or requeued:
        log_display_event(logging.INFO, "display_proxy_enqueue", job_id=job.id,
                          video_id=video.id, project_id=video.project_id, profile=profile,
                          status="queued")
    return job


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class DisplayProxyWorker:
    def __init__(self, *, processor: DisplayProcessor, session_factory, settings) -> None:
        self.processor, self.session_factory, self.settings = processor, session_factory, settings
        self._executor = None
        self._lock = ProcessLock(settings.display_proxies_dir / ".worker.lock")

    def _paths(self, payload: dict) -> tuple[Path, Path]:
        names = display_proxy_names(payload)
        if names is None:
            raise DisplayProxyError("invalid frozen display proxy payload")
        temp_name, final_name = names
        return (self.settings.display_proxies_dir / temp_name,
                self.settings.display_proxies_dir / final_name)

    def _require_disk_space(self, additional_bytes: int) -> None:
        try:
            free = shutil.disk_usage(self.settings.display_proxies_dir).free
        except OSError as exc:
            raise DisplayProxyDiskSpaceError(DISPLAY_PROXY_DISK_SPACE_ERROR) from exc
        if free - additional_bytes < self.settings.display_proxy_disk_reserve_bytes:
            raise DisplayProxyDiskSpaceError(DISPLAY_PROXY_DISK_SPACE_ERROR)

    @staticmethod
    def _safe_processing_error(exc: Exception) -> Exception:
        text = str(exc).lower()
        if (getattr(exc, "errno", None) == errno.ENOSPC
                or "no space left on device" in text
                or "not enough space on the disk" in text):
            return DisplayProxyDiskSpaceError(DISPLAY_PROXY_DISK_SPACE_ERROR)
        return exc

    def _source_path(self, storage_path: str | None) -> Path:
        root = self.settings.videos_dir.resolve()
        if not storage_path:
            raise DisplayProxyError("display proxy source is missing or outside videos directory")
        raw = Path(storage_path)
        result = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
        if result == root or not result.is_relative_to(root) or not result.is_file():
            raise DisplayProxyError("display proxy source is missing or outside videos directory")
        return result

    @staticmethod
    def _owns(db: Session, job_id: int, token: str, payload: dict) -> bool:
        job = db.get(BackgroundJob, job_id)
        video = db.get(Video, payload["video_id"])
        return bool(job and video and job.job_type == JOB_TYPE_DISPLAY_PROXY
                    and job.payload == payload and job.status == "running" and job.run_token == token
                    and video.project_id == payload["project_id"]
                    and video.source_sha256 == payload["source_sha256"]
                    and video.display_source_sha256 is None
                    and video.display_profile_version is None
                    and video.display_status == "processing")

    def _cancel_if_owned_job(self, job_id: int, token: str) -> None:
        with self.session_factory() as db:
            db.query(BackgroundJob).filter(BackgroundJob.id == job_id,
                BackgroundJob.status == "running", BackgroundJob.run_token == token).update({
                    "status": "cancelled", "error": "display proxy ownership changed",
                    "finished_at": _now(), "run_token": None}, synchronize_session=False)
            db.commit()

    def _fail(self, job_id: int, token: str, payload: dict, exc: Exception,
              source: Path | None = None, elapsed_ms: int | None = None) -> None:
        error = (sanitize_error(exc, source or "", self.settings.videos_dir,
                                self.settings.display_proxies_dir)
                 or "display proxy processing failed")
        with self.session_factory() as db:
            job_changed = db.query(BackgroundJob).filter(
                BackgroundJob.id == job_id, BackgroundJob.status == "running",
                BackgroundJob.run_token == token).update({"status": "failed", "error": error,
                    "finished_at": _now(), "run_token": None}, synchronize_session=False)
            if job_changed and all(key in payload for key in
                                   ("video_id", "source_sha256", "profile_version")):
                db.query(Video).filter(Video.id == payload["video_id"],
                    Video.source_sha256 == payload["source_sha256"],
                    Video.display_source_sha256.is_(None),
                    Video.display_profile_version.is_(None),
                    Video.display_status == "processing").update({
                        "display_status": "failed", "display_error": error},
                        synchronize_session=False)
            db.commit()
        identity = ({"video_id": payload["video_id"], "project_id": payload["project_id"],
                     "profile": payload["profile_version"]} if _valid_payload(payload) else {})
        log_display_event(logging.ERROR, "display_proxy_failed", job_id=job_id,
                          status="failed", elapsed_ms=elapsed_ms,
                          error_category=error_category(exc), **identity)

    def _claim(self, job_id: int) -> tuple[str, dict] | None:
        token = uuid.uuid4().hex
        with self.session_factory() as db:
            changed = db.query(BackgroundJob).filter(
                BackgroundJob.id == job_id, BackgroundJob.job_type == JOB_TYPE_DISPLAY_PROXY,
                BackgroundJob.status == "queued").update({"status": "running",
                    "run_token": token, "attempts": BackgroundJob.attempts + 1,
                    "started_at": _now(), "finished_at": None, "error": None},
                    synchronize_session=False)
            if changed != 1:
                db.rollback(); return None
            job = db.get(BackgroundJob, job_id); payload = dict(job.payload or {})
            if not _valid_payload(payload):
                db.commit()
                self._fail(job_id, token, payload, DisplayProxyError("invalid frozen display proxy payload"))
                return None
            video_changed = db.query(Video).filter(Video.id == payload["video_id"],
                Video.project_id == payload["project_id"],
                Video.source_sha256 == payload["source_sha256"],
                Video.display_source_sha256.is_(None),
                Video.display_profile_version.is_(None),
                Video.display_status.in_(("pending", "failed"))).update({
                    "display_status": "processing", "display_error": None},
                    synchronize_session=False)
            if video_changed != 1:
                job.status, job.error, job.finished_at, job.run_token = (
                    "cancelled", "display proxy ownership changed", _now(), None)
                db.commit(); return None
            db.commit()
        log_display_event(logging.INFO, "display_proxy_claim", job_id=job_id,
                          video_id=payload["video_id"], project_id=payload["project_id"],
                          profile=payload["profile_version"], status="running")
        return token, payload

    def _after_terminal_commit(self, job_id: int, payload: dict, final: Path) -> None:
        """Fault-injection seam called only after the terminal commit returns."""

    def _commit_terminal(self, db: Session) -> None:
        """Commit seam used to exercise a definite terminal transaction failure."""
        db.commit()

    def _terminal_commit_is_visible(self, job_id: int, payload: dict, final: Path) -> bool:
        """Resolve an uncertain commit with a new transaction/session."""
        with self.session_factory() as db:
            job = db.get(BackgroundJob, job_id)
            video = db.get(Video, payload["video_id"])
            return bool(job and video
                        and job.job_type == JOB_TYPE_DISPLAY_PROXY
                        and job.payload == payload
                        and job.status == "succeeded" and job.run_token is None
                        and job.result_path == final.name
                        and video.project_id == payload["project_id"]
                        and video.display_status == "ready"
                        and video.display_path == final.name
                        and video.source_sha256 == payload["source_sha256"]
                        and video.display_source_sha256 == payload["source_sha256"]
                        and video.display_profile_version == payload["profile_version"])

    def _final_is_referenced(self, final: Path) -> bool:
        with self.session_factory() as db:
            return db.query(Video.id).filter(
                Video.display_status == "ready", Video.display_path == final.name).first() is not None

    def _run(self, job_id: int) -> None:
        claim = self._claim(job_id)
        if claim is None: return
        token, payload = claim
        started = time.monotonic()
        temp, final = self._paths(payload); source = None; replaced = False
        try:
            temp.unlink(missing_ok=True)
            with self.session_factory() as db:
                if not self._owns(db, job_id, token, payload):
                    self._cancel_if_owned_job(job_id, token); return
                source = self._source_path(db.get(Video, payload["video_id"]).storage_path)
            if _hash(source) != payload["source_sha256"]:
                raise DisplayProxyError("source SHA-256 mismatch before transcoding")
            estimated = math.ceil(source.stat().st_size * DISPLAY_PROXY_ESTIMATED_SIZE_RATIO)
            self._require_disk_space(estimated)
            self.processor.render(input_path=str(source), output_path=str(temp))
            if _hash(source) != payload["source_sha256"]:
                raise DisplayProxyError("source SHA-256 changed during transcoding")
            with temp.open("rb+") as handle:
                handle.flush(); os.fsync(handle.fileno())
            # rename itself is allocation-free, but publishing while already below the
            # reserve would make the generated final compete with recovery/deletion IO.
            self._require_disk_space(0)
            with self.session_factory() as db:
                if not self._owns(db, job_id, token, payload):
                    temp.unlink(missing_ok=True); self._cancel_if_owned_job(job_id, token); return
            os.replace(temp, final); replaced = True
            _fsync_directory(final.parent)
            with self.session_factory() as db:
                if not self._owns(db, job_id, token, payload):
                    raise DisplayProxyOwnershipLost("display proxy ownership changed after publish")
                video_changed = db.query(Video).filter(Video.id == payload["video_id"],
                    Video.project_id == payload["project_id"],
                    Video.source_sha256 == payload["source_sha256"],
                    Video.display_source_sha256.is_(None),
                    Video.display_profile_version.is_(None),
                    Video.display_status == "processing").update({"display_path": final.name,
                        "display_status": "ready", "display_error": None,
                        "display_source_sha256": payload["source_sha256"],
                        "display_profile_version": payload["profile_version"],
                        "display_generated_at": _now()}, synchronize_session=False)
                job_changed = db.query(BackgroundJob).filter(BackgroundJob.id == job_id,
                    BackgroundJob.status == "running", BackgroundJob.run_token == token).update({
                        "status": "succeeded", "progress": 100, "result_path": final.name,
                        "error": None, "finished_at": _now(), "run_token": None},
                        synchronize_session=False)
                if video_changed != 1 or job_changed != 1:
                    db.rollback(); raise DisplayProxyOwnershipLost(
                        "display proxy ownership changed during commit")
                self._commit_terminal(db)
                self._after_terminal_commit(job_id, payload, final)
            log_display_event(logging.INFO, "display_proxy_ready", job_id=job_id,
                              video_id=payload["video_id"], project_id=payload["project_id"],
                              profile=payload["profile_version"], status="ready",
                              elapsed_ms=round((time.monotonic() - started) * 1000),
                              bytes=final.stat().st_size)
        except DisplayProxyOwnershipLost:
            temp.unlink(missing_ok=True)
            if replaced and self._terminal_commit_is_visible(job_id, payload, final):
                return
            if replaced and not self._final_is_referenced(final):
                final.unlink(missing_ok=True)
            self._cancel_if_owned_job(job_id, token)
        except Exception as exc:
            temp.unlink(missing_ok=True)
            if replaced and self._terminal_commit_is_visible(job_id, payload, final):
                return
            if replaced and not self._final_is_referenced(final):
                final.unlink(missing_ok=True)
            safe_exc = self._safe_processing_error(exc)
            self._fail(job_id, token, payload, safe_exc, source,
                       round((time.monotonic() - started) * 1000))

    def submit(self, job_id: int) -> None:
        if self.settings.display_proxy_synchronous:
            self._run(job_id)
        elif self._executor is not None:
            self._executor.submit(self._run, job_id)

    def recover(self) -> None:
        """Recover only this lane; never creates work by scanning videos."""
        queued: list[int] = []
        recovery_events: list[dict] = []
        with self.session_factory() as db:
            jobs = db.query(BackgroundJob).filter(
                BackgroundJob.job_type == JOB_TYPE_DISPLAY_PROXY,
                BackgroundJob.status.in_(("queued", "running"))).all()
            for job in jobs:
                payload = job.payload if isinstance(job.payload, dict) else {}
                if job.status == "queued":
                    video = db.get(Video, payload.get("video_id"))
                    if (_valid_payload(payload) and video
                            and video.display_status == "processing"
                            and video.project_id == payload["project_id"]
                            and video.source_sha256 == payload["source_sha256"]
                            and video.display_source_sha256 is None
                            and video.display_profile_version is None):
                        video.display_status = "pending"
                    queued.append(job.id)
                    recovery_events.append({"job_id": job.id, "status": "queued",
                                            **({"video_id": payload["video_id"],
                                                "project_id": payload["project_id"],
                                                "profile": payload["profile_version"]}
                                               if _valid_payload(payload) else {})})
                    continue
                if not _valid_payload(payload):
                    # A malformed frozen payload cannot establish ownership of any
                    # filesystem path. Fail it without touching candidate outputs so
                    # one corrupt row cannot block recovery or delete another job's
                    # temp/final file.
                    job.status, job.error, job.finished_at, job.run_token = (
                        "failed", "invalid frozen display proxy payload", _now(), None)
                    recovery_events.append({"job_id": job.id, "status": "failed",
                                            "error_category": "invalid_payload"})
                    continue
                temp = final = None
                temp, final = self._paths(payload)
                video = db.get(Video, payload.get("video_id"))
                owns = bool(video and _valid_payload(payload)
                            and video.display_status == "processing"
                            and video.project_id == payload.get("project_id")
                            and video.source_sha256 == payload.get("source_sha256")
                            and video.display_source_sha256 is None
                            and video.display_profile_version is None)
                if temp: temp.unlink(missing_ok=True)
                if final and owns: final.unlink(missing_ok=True)
                if job.attempts < self.settings.display_proxy_max_attempts:
                    job.status, job.run_token, job.error = "queued", None, None
                    if owns: video.display_status = "pending"
                    queued.append(job.id)
                    recovery_events.append({"job_id": job.id, "video_id": payload["video_id"],
                                            "project_id": payload["project_id"],
                                            "profile": payload["profile_version"],
                                            "status": "queued"})
                else:
                    job.status, job.error, job.finished_at, job.run_token = (
                        "failed", "display proxy retry limit exhausted", _now(), None)
                    if owns: video.display_status, video.display_error = "failed", job.error
                    recovery_events.append({"job_id": job.id, "video_id": payload["video_id"],
                                            "project_id": payload["project_id"],
                                            "profile": payload["profile_version"],
                                            "status": "failed",
                                            "error_category": "retry_exhausted"})
            active_ids = set()
            for job in jobs:
                payload = job.payload
                if not (_valid_payload(payload) and job.status in {"queued", "running"}):
                    continue
                video = db.get(Video, payload["video_id"])
                if (video and video.display_status == "processing"
                        and video.project_id == payload["project_id"]
                        and video.source_sha256 == payload["source_sha256"]
                        and video.display_source_sha256 is None
                        and video.display_profile_version is None):
                    active_ids.add(video.id)
            for video in db.query(Video).filter(Video.display_status == "processing").all():
                if video.id not in active_ids:
                    video.display_status = "failed"
                    video.display_error = "display proxy owner missing after restart"
                    recovery_events.append({"video_id": video.id, "project_id": video.project_id,
                                            "status": "failed", "error_category": "owner_missing"})
            db.commit()
        for fields in recovery_events:
            log_display_event(logging.WARNING, "display_proxy_recovery", **fields)
        for job_id in queued: self.submit(job_id)

    def start(self) -> None:
        self._lock.acquire()
        try:
            if not self.settings.display_proxy_synchronous:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="display-proxy")
            self.recover()
        except Exception:
            if self._executor: self._executor.shutdown(wait=True); self._executor = None
            self._lock.release(); raise

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True); self._executor = None
        self._lock.release()
