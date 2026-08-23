"""Retention cleanup for generated exports, known temporary files, and job history."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from .cleanup_io import (
    append_cleanup_issues,
    remove_checked,
    safe_path,
    trusted_root,
    update_cleanup_log,
)
from .models import Annotation, BackgroundJob, Clip, Video

TERMINAL_JOB_STATUSES = ("succeeded", "failed", "cancelled")
_VIDEO_TEMP = re.compile(r"^[0-9a-fA-F]{32}\.part$")
_CLIP_TEMP = re.compile(r"^\.clip_\d+_rev\d+\.[0-9a-fA-F]{32}\.mp4\.part$")
_THUMB_TEMP = re.compile(r"^\.clip_\d+_rev\d+\.[0-9a-fA-F]{32}\.jpg\.part$")
_EXPORT_STAGING = re.compile(r"^\.export(?:-\d+|_\d+_\d+)\.staging$")
_EXPORT_TEMP_ZIP = re.compile(r"^\.export(?:-\d+|_\d+_\d+)\.tmp\.zip$")
_SUBMISSION_STAGING = re.compile(
    r"^\.submission(?:-media-job-\d+-|_media_job_\d+_)[0-9a-fA-F]{32}\.staging$"
)
_EXPORT_ZIP = re.compile(r"^export_project_\d+_\d+\.zip$")
_MEDIA_NAME = re.compile(r"^clip_(?P<annotation_id>\d+)_rev(?P<revision>\d+)\.(?P<ext>mp4|jpg)$")


def _safe_path(
    stored: str | None, root_dir: Path, data_dir: Path
) -> tuple[Path | None, str | None]:
    return safe_path(stored, root_dir, data_dir)


def _older_than(path: Path, cutoff: datetime) -> bool:
    try:
        return path.stat(follow_symlinks=False).st_mtime < cutoff.timestamp()
    except (FileNotFoundError, OSError):
        return False


def _issue(kind: str, path: str | None, reason: str, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"kind": kind, "reason": reason}
    if path is not None:
        entry["path"] = path
    entry.update(extra)
    return entry


def _root_issue(root: Path, reason: str) -> dict[str, Any]:
    return _issue("untrusted-root", str(root), reason, root=root.name)


def _referenced_media_paths(db: Session, settings) -> set[Path]:
    referenced: set[Path] = set()
    for clip_path, thumbnail_path in db.query(Clip.clip_path, Clip.thumbnail_path).all():
        for stored, root in (
            (clip_path, settings.clips_dir),
            (thumbnail_path, settings.thumbnails_dir),
        ):
            path, reason = _safe_path(stored, root, settings.data_dir)
            if path is not None and reason is None:
                referenced.add(path)
    return referenced


def _retry_cleanup_issues(
    db: Session, settings, report: dict[str, Any], *, dry_run: bool
) -> None:
    log_path = settings.cleanup_log
    if not log_path.exists():
        return
    referenced = _referenced_media_paths(db, settings)
    def consume(lines: list[str]) -> list[str]:
        replacements: list[str] = []
        for line in lines:
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                replacements.append(line)
                report["issues_retained"] += 1
                continue
            if (
                not isinstance(entry, dict)
                or entry.get("kind") != "delete-failed"
                or entry.get("cleanup_status") == "resolved"
            ):
                replacements.append(line)
                report["issues_retained"] += 1
                continue
            match = _MEDIA_NAME.fullmatch(Path(str(entry.get("path", ""))).name)
            if match is None:
                replacements.append(line)
                report["issues_retained"] += 1
                continue
            annotation_id = int(match.group("annotation_id"))
            revision = int(match.group("revision"))
            root = settings.clips_dir if match.group("ext") == "mp4" else settings.thumbnails_dir
            path, reason = _safe_path(entry.get("path"), root, settings.data_dir)
            trusted, _ = trusted_root(settings.data_dir, root)
            if path is None or trusted is None or path.parent != trusted or path in referenced:
                replacements.append(line)
                report["issues_retained"] += 1
                continue
            row = (
                db.query(Annotation.id, Video.annotation_revision)
                .join(Video, Video.id == Annotation.video_id)
                .filter(Annotation.id == annotation_id)
                .one_or_none()
            )
            if row is not None and row.annotation_revision == revision:
                replacements.append(line)
                report["issues_retained"] += 1
                continue
            if dry_run:
                replacements.append(line)
                report["issues_would_resolve"] += 1
                continue
            _deleted, delete_reason = remove_checked(
                path, root_dir=root, data_dir=settings.data_dir
            )
            if delete_reason is not None:
                replacements.append(line)
                report["issues_retained"] += 1
                continue
            entry["cleanup_status"] = "resolved"
            entry["resolved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            replacements.append(json.dumps(entry, ensure_ascii=False))
            report["issues_resolved"] += 1
        return replacements

    try:
        update_cleanup_log(log_path, consume, write=not dry_run)
    except OSError as exc:
        report["issues"].append(_issue("issue-log-read-failed", str(log_path), str(exc)))


def _remove_known_temp(
    data_dir: Path,
    root_dir: Path,
    pattern: re.Pattern[str],
    cutoff: datetime,
    report: dict[str, Any],
    *,
    directories: bool = False,
    dry_run: bool,
) -> None:
    root, reason = trusted_root(data_dir, root_dir)
    if root is None:
        report["issues"].append(_root_issue(root_dir, reason or "unsafe"))
        return
    if not root.exists():
        return
    for candidate in root.iterdir():
        path, path_reason = _safe_path(candidate.name, root_dir, data_dir)
        if path is None:
            if pattern.fullmatch(candidate.name):
                report["issues"].append(_issue("unsafe-path", str(candidate), path_reason or "unsafe"))
            continue
        if not pattern.fullmatch(path.name) or not _older_than(path, cutoff):
            continue
        try:
            expected_type = path.is_dir() if directories else path.is_file()
        except OSError:
            expected_type = False
        if not expected_type:
            continue
        if dry_run:
            report["would_delete"] += 1
            continue
        deleted, delete_reason = remove_checked(
            path,
            directory=directories,
            root_dir=root_dir,
            data_dir=data_dir,
        )
        report["deleted"] += int(deleted)
        if delete_reason is not None:
            report["issues"].append(_issue("delete-failed", str(path), delete_reason))


def _valid_export_path(job: BackgroundJob, settings, now: datetime) -> Path | None:
    if (
        job.job_type != "export"
        or job.status != "succeeded"
        or job.expires_at is None
        or job.expires_at <= now
        or not job.result_path
        or not _EXPORT_ZIP.fullmatch(Path(job.result_path).name)
    ):
        return None
    path, reason = _safe_path(job.result_path, settings.exports_dir, settings.data_dir)
    root, _ = trusted_root(settings.data_dir, settings.exports_dir)
    if reason is not None or path is None or root is None or path.parent != root:
        return None
    return path


def run_retention_cleanup(
    db: Session,
    settings,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    current_job_id: int | None = None,
) -> dict[str, Any]:
    """Run one deterministic retention pass. ``now`` is injectable for boundary tests."""
    now = now or datetime.utcnow()
    temp_cutoff = now - timedelta(hours=settings.temp_retention_hours)
    job_cutoff = now - timedelta(days=settings.job_retention_days)
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "deleted": 0,
        "would_delete": 0,
        "result_paths_cleared": 0,
        "result_paths_would_clear": 0,
        "jobs_deleted": 0,
        "jobs_would_delete": 0,
        "issues_resolved": 0,
        "issues_would_resolve": 0,
        "issues_retained": 0,
        "issues": [],
    }

    _retry_cleanup_issues(db, settings, report, dry_run=dry_run)
    expired_exports = (
        db.query(BackgroundJob)
        .filter(
            BackgroundJob.job_type == "export",
            BackgroundJob.result_path.is_not(None),
            BackgroundJob.expires_at.is_not(None),
            BackgroundJob.expires_at <= now,
        )
        .all()
    )
    expired_ids = {job.id for job in expired_exports}
    for job in expired_exports:
        path, reason = _safe_path(job.result_path, settings.exports_dir, settings.data_dir)
        root, root_reason = trusted_root(settings.data_dir, settings.exports_dir)
        if root is None:
            issue = _root_issue(settings.exports_dir, root_reason or "unsafe")
            issue["job_id"] = job.id
            report["issues"].append(issue)
            if not dry_run:
                append_cleanup_issues(settings.cleanup_log, [issue])
            continue
        if path is None or path.parent != root or not _EXPORT_ZIP.fullmatch(path.name):
            issue = _issue("unsafe-path", job.result_path, reason or "invalid-export-name", job_id=job.id)
            report["issues"].append(issue)
            if dry_run:
                report["result_paths_would_clear"] += 1
                continue
            append_cleanup_issues(settings.cleanup_log, [issue])
            job.result_path = None
            try:
                db.commit()
            except Exception as exc:
                db.rollback()
                failure = _issue("db-update-failed", None, str(exc), job_id=job.id)
                report["issues"].append(failure)
                append_cleanup_issues(settings.cleanup_log, [failure])
                continue
            report["result_paths_cleared"] += 1
            continue
        if dry_run:
            report["would_delete"] += int(path.exists())
            report["result_paths_would_clear"] += 1
            continue
        deleted, delete_reason = remove_checked(
            path, root_dir=settings.exports_dir, data_dir=settings.data_dir
        )
        if delete_reason is not None:
            issue = _issue("delete-failed", str(path), delete_reason, job_id=job.id)
            report["issues"].append(issue)
            append_cleanup_issues(settings.cleanup_log, [issue])
            continue
        report["deleted"] += int(deleted)
        job.result_path = None
        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            issue = _issue("db-update-failed", str(path), str(exc), job_id=job.id)
            report["issues"].append(issue)
            append_cleanup_issues(settings.cleanup_log, [issue])
            continue
        report["result_paths_cleared"] += 1

    for root, pattern, directories in (
        (settings.videos_dir, _VIDEO_TEMP, False),
        (settings.videos_dir, _SUBMISSION_STAGING, False),
        (settings.detection_imports_dir, _VIDEO_TEMP, False),
        (settings.clips_dir, _CLIP_TEMP, False),
        (settings.thumbnails_dir, _THUMB_TEMP, False),
        (settings.exports_dir, _EXPORT_STAGING, True),
        (settings.exports_dir, _EXPORT_TEMP_ZIP, False),
    ):
        _remove_known_temp(
            settings.data_dir,
            root,
            pattern,
            temp_cutoff,
            report,
            directories=directories,
            dry_run=dry_run,
        )

    all_result_jobs = db.query(BackgroundJob).filter(BackgroundJob.result_path.is_not(None)).all()
    referenced_exports = {
        path for job in all_result_jobs if (path := _valid_export_path(job, settings, now)) is not None
    }
    exports_root, exports_reason = trusted_root(settings.data_dir, settings.exports_dir)
    if exports_root is None:
        report["issues"].append(_root_issue(settings.exports_dir, exports_reason or "unsafe"))
    elif exports_root.exists():
        for candidate in exports_root.iterdir():
            path, reason = _safe_path(candidate.name, settings.exports_dir, settings.data_dir)
            if path is None:
                if _EXPORT_ZIP.fullmatch(candidate.name):
                    report["issues"].append(_issue("unsafe-path", str(candidate), reason or "unsafe"))
                continue
            if (
                not _EXPORT_ZIP.fullmatch(path.name)
                or path in referenced_exports
                or not _older_than(path, temp_cutoff)
            ):
                continue
            if dry_run:
                report["would_delete"] += 1
            else:
                deleted, delete_reason = remove_checked(
                    path, root_dir=settings.exports_dir, data_dir=settings.data_dir
                )
                report["deleted"] += int(deleted)
                if delete_reason is not None:
                    report["issues"].append(_issue("delete-failed", str(path), delete_reason))

    dirty_jobs = [
        job
        for job in all_result_jobs
        if job.id not in expired_ids
        and job.status in TERMINAL_JOB_STATUSES
        and _valid_export_path(job, settings, now) is None
    ]
    for job in dirty_jobs:
        if dry_run:
            report["result_paths_would_clear"] += 1
            continue
        job.result_path = None
        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            report["issues"].append(_issue("db-update-failed", None, str(exc), job_id=job.id))
            continue
        report["result_paths_cleared"] += 1

    old_jobs = (
        db.query(BackgroundJob)
        .filter(
            BackgroundJob.status.in_(TERMINAL_JOB_STATUSES),
            BackgroundJob.finished_at.is_not(None),
            BackgroundJob.finished_at < job_cutoff,
            BackgroundJob.result_path.is_(None),
        )
        .all()
    )
    old_jobs = [job for job in old_jobs if job.id != current_job_id]
    if dry_run:
        report["jobs_would_delete"] = len(old_jobs)
    else:
        for job in old_jobs:
            db.delete(job)
        if old_jobs:
            db.commit()
        report["jobs_deleted"] = len(old_jobs)
    return report


class RetentionCleaner:
    """Single-process periodic runner with overlap prevention and prompt shutdown."""

    def __init__(self, session_factory, settings, *, synchronous: bool = False) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.synchronous = synchronous
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.settings.cleanup_enabled:
            return
        if self.synchronous:
            self.run_once()
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="retention-cleaner", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            if self._stop.wait(self.settings.cleanup_interval_seconds):
                return

    def run_once(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        if not self._lock.acquire(blocking=False):
            return None
        job_id: int | None = None
        try:
            with self.session_factory() as db:
                job = BackgroundJob(
                    job_type="cleanup", status="running", progress=0, started_at=now or datetime.utcnow()
                )
                db.add(job)
                db.commit()
                db.refresh(job)
                job_id = job.id
            with self.session_factory() as db:
                report = run_retention_cleanup(db, self.settings, now=now, current_job_id=job_id)
                job = db.get(BackgroundJob, job_id)
                if job is not None:
                    job.status = "succeeded"
                    job.progress = 100
                    job.payload = report
                    job.finished_at = now or datetime.utcnow()
                    db.commit()
                return report
        except Exception as exc:
            if job_id is not None:
                try:
                    with self.session_factory() as db:
                        job = db.get(BackgroundJob, job_id)
                        if job is not None:
                            job.status = "failed"
                            job.error = str(exc)[:2000]
                            job.finished_at = now or datetime.utcnow()
                            db.commit()
                except Exception:
                    pass
            return None
        finally:
            self._lock.release()
