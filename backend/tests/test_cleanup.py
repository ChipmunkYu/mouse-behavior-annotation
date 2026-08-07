from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import database as db_mod
from app.cleanup import RetentionCleaner, run_retention_cleanup
from app.cleanup_io import append_cleanup_issues
from app.config import Settings
from app.models import Annotation, BackgroundJob, Clip, Video


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(
        env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'cleanup.db').as_posix()}",
        cleanup_enabled=False,
    )
    for path in (
        settings.videos_dir,
        settings.clips_dir,
        settings.thumbnails_dir,
        settings.exports_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    db_mod.configure_engine(settings.resolved_database_url)
    db_mod.ensure_schema(settings.resolved_database_url)
    return settings


def _age(path: Path, now: datetime, hours: int) -> None:
    stamp = (now - timedelta(hours=hours)).timestamp()
    os.utime(path, (stamp, stamp))


def test_expired_export_boundary_and_out_of_bounds(tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 1, 2, 12)
    expired = settings.exports_dir / "export_project_1_1.zip"
    boundary = settings.exports_dir / "export_project_1_2.zip"
    outside = tmp_path / "outside.zip"
    for path in (expired, boundary, outside):
        path.write_bytes(b"zip")
    with db_mod.SessionLocal() as db:
        jobs = [
            BackgroundJob(job_type="export", status="succeeded", result_path=expired.name, expires_at=now - timedelta(seconds=1)),
            BackgroundJob(job_type="export", status="succeeded", result_path=boundary.name, expires_at=now),
            BackgroundJob(job_type="export", status="succeeded", result_path=str(outside), expires_at=now - timedelta(seconds=1)),
        ]
        db.add_all(jobs)
        db.commit()
        run_retention_cleanup(db, settings, now=now)
        assert db.get(BackgroundJob, jobs[0].id).result_path is None
        assert db.get(BackgroundJob, jobs[1].id).result_path is None
        assert db.get(BackgroundJob, jobs[2].id).result_path is None
    assert not expired.exists()
    assert not boundary.exists()
    assert outside.exists()
    assert "unsafe-path" in settings.cleanup_log.read_text(encoding="utf-8")


def test_known_temps_orphan_and_original_video_retention(tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 1, 2, 12)
    old_temp = settings.videos_dir / ("a" * 32 + ".part")
    boundary = settings.videos_dir / ("b" * 32 + ".part")
    original = settings.videos_dir / "original.mp4"
    unknown = settings.clips_dir / "random.part"
    media_temp = settings.clips_dir / (".clip_1_rev1." + "c" * 32 + ".mp4.part")
    orphan = settings.exports_dir / "export_project_9_8.zip"
    referenced = settings.exports_dir / "export_project_9_9.zip"
    staging = settings.exports_dir / ".export_9_8.staging"
    staging.mkdir()
    for path in (old_temp, boundary, original, unknown, media_temp, orphan, referenced):
        path.write_bytes(b"x")
    for path in (old_temp, media_temp, orphan, staging):
        _age(path, now, 25)
    _age(boundary, now, 24)
    with db_mod.SessionLocal() as db:
        db.add(BackgroundJob(job_type="export", status="succeeded", result_path=referenced.name))
        db.commit()
        run_retention_cleanup(db, settings, now=now)
    assert not old_temp.exists() and not media_temp.exists() and not orphan.exists()
    assert not staging.exists()
    assert boundary.exists() and original.exists() and unknown.exists() and referenced.exists()


def test_issue_retry_requires_program_name_and_db_proof(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    now = datetime(2026, 1, 2, 12)
    removable = settings.clips_dir / "clip_10_rev1.mp4"
    current = settings.clips_dir / "clip_11_rev2.mp4"
    referenced = settings.clips_dir / "clip_12_rev1.mp4"
    invalid_name = settings.clips_dir / "orphan.mp4"
    retry = settings.thumbnails_dir / "clip_13_rev1.jpg"
    outside = tmp_path / "clip_14_rev1.mp4"
    for path in (removable, current, referenced, invalid_name, retry, outside):
        path.write_bytes(b"x")
    entries = [
        json.dumps({"kind": "delete-failed", "path": str(removable)}),
        json.dumps({"kind": "delete-failed", "path": str(current)}),
        json.dumps({"kind": "delete-failed", "path": str(outside)}),
        "not-json",
        json.dumps({"kind": "delete-failed", "path": str(referenced)}),
        json.dumps({"kind": "delete-failed", "path": str(invalid_name)}),
        json.dumps({"kind": "delete-failed", "path": str(retry)}),
    ]
    settings.cleanup_log.write_text("\n".join(entries) + "\n", encoding="utf-8")
    original_unlink = Path.unlink

    def fail_retry(self, *args, **kwargs):
        if self == retry:
            raise PermissionError("busy")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_retry)
    with db_mod.SessionLocal() as db:
        # Disable FK checks only for this focused cleanup unit fixture.
        db.connection().exec_driver_sql("PRAGMA foreign_keys=OFF")
        db.add(Video(id=20, project_id=1, filename="v.mp4", annotation_revision=2))
        db.add(
            Annotation(
                id=11,
                video_id=20,
                annotator_id=1,
                category_id=1,
                start_time=0,
                end_time=1,
                start_frame=0,
                end_frame=1,
            )
        )
        db.add(Clip(project_id=1, annotation_id=12, source_revision=1, status="failed", clip_path=referenced.name))
        db.commit()
        run_retention_cleanup(db, settings, now=now)
    remaining = settings.cleanup_log.read_text(encoding="utf-8")
    retained_paths = {
        json.loads(line).get("path")
        for line in remaining.splitlines()
        if line.startswith("{")
    }
    assert not removable.exists()
    assert current.exists() and referenced.exists() and invalid_name.exists()
    assert retry.exists() and outside.exists()
    assert "not-json" in remaining
    assert {str(current), str(referenced), str(invalid_name), str(retry), str(outside)} <= retained_paths
    resolved = [
        json.loads(line)
        for line in remaining.splitlines()
        if line.startswith("{") and json.loads(line).get("path") == str(removable)
    ]
    assert resolved[0]["cleanup_status"] == "resolved"


def test_job_retention_and_dry_run(tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 2, 1)
    old = now - timedelta(days=31)
    with db_mod.SessionLocal() as db:
        rows = [
            BackgroundJob(job_type="media", status="succeeded", finished_at=old),
            BackgroundJob(job_type="media", status="running", finished_at=old),
            BackgroundJob(job_type="media", status="failed", finished_at=old, result_path="held.zip"),
            BackgroundJob(job_type="media", status="failed", finished_at=now - timedelta(days=30)),
        ]
        db.add_all(rows)
        db.commit()
        old_id = rows[0].id
        report = run_retention_cleanup(db, settings, now=now, dry_run=True)
        assert report["jobs_would_delete"] == 1 and db.get(BackgroundJob, old_id) is not None
        run_retention_cleanup(db, settings, now=now)
        assert db.get(BackgroundJob, old_id) is None
        assert db.get(BackgroundJob, rows[1].id) is not None
        assert db.get(BackgroundJob, rows[2].id) is None
        assert db.get(BackgroundJob, rows[3].id) is not None


def test_settings_reject_invalid_cleanup_retention_values():
    for field in (
        "export_retention_days",
        "temp_retention_hours",
        "job_retention_days",
    ):
        with pytest.raises(ValidationError):
            Settings(**{field: -1})
    with pytest.raises(ValidationError):
        Settings(cleanup_interval_seconds=0)


def _symlink(target: Path, link: Path, *, directory: bool = True) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")


def test_symlink_roots_refuse_cleanup_and_preserve_external_targets(tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 2, 1)
    external_clips = tmp_path / "external-clips"
    external_exports = tmp_path / "external-exports"
    external_clips.mkdir()
    external_exports.mkdir()
    clip_temp = external_clips / (".clip_1_rev1." + "a" * 32 + ".mp4.part")
    logged_media = external_clips / "clip_1_rev1.mp4"
    export = external_exports / "export_project_1_1.zip"
    for path in (clip_temp, logged_media, export):
        path.write_bytes(b"keep")
        _age(path, now, 25)
    settings.clips_dir.rmdir()
    settings.exports_dir.rmdir()
    _symlink(external_clips, settings.clips_dir)
    _symlink(external_exports, settings.exports_dir)
    settings.cleanup_log.write_text(
        json.dumps({"kind": "delete-failed", "path": str(settings.clips_dir / logged_media.name)}) + "\n",
        encoding="utf-8",
    )
    with db_mod.SessionLocal() as db:
        db.add(
            BackgroundJob(
                job_type="export",
                status="succeeded",
                result_path=export.name,
                expires_at=now,
            )
        )
        db.commit()
        report = run_retention_cleanup(db, settings, now=now)
    assert clip_temp.exists() and logged_media.exists() and export.exists()
    assert any(issue["kind"] == "untrusted-root" for issue in report["issues"])
    assert "cleanup_status" not in settings.cleanup_log.read_text(encoding="utf-8")


def test_staging_internal_symlink_is_rejected(tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 2, 1)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    staging = settings.exports_dir / ".export_1_1.staging"
    staging.mkdir()
    _symlink(outside, staging / "linked.txt", directory=False)
    _age(staging, now, 25)
    with db_mod.SessionLocal() as db:
        report = run_retention_cleanup(db, settings, now=now)
    assert staging.exists() and outside.read_text(encoding="utf-8") == "keep"
    assert any(issue["reason"] == "internal-symlink" for issue in report["issues"])


def test_staging_directory_symlink_is_rejected(tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 2, 1)
    outside = tmp_path / "external-staging"
    outside.mkdir()
    payload = outside / "payload.txt"
    payload.write_text("keep", encoding="utf-8")
    staging = settings.exports_dir / ".export_1_1.staging"
    _symlink(outside, staging)
    _age(staging, now, 25)
    with db_mod.SessionLocal() as db:
        report = run_retention_cleanup(db, settings, now=now)
    assert staging.is_symlink() and payload.read_text(encoding="utf-8") == "keep"
    assert any(issue["reason"] == "symlink" for issue in report["issues"])


def test_terminal_dirty_jobs_do_not_protect_orphan_exports(tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 2, 1)
    valid = settings.exports_dir / "export_project_1_1.zip"
    dirty_export = settings.exports_dir / "export_project_1_2.zip"
    dirty_non_export = settings.exports_dir / "export_project_1_3.zip"
    for path in (valid, dirty_export, dirty_non_export):
        path.write_bytes(b"zip")
        _age(path, now, 25)
    with db_mod.SessionLocal() as db:
        rows = [
            BackgroundJob(job_type="export", status="succeeded", result_path=valid.name, expires_at=now + timedelta(days=1)),
            BackgroundJob(job_type="export", status="failed", result_path=dirty_export.name, finished_at=now - timedelta(days=31)),
            BackgroundJob(job_type="media", status="succeeded", result_path=dirty_non_export.name, finished_at=now - timedelta(days=31)),
        ]
        db.add_all(rows)
        db.commit()
        run_retention_cleanup(db, settings, now=now)
        assert db.get(BackgroundJob, rows[0].id).result_path == valid.name
        assert db.get(BackgroundJob, rows[1].id) is None
        assert db.get(BackgroundJob, rows[2].id) is None
    assert valid.exists()
    assert not dirty_export.exists() and not dirty_non_export.exists()


def test_expired_export_commit_failure_recovers_next_pass(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    now = datetime(2026, 2, 1)
    export = settings.exports_dir / "export_project_1_1.zip"
    export.write_bytes(b"zip")
    with db_mod.SessionLocal() as db:
        job = BackgroundJob(job_type="export", status="succeeded", result_path=export.name, expires_at=now)
        db.add(job)
        db.commit()
        job_id = job.id
        real_commit = db.commit
        calls = 0

        def fail_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("commit failed")
            real_commit()

        monkeypatch.setattr(db, "commit", fail_once)
        run_retention_cleanup(db, settings, now=now)
        assert not export.exists()
        assert db.get(BackgroundJob, job_id).result_path == export.name
        run_retention_cleanup(db, settings, now=now)
        assert db.get(BackgroundJob, job_id).result_path is None


def test_comprehensive_dry_run_has_zero_side_effects(tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 2, 1)
    export = settings.exports_dir / "export_project_1_1.zip"
    temp = settings.videos_dir / ("a" * 32 + ".part")
    media = settings.clips_dir / "clip_99_rev1.mp4"
    for path in (export, temp, media):
        path.write_bytes(b"x")
        _age(path, now, 25)
    issue = json.dumps({"kind": "delete-failed", "path": str(media)}) + "\n"
    settings.cleanup_log.write_text(issue, encoding="utf-8")
    with db_mod.SessionLocal() as db:
        rows = [
            BackgroundJob(job_type="export", status="succeeded", result_path=export.name, expires_at=now),
            BackgroundJob(job_type="media", status="failed", result_path="dirty.zip", finished_at=now - timedelta(days=31)),
            BackgroundJob(job_type="media", status="failed", finished_at=now - timedelta(days=31)),
        ]
        db.add_all(rows)
        db.commit()
        ids = [row.id for row in rows]
        report = run_retention_cleanup(db, settings, now=now, dry_run=True)
        assert [db.get(BackgroundJob, row_id).result_path for row_id in ids] == [export.name, "dirty.zip", None]
        assert all(db.get(BackgroundJob, row_id) is not None for row_id in ids)
    assert export.exists() and temp.exists() and media.exists()
    assert settings.cleanup_log.read_text(encoding="utf-8") == issue
    assert report["deleted"] == report["result_paths_cleared"] == report["jobs_deleted"] == 0


def test_cleanup_log_concurrent_append_is_not_lost(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    media = settings.clips_dir / "clip_90_rev1.mp4"
    media.write_bytes(b"x")
    settings.cleanup_log.write_text(
        json.dumps({"kind": "delete-failed", "path": str(media)}) + "\n",
        encoding="utf-8",
    )
    entered = threading.Event()
    release = threading.Event()
    import app.cleanup as cleanup_module

    real_remove = cleanup_module.remove_checked

    def blocked_remove(path, **kwargs):
        entered.set()
        release.wait(timeout=2)
        return real_remove(path, **kwargs)

    monkeypatch.setattr(cleanup_module, "remove_checked", blocked_remove)

    def consume():
        with db_mod.SessionLocal() as db:
            run_retention_cleanup(db, settings)

    consumer = threading.Thread(target=consume)
    consumer.start()
    assert entered.wait(timeout=2)
    appender = threading.Thread(
        target=append_cleanup_issues,
        args=(settings.cleanup_log, [{"kind": "later", "path": "audit"}]),
    )
    appender.start()
    release.set()
    consumer.join(timeout=2)
    appender.join(timeout=2)
    lines = settings.cleanup_log.read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line).get("kind") == "later" for line in lines)


def test_worker_records_success_failure_and_shutdown(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.cleanup_enabled = True
    cleaner = RetentionCleaner(db_mod.SessionLocal, settings, synchronous=True)
    assert cleaner.run_once() is not None
    with db_mod.SessionLocal() as db:
        assert db.query(BackgroundJob).filter_by(job_type="cleanup", status="succeeded").count() == 1

    import app.cleanup as cleanup_module

    monkeypatch.setattr(cleanup_module, "run_retention_cleanup", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cleaner.run_once() is None
    with db_mod.SessionLocal() as db:
        assert db.query(BackgroundJob).filter_by(job_type="cleanup", status="failed").count() == 1

    settings.cleanup_interval_seconds = 3600
    background = RetentionCleaner(db_mod.SessionLocal, settings)
    monkeypatch.setattr(background, "run_once", lambda **kwargs: {})
    background.start()
    started = time.monotonic()
    background.shutdown()
    assert time.monotonic() - started < 1
