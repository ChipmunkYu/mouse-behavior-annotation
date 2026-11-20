"""Context residency, durable scheduling, frozen metadata and streaming pressure."""
import errno
import json
import os
import zipfile
import threading
from pathlib import Path

import pytest

from app import context_cache as cache, export_jobs
from app.config import Settings
from app.models import BackgroundJob, Project, Video
from app.schemas import JobOut
from tests.test_project_export import _approved, _archive, _export
from tests.test_reviews import _annotate_with_mouse, _review, _submit
from tests.conftest import auth_headers


@pytest.fixture(autouse=True)
def _enable_prewarm(request):
    if "media_ctx" in request.fixturenames:
        request.getfixturevalue("media_ctx").app.state.settings.context_prewarm_enabled = True


def _status(ctx, project, headers):
    response = ctx.client.get(f"/api/projects/{project['id']}/export/status", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _cold(ctx):
    ctx.app.state.settings.context_prewarm_enabled = False


def test_hash_memo_retains_1025_entry_working_set(tmp_path, monkeypatch):
    paths = [tmp_path / f"{i}-{name}" for i in range(1025) for name in sorted(cache.FILES)]
    for path in paths:
        path.write_bytes(b"small")
    for path in paths:
        cache.file_hash(path)
    def forbidden(*args, **kwargs):
        pytest.fail("second scan must not open/hash resident material")
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(cache.hashlib, "sha256", forbidden)
    for path in paths:
        assert cache.file_hash(path)


def test_failed_claim_preserves_schedule_during_drain(media_ctx, monkeypatch):
    ctx = media_ctx
    worker = export_jobs.ExportWorker(ctx.processor, ctx.session_factory,
        ctx.app.state.settings.model_copy(update={"media_synchronous": False}))
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    consumed = []
    with ctx.session_factory() as db:
        first = BackgroundJob(job_type="export", status="queued", payload={}, dedupe_key="claim-first")
        db.add(first); db.commit(); first_id = first.id
    def run(job_id):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
            return False
        with ctx.session_factory() as db:
            db.get(BackgroundJob, job_id).status = "succeeded"
            db.commit()
        consumed.append(job_id)
        if len(consumed) == 2:
            done.set()
    monkeypatch.setattr(worker, "_run_job", run)
    try:
        worker.schedule(first_id)
        assert entered.wait(10)
        with ctx.session_factory() as db:
            second = BackgroundJob(job_type="export", status="queued", payload={}, dedupe_key="claim-second")
            db.add(second); db.commit(); second_id = second.id
        worker.schedule(second_id)
        release.set()
        assert done.wait(10)
        assert consumed == [first_id, second_id]
    finally:
        release.set()
        worker.shutdown()
        worker._executor.shutdown(wait=True)


def test_preparation_batch_queries_and_ready_queued_state(media_ctx, monkeypatch):
    from sqlalchemy import event
    ctx = media_ctx
    _cold(ctx)
    headers, project, *_ = _approved(ctx, two_categories=True)
    with ctx.session_factory() as db:
        rows = export_jobs.asset_rows(db, project["id"], None)
        cache.enqueue_prewarm(db, ctx.app.state.settings.model_copy(update={"context_prewarm_enabled": True}), rows)
        db.commit()
        calls = []
        def count(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                calls.append(statement)
        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", count)
        try:
            monkeypatch.setattr(cache, "lookup", lambda settings, video, key: None)
            cache.preparation(db, ctx.app.state.settings, rows)
            baseline = len(calls)
            calls.clear()
            cache.preparation(db, ctx.app.state.settings, rows * 20)
            assert len(calls) <= baseline
            seen = []
            def lookup(*args):
                seen.append(1)
                return Path("resident") if len(seen) == 1 else None
            monkeypatch.setattr(cache, "lookup", lookup)
            assert cache.preparation(db, ctx.app.state.settings, rows)["state"] == "pending"
        finally:
            event.remove(engine, "before_cursor_execute", count)


def test_publication_prewarm_then_hot_export_no_render_tracks_or_copy(media_ctx, monkeypatch):
    ctx = media_ctx
    headers, project, *_ = _approved(ctx)
    assert _status(ctx, project, headers)["preparation"]["state"] == "ready"
    worker = ctx.app.state.export_worker
    def forbidden(*args, **kwargs):
        raise AssertionError("hot export must not regenerate or copy source")
    monkeypatch.setattr(ctx.processor, "render_clip", forbidden)
    monkeypatch.setattr(worker, "_write_tracks_range", forbidden)
    monkeypatch.setattr(export_jobs, "stage_submission_input", forbidden)
    job = _export(ctx, project, headers)
    assert job["status"] == "succeeded" and job["phase"] == "completed"
    assert "payload" not in job
    with zipfile.ZipFile(_archive(ctx, job)) as archive:
        assert len(archive.namelist()) == 4


def test_heuristic_prewarm_and_hot_export_keep_fixed_media_and_event(media_ctx, monkeypatch):
    from tests.test_heuristic_judgment import setup_project, upload, save, submit, approve, files
    ctx = media_ctx
    headers, pid, categories = setup_project(ctx)
    _, completed = upload(ctx, headers, pid)
    base = f"/api/projects/{pid}/videos/{completed['video_id']}"
    save(ctx, base, headers, categories[0])
    submit(ctx, base, headers)
    approve(ctx, base, headers)
    assert not ctx.processor.clip_calls
    status = ctx.client.get(f"/api/projects/{pid}/export/status?source=heuristic", headers=headers).json()
    assert status["preparation"]["state"] == "ready"
    def forbidden(*args, **kwargs):
        pytest.fail("hot heuristic export must not copy source or regenerate context")
    monkeypatch.setattr(export_jobs, "stage_submission_input", forbidden)
    monkeypatch.setattr(ctx.app.state.export_worker, "_write_tracks_range", forbidden)
    result = ctx.client.post(f"/api/projects/{pid}/export", json={"source": "heuristic"}, headers=headers)
    assert result.status_code == 201 and result.json()["status"] == "succeeded", result.text
    with zipfile.ZipFile(_archive(ctx, result.json())) as archive:
        docs = {name.rsplit("/", 1)[1]: archive.read(name) for name in archive.namelist()}
        assert docs["clip.mp4"] == files()["video"]
        assert json.loads(docs["annotation.json"])["frame_range"] == {"start": 2, "end": 7}
        assert len(json.loads(docs["tracks.json"])) == 10
        assert json.loads(docs["metadata.json"])["source"]["annotation"]["start_frame"] == 102


def test_cold_group_one_copy_and_one_item_peak(media_ctx, monkeypatch):
    ctx = media_ctx
    _cold(ctx)
    headers, project, *_ = _approved(ctx, two_categories=True)
    settings = ctx.app.state.settings
    settings.context_cache_max_bytes = 0
    real_stage = export_jobs.stage_submission_input
    copies, peak_sources, peak_scratch = [], [], []
    def stage(*args, **kwargs):
        result = real_stage(*args, **kwargs)
        copies.append(result)
        peak_sources.append(len(list(settings.videos_dir.glob("*.staging"))))
        return result
    monkeypatch.setattr(export_jobs, "stage_submission_input", stage)
    original = ctx.app.state.export_worker._write_item
    def write(*args, **kwargs):
        peak_scratch.append(len(list(cache.cache_root(settings).glob("video-*/.job-*"))))
        return original(*args, **kwargs)
    monkeypatch.setattr(ctx.app.state.export_worker, "_write_item", write)
    job = _export(ctx, project, headers)
    assert job["status"] == "succeeded"
    assert len(copies) == max(peak_sources) == max(peak_scratch) == 1
    assert not list(settings.videos_dir.glob("*.staging"))
    assert not list(cache.cache_root(settings).glob("video-*/*"))


def test_canonical_owners_across_submissions_share_source_copy(media_ctx, monkeypatch):
    ctx = media_ctx
    _cold(ctx)
    headers, project, categories, video, _ = _approved(ctx)
    state = ctx.client.get(f"/api/projects/{project['id']}/videos/{video['id']}/review-state", headers=headers).json()
    response = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/submissions/{state['submission_id']}/reopen",
        json={"reason": "add category"}, headers=auth_headers(ctx.client, "reviewer1", "pw123"))
    assert response.status_code == 200, response.text
    _annotate_with_mouse(ctx, headers, project, video, categories[1]["id"], start_time=.08, mouse_ids=[1])
    assert _submit(ctx, headers, project, video).status_code == 200
    ctx.app.state.settings.context_prewarm_enabled = True
    assert _review(ctx, auth_headers(ctx.client, "reviewer1", "pw123"), project, video, "approved").status_code == 200
    with ctx.session_factory() as db:
        rows = export_jobs.asset_rows(db, project["id"], None)
        assert len({s.id for a, s, c in rows}) == 2
        assert len({cache.source_group_identity(s) for a, s, c in rows}) == 1
        jobs = db.query(BackgroundJob).filter_by(job_type=cache.JOB_TYPE_PREWARM).all()
        assert {job.payload["context"]["submission_id"] for job in jobs} == {s.id for a, s, c in rows}
        assert all(job.status == "succeeded" for job in jobs)
    cache.evict_cache(ctx.app.state.settings, target_bytes=0)
    copies = []
    original = export_jobs.stage_submission_input
    def stage(*args, **kwargs):
        copies.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(export_jobs, "stage_submission_input", stage)
    assert _export(ctx, project, headers)["status"] == "succeeded"
    assert len(copies) == 1
    worker = ctx.app.state.export_worker
    real_verify, verifies = worker._verify_hot_source, []
    def verify(submission):
        verifies.append(1)
        return real_verify(submission)
    def forbidden(*args, **kwargs):
        pytest.fail("hot same-source group must not render or generate tracks")
    monkeypatch.setattr(worker, "_verify_hot_source", verify)
    monkeypatch.setattr(worker, "_write_tracks_range", forbidden)
    monkeypatch.setattr(ctx.processor, "render_clip", forbidden)
    assert _export(ctx, project, headers)["status"] == "succeeded"
    assert len(copies) == len(verifies) == 1


def test_prewarm_reused_canonical_owners_are_enqueued(media_ctx, monkeypatch):
    ctx = media_ctx
    _cold(ctx)
    headers, project, *_ = _approved(ctx)
    settings = ctx.app.state.settings
    settings.context_prewarm_enabled = True
    with ctx.session_factory() as db:
        rows = export_jobs.asset_rows(db, project["id"], None)
        ids = cache.enqueue_prewarm(db, settings, rows)
        db.rollback()
        assert db.query(BackgroundJob).filter_by(job_type=cache.JOB_TYPE_PREWARM).count() == 0
        ids = cache.enqueue_prewarm(db, settings, rows)
        db.commit()
    assert len(ids) == 1
    ctx.app.state.export_worker.wake()
    assert _status(ctx, project, headers)["preparation"]["state"] == "ready"
    with ctx.session_factory() as db:
        job = db.get(BackgroundJob, ids[0])
        job.status = "failed"
        db.commit()
        assert cache.enqueue_prewarm(db, settings, export_jobs.asset_rows(db, project["id"], None)) == ids
        assert job.status == "queued"
        db.rollback()


@pytest.mark.parametrize("limit", [0, 1, 4000])
def test_zip_can_exceed_cache_capacity(media_ctx, limit):
    ctx = media_ctx
    _cold(ctx)
    headers, project, *_ = _approved(ctx, two_categories=True)
    settings = ctx.app.state.settings
    settings.context_cache_max_bytes = limit
    job = _export(ctx, project, headers)
    assert job["status"] == "succeeded"
    assert cache.evict_cache(settings)["total_bytes"] <= limit
    if limit <= 1:
        assert _archive(ctx, job).stat().st_size > limit
    with zipfile.ZipFile(_archive(ctx, job)) as archive:
        assert len(archive.namelist()) == 8
    assert not list(cache.cache_root(settings).glob("video-*/.job-*"))


def test_cache_tamper_not_ready_and_cold_repair(media_ctx):
    ctx = media_ctx
    headers, project, *_ = _approved(ctx)
    settings = ctx.app.state.settings
    target = next(cache.cache_root(settings).glob("video-*/*/item/clip.mp4"))
    old_stat = target.stat()
    data = target.read_bytes()
    target.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    os.utime(target, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    assert _status(ctx, project, headers)["preparation"]["ready_count"] == 0
    before = len(ctx.processor.clip_calls)
    job = _export(ctx, project, headers)
    assert job["status"] == "succeeded"
    assert len(ctx.processor.clip_calls) == before + 1
    assert _status(ctx, project, headers)["preparation"]["state"] == "ready"


def test_distance_frozen_and_cache_not_retranscoded(media_ctx, monkeypatch):
    ctx = media_ctx
    headers, project, _, video, _ = _approved(ctx)
    worker = ctx.app.state.export_worker
    with ctx.session_factory() as db:
        v = db.get(Video, video["id"])
        v.distance_calibration = {"cm_per_pixel": 0.5}
        db.commit()
        job = export_jobs.enqueue_export_job(db, db.get(Project, project["id"]), None)
        job_id = job.id
        v.distance_calibration = {"cm_per_pixel": 0.9}
        db.commit()
    def forbidden(*args, **kwargs):
        raise AssertionError("distance must not regenerate context")
    monkeypatch.setattr(ctx.processor, "render_clip", forbidden)
    worker._run_job(job_id)
    with ctx.session_factory() as db:
        job = db.get(BackgroundJob, job_id)
        assert job.status == "succeeded"
        path = ctx.app.state.settings.exports_dir / job.result_path
    with zipfile.ZipFile(path) as archive:
        metadata = json.loads(archive.read(next(n for n in archive.namelist() if n.endswith("metadata.json"))))
        assert metadata["distance_scale"]["cm_per_pixel"] == 0.5


def test_legacy_context_extension_once(media_ctx):
    ctx = media_ctx
    headers, project, _, video, _ = _approved(ctx)
    with ctx.session_factory() as db:
        job = export_jobs.enqueue_export_job(db, db.get(Project, project["id"]), None)
        payload = json.loads(json.dumps(job.payload))
        payload.pop("preparation_version")
        for ref in payload["refs"]:
            ref.pop("context"); ref.pop("distance_scale")
        job.payload = payload
        db.get(Video, video["id"]).distance_calibration = {"cm_per_pixel": .25}
        job.status = "running"
        db.commit()
        ctx.app.state.export_worker._extend_legacy_payload(db, job)
        frozen = json.loads(json.dumps(job.payload))
        db.get(Video, video["id"]).distance_calibration = {"cm_per_pixel": .75}
        db.commit()
        ctx.app.state.export_worker._extend_legacy_payload(db, job)
        assert job.payload == frozen
        assert frozen["refs"][0]["distance_scale"]["cm_per_pixel"] == .25


@pytest.mark.parametrize("failure_at", ["source", "source_chunk", "zip"])
def test_enospc_no_publication_and_safe_code(media_ctx, monkeypatch, failure_at):
    ctx = media_ctx
    _cold(ctx)
    headers, project, *_ = _approved(ctx)
    real_check = cache.check_space
    source_checks = []
    def check(settings, path, additional=0):
        if Path(path) == settings.videos_dir.resolve():
            source_checks.append(additional)
        if (failure_at == "source" and Path(path) == settings.videos_dir.resolve()) or (
                failure_at == "source_chunk" and len(source_checks) == 2) or (
                failure_at == "zip" and Path(path) == settings.exports_dir):
            raise OSError(errno.ENOSPC, "private /internal/source.mp4")
        return real_check(settings, path, additional)
    monkeypatch.setattr(cache, "check_space", check)
    job = _export(ctx, project, headers)
    assert job["status"] == "failed"
    assert job["error"] == "processing_failed" and job["error_code"] == "storage_insufficient"
    assert "/internal/" not in json.dumps(job)
    assert not list(ctx.app.state.settings.exports_dir.iterdir())
    assert not list(ctx.app.state.settings.videos_dir.glob("*.staging"))


def test_lost_wakeup_and_running_startup_recovery(media_ctx, monkeypatch):
    ctx = media_ctx
    worker = ctx.app.state.export_worker
    real_wake = worker.wake
    monkeypatch.setattr(worker, "wake", lambda: None)
    headers, project, *_ = _approved(ctx)
    assert _status(ctx, project, headers)["preparation"]["state"] == "pending"
    with ctx.session_factory() as db:
        job = db.query(BackgroundJob).filter_by(job_type=cache.JOB_TYPE_PREWARM).one()
        job.status, job.attempts = "running", 1
        db.commit()
    monkeypatch.setattr(worker, "wake", real_wake)
    worker.start()
    assert _status(ctx, project, headers)["preparation"]["state"] == "ready"


def test_publication_rollback_does_not_enqueue(media_ctx, monkeypatch):
    ctx = media_ctx
    from app.routers import reviews
    real_enqueue = reviews.enqueue_prewarm
    def fail_after_enqueue(*args, **kwargs):
        ids = real_enqueue(*args, **kwargs)
        assert ids
        raise RuntimeError("publication rollback")
    monkeypatch.setattr(reviews, "enqueue_prewarm", fail_after_enqueue)
    with pytest.raises(RuntimeError, match="publication rollback"):
        _approved(ctx)
    with ctx.session_factory() as db:
        assert db.query(BackgroundJob).filter_by(job_type=cache.JOB_TYPE_PREWARM).count() == 0


def test_durable_drain_export_priority_between_prewarm_entries(media_ctx, monkeypatch):
    ctx = media_ctx
    worker = ctx.app.state.export_worker
    with ctx.session_factory() as db:
        db.add_all([BackgroundJob(job_type=kind, status="queued", payload={}, dedupe_key=f"test-{i}")
                    for i, kind in enumerate([cache.JOB_TYPE_PREWARM, cache.JOB_TYPE_PREWARM, "export"])])
        db.commit()
    order = []
    def run(job_id):
        with ctx.session_factory() as db:
            job = db.get(BackgroundJob, job_id)
            order.append(job.job_type)
            job.status = "succeeded"
            if len(order) == 2:
                db.add(BackgroundJob(job_type="export", status="queued", payload={}, dedupe_key="late-export"))
            db.commit()
    monkeypatch.setattr(worker, "_run_job", run)
    worker.wake()
    assert order == ["export", cache.JOB_TYPE_PREWARM, "export", cache.JOB_TYPE_PREWARM]


def test_real_async_drain_does_not_queue_bulk_futures(media_ctx, monkeypatch):
    ctx = media_ctx
    settings = ctx.app.state.settings.model_copy(update={"media_synchronous": False})
    worker = export_jobs.ExportWorker(ctx.processor, ctx.session_factory, settings)
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    order = []
    with ctx.session_factory() as db:
        db.add_all([BackgroundJob(job_type=cache.JOB_TYPE_PREWARM, status="queued", payload={},
                                 dedupe_key=f"async-prewarm-{i}") for i in range(2)])
        db.commit()
    def run(job_id):
        with ctx.session_factory() as db:
            job = db.get(BackgroundJob, job_id)
            kind = job.job_type
            job.status = "succeeded"
            db.commit()
        order.append(kind)
        if len(order) == 1:
            entered.set()
            assert release.wait(10)
        if len(order) == 3:
            done.set()
    monkeypatch.setattr(worker, "_run_job", run)
    try:
        worker.wake()
        assert entered.wait(10)
        with ctx.session_factory() as db:
            job = BackgroundJob(job_type="export", status="queued", payload={}, dedupe_key="async-export")
            db.add(job); db.commit(); job_id = job.id
        worker.schedule(job_id)
        assert len(worker._futures) == 1
        release.set()
        assert done.wait(10)
        assert order == [cache.JOB_TYPE_PREWARM, "export", cache.JOB_TYPE_PREWARM]
    finally:
        release.set()
        worker._executor.shutdown(wait=True)


def test_capacity_deferred_not_retried_and_zero_disables_prewarm(media_ctx):
    ctx = media_ctx
    settings = ctx.app.state.settings
    settings.context_cache_max_bytes = 1
    headers, project, *_ = _approved(ctx)
    assert _status(ctx, project, headers)["preparation"]["deferred_count"] == 1
    calls = len(ctx.processor.clip_calls)
    ctx.app.state.export_worker.start()
    assert len(ctx.processor.clip_calls) == calls
    settings.context_cache_max_bytes = 0
    assert _export(ctx, project, headers)["status"] == "succeeded"


@pytest.mark.parametrize("mode", ["max0", "disabled", "legacy"])
def test_nonzero_unprepared_without_jobs_is_partial_but_exports(media_ctx, mode):
    ctx = media_ctx
    settings = ctx.app.state.settings
    if mode == "max0":
        settings.context_cache_max_bytes = 0
    else:
        settings.context_prewarm_enabled = False
    headers, project, *_ = _approved(ctx)
    if mode == "legacy":
        settings.context_prewarm_enabled = True
    with ctx.session_factory() as db:
        assert db.query(BackgroundJob).filter_by(job_type=cache.JOB_TYPE_PREWARM).count() == 0
        assert cache.preparation(db, settings, []) == dict(
            state="empty", total_count=0, ready_count=0, failed_count=0, deferred_count=0)
    assert _status(ctx, project, headers)["preparation"] == dict(
        state="partial", total_count=1, ready_count=0, failed_count=0, deferred_count=0)
    assert _export(ctx, project, headers)["status"] == "succeeded"


def test_lru_pin_and_status_reads_hash_only_once(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, context_cache_max_bytes=100000)
    key = "a" * 64
    def build(target):
        target.mkdir()
        for name in cache.FILES:
            (target / name).write_bytes(b"test")
    with cache.material(settings, 1, key, 123, build):
        assert key in cache.PINNED_KEYS
        assert cache.evict_cache(settings, target_bytes=0)["removed_keys"] == []
    # All file hashes were bound to the builder scratch paths, so first lookup
    # hashes the final identity. Later status reads only stat the same identities.
    assert cache.lookup(settings, 1, key)
    real_open = Path.open
    def guard(path, *args, **kwargs):
        if path.name in cache.FILES:
            raise AssertionError("unchanged status must not rehash material")
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guard)
    assert cache.lookup(settings, 1, key)
    assert cache.evict_cache(settings, target_bytes=0)["removed_keys"] == [key]
    assert cache.lookup(settings, 1, key) is None


def test_legacy_public_job_phase_unknown_and_errors_sanitized():
    from datetime import datetime
    result = JobOut.model_validate(dict(id=1, job_type="export", status="running", progress=90,
        error="Worker cleanup incomplete: /private/file", created_at=datetime.utcnow()))
    assert result.phase is None and result.error_code == "cleanup_incomplete"
    assert result.error == "processing_failed"
    assert Settings().context_prewarm_enabled is True


def test_evicted_ready_prep_becomes_partial_and_hot_source_change_rejected(media_ctx):
    ctx = media_ctx
    headers, project, *_ = _approved(ctx)
    settings = ctx.app.state.settings
    assert cache.evict_cache(settings, target_bytes=0)["removed_keys"]
    assert _status(ctx, project, headers)["preparation"]["state"] == "partial"
    assert _export(ctx, project, headers)["status"] == "succeeded"
    with ctx.session_factory() as db:
        row = export_jobs.asset_rows(db, project["id"], None)[0]
        source = settings.videos_dir / row[1].source_storage_key
    source.write_bytes(b"CHANGED!")
    assert _export(ctx, project, headers)["status"] == "failed"


def test_manifest_wrong_key_or_file_set_is_not_ready(tmp_path):
    settings = Settings(data_dir=tmp_path)
    key = "b" * 64
    entry = cache.entry_path(settings, 1, key)
    entry.mkdir(parents=True)
    (entry / "manifest.json").write_text(json.dumps({"key": key, "files": {"../outside": {}}}))
    assert cache.lookup(settings, 1, key) is None
    (entry / "manifest.json").write_text("[]")
    assert cache.lookup(settings, 1, key) is None
    with pytest.raises(ValueError):
        cache.entry_path(settings, 1, "../outside")


def test_linked_cache_root_is_rejected_without_building(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    root = cache.cache_root(settings)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == root or original(path))
    assert cache.lookup(settings, 1, "c" * 64) is None
    with pytest.raises(ValueError, match="symlink"):
        with cache.material(settings, 1, "c" * 64, 42,
                            lambda target: pytest.fail("unsafe root must not build")):
            pass
    assert "c" * 64 not in cache.PINNED_KEYS
