import hashlib
import errno
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Query

from app.display_proxy_jobs import DisplayProxyWorker, display_proxy_names, enqueue_display_proxy
from app.display_proxy_processor import DISPLAY_PROXY_PROFILE_VERSION, DisplayProxyError
from app.models import BackgroundJob, Video
from app.process_lock import ProcessLock, ProcessLockError


class FakeDisplayProcessor:
    def __init__(self, fail=False, mutate=None):
        self.fail, self.mutate, self.calls = fail, mutate, []

    def render(self, *, input_path, output_path):
        self.calls.append((input_path, output_path))
        if self.fail:
            raise DisplayProxyError(f"failed for {input_path}")
        Path(output_path).write_bytes(b"proxy")
        if self.mutate:
            Path(input_path).write_bytes(self.mutate)


class OwnershipChangingProcessor(FakeDisplayProcessor):
    def __init__(self, session_factory, video_id):
        super().__init__(); self.session_factory, self.video_id = session_factory, video_id

    def render(self, *, input_path, output_path):
        super().render(input_path=input_path, output_path=output_path)
        with self.session_factory() as db:
            video = db.get(Video, self.video_id)
            video.display_status, video.display_error = "failed", "new owner state"
            db.commit()


def _queued(ctx, content=b"source"):
    info = ctx.make_project_with_video()
    settings = ctx.raw_client.app.state.settings
    source = settings.videos_dir / f"source-{info['video']['id']}.mp4"
    source.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    with ctx.session_factory() as db:
        video = db.get(Video, info["video"]["id"])
        video.storage_path, video.source_sha256 = source.name, digest
        job = enqueue_display_proxy(db, video)
        job_id, video_id, key = job.id, video.id, job.dedupe_key
        db.commit()
    return settings, source, digest, job_id, video_id, key


def test_worker_payload_validation_rejects_legacy_profile():
    assert display_proxy_names({
        "video_id": 1,
        "project_id": 1,
        "source_sha256": "a" * 64,
        "profile_version": "candidate-720p-h264-crf28-g30-sar1",
    }) is None


def test_enqueue_dedupes_and_requires_source_hash(ctx):
    settings, source, digest, job_id, video_id, key = _queued(ctx)
    with ctx.session_factory() as db:
        video = db.get(Video, video_id)
        same = enqueue_display_proxy(db, video)
        assert same.id == job_id
        assert set(same.payload) == {"video_id", "project_id", "source_sha256", "profile_version"}
        assert "storage_path" not in same.payload
        assert key == f"display-proxy:video:{video_id}:source:{digest}:profile:{DISPLAY_PROXY_PROFILE_VERSION}"
        video.source_sha256 = None
        with pytest.raises(ValueError, match="source_sha256"):
            enqueue_display_proxy(db, video)


def test_enqueue_running_job_preserves_worker_ownership_and_video(ctx):
    _settings, _source, _digest, job_id, video_id, _ = _queued(ctx)
    with ctx.session_factory() as db:
        job, video = db.get(BackgroundJob, job_id), db.get(Video, video_id)
        job.status, job.run_token, job.attempts = "running", "current-owner", 2
        job.started_at = video.display_generated_at
        video.display_status, video.display_error = "processing", None
        db.commit()
    with ctx.session_factory() as db:
        job, video = db.get(BackgroundJob, job_id), db.get(Video, video_id)
        same = enqueue_display_proxy(db, video)
        db.commit()
        assert same.id == job_id
    with ctx.session_factory() as db:
        job, video = db.get(BackgroundJob, job_id), db.get(Video, video_id)
        assert (job.status, job.run_token, job.attempts) == ("running", "current-owner", 2)
        assert video.display_status == "processing"


def test_success_claims_with_token_and_commits_ready_atomically(ctx):
    settings, _source, digest, job_id, video_id, _ = _queued(ctx)
    processor = FakeDisplayProcessor()
    worker = DisplayProxyWorker(processor=processor, session_factory=ctx.session_factory,
                                settings=settings)
    worker.start(); worker.shutdown()
    with ctx.session_factory() as db:
        job, video = db.get(BackgroundJob, job_id), db.get(Video, video_id)
        assert job.status == "succeeded" and job.run_token is None and job.attempts == 1
        assert video.display_status == "ready"
        assert video.display_source_sha256 == digest
        assert video.display_profile_version.startswith("candidate-")
        assert (settings.display_proxies_dir / video.display_path).read_bytes() == b"proxy"
        assert not list(settings.display_proxies_dir.glob("*.part"))


def test_failure_is_redacted_and_source_change_is_detected(ctx):
    settings, source, _digest, job_id, video_id, _ = _queued(ctx)
    worker = DisplayProxyWorker(processor=FakeDisplayProcessor(fail=True),
                                session_factory=ctx.session_factory, settings=settings)
    worker.start(); worker.shutdown()
    with ctx.session_factory() as db:
        job, video = db.get(BackgroundJob, job_id), db.get(Video, video_id)
        assert job.status == video.display_status == "failed"
        assert job.run_token is None
        assert str(source) not in job.error and "<media-path>" in job.error


def test_start_reserve_shortage_fails_without_render_or_source_damage(ctx, monkeypatch):
    settings, source, _digest, job_id, video_id, _ = _queued(ctx, b"x" * 100)
    settings.display_proxy_disk_reserve_bytes = 1000
    processor = FakeDisplayProcessor()
    monkeypatch.setattr(
        "app.display_proxy_jobs.shutil.disk_usage", lambda _path: SimpleNamespace(free=1029)
    )
    worker = DisplayProxyWorker(processor=processor, session_factory=ctx.session_factory,
                                settings=settings)
    worker.start(); worker.shutdown()
    with ctx.session_factory() as db:
        job, video = db.get(BackgroundJob, job_id), db.get(Video, video_id)
        assert job.status == video.display_status == "failed"
        assert job.error == video.display_error == "display proxy disk space unavailable"
    assert processor.calls == [] and source.read_bytes() == b"x" * 100
    assert not list(settings.display_proxies_dir.glob("*.part"))


def test_publish_reserve_shortage_cleans_temp_and_never_replaces_ready(ctx, monkeypatch):
    settings, source, _digest, job_id, video_id, _ = _queued(ctx, b"source")
    settings.display_proxy_disk_reserve_bytes = 1000
    calls = iter((SimpleNamespace(free=2000), SimpleNamespace(free=999)))
    monkeypatch.setattr("app.display_proxy_jobs.shutil.disk_usage", lambda _path: next(calls))
    worker = DisplayProxyWorker(processor=FakeDisplayProcessor(),
                                session_factory=ctx.session_factory, settings=settings)
    worker.start(); worker.shutdown()
    with ctx.session_factory() as db:
        assert db.get(BackgroundJob, job_id).status == "failed"
        assert db.get(Video, video_id).display_status == "failed"
    assert source.read_bytes() == b"source"
    assert not list(settings.display_proxies_dir.glob("*.part"))
    assert not list(settings.display_proxies_dir.glob("*.mp4"))


def test_enospc_is_stable_and_cleans_partial_output(ctx):
    settings, source, _digest, job_id, video_id, _ = _queued(ctx)
    settings.display_proxy_disk_reserve_bytes = 0

    class EnospcProcessor:
        def render(self, *, output_path, **_kwargs):
            Path(output_path).write_bytes(b"partial")
            raise OSError(errno.ENOSPC, "No space left on device", output_path)

    worker = DisplayProxyWorker(processor=EnospcProcessor(),
                                session_factory=ctx.session_factory, settings=settings)
    worker.start(); worker.shutdown()
    with ctx.session_factory() as db:
        job, video = db.get(BackgroundJob, job_id), db.get(Video, video_id)
        assert job.status == video.display_status == "failed"
        assert job.error == video.display_error == "display proxy disk space unavailable"
        assert str(settings.display_proxies_dir) not in job.error
    assert source.exists() and not list(settings.display_proxies_dir.glob("*.part"))


def test_empty_runtime_error_persists_nonempty_terminal_failure(ctx):
    settings, _source, _digest, job_id, video_id, _ = _queued(ctx)

    class EmptyErrorProcessor:
        def render(self, **_kwargs):
            raise RuntimeError()

    worker = DisplayProxyWorker(processor=EmptyErrorProcessor(),
                                session_factory=ctx.session_factory, settings=settings)
    worker.start(); worker.shutdown()
    with ctx.session_factory() as db:
        job, video = db.get(BackgroundJob, job_id), db.get(Video, video_id)
        assert job.status == "failed" and job.run_token is None
        assert job.error == video.display_error == "display proxy processing failed"


def test_terminal_commit_applied_but_exception_preserves_published_result(ctx):
    settings, _source, digest, job_id, video_id, _ = _queued(ctx)
    worker = DisplayProxyWorker(processor=FakeDisplayProcessor(),
                                session_factory=ctx.session_factory, settings=settings)

    def raise_after_commit(*_args):
        raise RuntimeError("connection lost after commit")

    worker._after_terminal_commit = raise_after_commit
    worker.start(); worker.shutdown()
    with ctx.session_factory() as db:
        job, video = db.get(BackgroundJob, job_id), db.get(Video, video_id)
        assert job.status == "succeeded" and job.run_token is None
        assert video.display_status == "ready" and video.display_source_sha256 == digest
        assert (settings.display_proxies_dir / video.display_path).exists()


def test_terminal_commit_failure_removes_unreferenced_publish_and_fails(ctx):
    settings, _source, _digest, job_id, video_id, _ = _queued(ctx)
    worker = DisplayProxyWorker(processor=FakeDisplayProcessor(),
                                session_factory=ctx.session_factory, settings=settings)

    def fail_commit(_db):
        raise RuntimeError("commit rejected")

    worker._commit_terminal = fail_commit
    worker.start(); worker.shutdown()
    with ctx.session_factory() as db:
        job, video = db.get(BackgroundJob, job_id), db.get(Video, video_id)
        assert job.status == "failed" and job.run_token is None
        assert video.display_status == "failed"
    assert not list(settings.display_proxies_dir.glob("*.mp4"))


def test_post_transcode_source_change_never_publishes(ctx):
    settings, _source, _digest, job_id, video_id, _ = _queued(ctx)
    worker = DisplayProxyWorker(processor=FakeDisplayProcessor(mutate=b"changed"),
                                session_factory=ctx.session_factory, settings=settings)
    worker.start(); worker.shutdown()
    with ctx.session_factory() as db:
        assert db.get(BackgroundJob, job_id).status == "failed"
        assert db.get(Video, video_id).display_status == "failed"
    assert not list(settings.display_proxies_dir.glob("*.mp4"))


def test_late_ownership_loss_cancels_job_without_overwriting_video(ctx):
    settings, _source, _digest, job_id, video_id, _ = _queued(ctx)
    processor = OwnershipChangingProcessor(ctx.session_factory, video_id)
    worker = DisplayProxyWorker(processor=processor, session_factory=ctx.session_factory,
                                settings=settings)
    worker.start(); worker.shutdown()
    with ctx.session_factory() as db:
        assert db.get(BackgroundJob, job_id).status == "cancelled"
        assert db.get(BackgroundJob, job_id).run_token is None
        video = db.get(Video, video_id)
        assert (video.display_status, video.display_error) == ("failed", "new owner state")
    assert not list(settings.display_proxies_dir.glob("*.mp4"))


def test_recovery_cleans_owned_files_at_retry_limit_and_is_idempotent(ctx):
    settings, _source, digest, job_id, video_id, _ = _queued(ctx)
    settings.display_proxy_max_attempts = 1
    worker = DisplayProxyWorker(processor=FakeDisplayProcessor(),
                                session_factory=ctx.session_factory, settings=settings)
    payload = {"video_id": video_id, "project_id": None, "source_sha256": digest,
               "profile_version": DISPLAY_PROXY_PROFILE_VERSION}
    with ctx.session_factory() as db:
        job, video = db.get(BackgroundJob, job_id), db.get(Video, video_id)
        payload = dict(job.payload)
        job.status, job.run_token, job.attempts = "running", "owned-token", 1
        video.display_status = "processing"
        db.commit()
    temp, final = worker._paths(payload)
    temp.write_bytes(b"stale-temp"); final.write_bytes(b"stale-final")
    worker.start(); worker.shutdown()
    worker.start(); worker.shutdown()
    with ctx.session_factory() as db:
        assert db.get(BackgroundJob, job_id).status == "failed"
        assert db.get(BackgroundJob, job_id).run_token is None
        assert db.get(Video, video_id).display_status == "failed"
    assert not temp.exists() and not final.exists()


def test_worker_resolves_current_video_storage_path(ctx):
    settings, source, _digest, job_id, video_id, _ = _queued(ctx)
    relocated = settings.videos_dir / "relocated.mp4"
    source.replace(relocated)
    with ctx.session_factory() as db:
        db.get(Video, video_id).storage_path = relocated.name
        db.commit()
    processor = FakeDisplayProcessor()
    worker = DisplayProxyWorker(processor=processor, session_factory=ctx.session_factory,
                                settings=settings)
    worker.start(); worker.shutdown()
    assert processor.calls[0][0] == str(relocated.resolve())
    with ctx.session_factory() as db:
        assert db.get(BackgroundJob, job_id).status == "succeeded"


def test_claim_has_token_and_owns_requires_exact_type_and_payload(ctx):
    settings, _source, _digest, job_id, _video_id, _ = _queued(ctx)
    worker = DisplayProxyWorker(processor=FakeDisplayProcessor(),
                                session_factory=ctx.session_factory, settings=settings)
    token = "claim-token"
    payload = worker._claim(job_id, token)
    assert payload is not None
    with ctx.session_factory() as db:
        job = db.get(BackgroundJob, job_id)
        assert job.status == "running" and job.run_token == token
        assert worker._owns(db, job_id, token, payload)
        job.job_type = "media"
        db.flush()
        assert not worker._owns(db, job_id, token, payload)
        job.job_type = "display_proxy"
        job.payload = {**payload, "extra": True}
        db.flush()
        assert not worker._owns(db, job_id, token, payload)
        db.rollback()


def test_old_token_cannot_terminalize_new_running_generation(ctx):
    settings, _source, _digest, job_id, video_id, _ = _queued(ctx)
    worker = DisplayProxyWorker(processor=FakeDisplayProcessor(),
                                session_factory=ctx.session_factory, settings=settings)
    old_token = "old-token"
    assert worker._claim(job_id, old_token) is not None
    with ctx.session_factory() as db:
        db.get(BackgroundJob, job_id).run_token = "new-token"
        db.commit()

    assert worker._terminalize_running(job_id, old_token) is False

    with ctx.session_factory() as db:
        job, video = db.get(BackgroundJob, job_id), db.get(Video, video_id)
        assert (job.status, job.run_token) == ("running", "new-token")
        assert video.display_status == "processing"


def test_token_change_after_payload_read_prevents_terminalization(ctx, monkeypatch):
    settings, _source, _digest, job_id, video_id, _ = _queued(ctx)
    worker = DisplayProxyWorker(processor=FakeDisplayProcessor(),
                                session_factory=ctx.session_factory, settings=settings)
    old_token = "old-token"
    assert worker._claim(job_id, old_token) is not None
    original_update = Query.update
    injected = False

    def replace_token_before_conditional_update(query, values, *args, **kwargs):
        nonlocal injected
        if not injected and "run_token" in values:
            injected = True
            with ctx.session_factory() as db:
                db.execute(update(BackgroundJob).where(BackgroundJob.id == job_id).values(
                    run_token="new-token"))
                db.commit()
        return original_update(query, values, *args, **kwargs)

    monkeypatch.setattr(Query, "update", replace_token_before_conditional_update)
    assert worker._terminalize_running(job_id, old_token) is False
    assert injected

    with ctx.session_factory() as db:
        job, video = db.get(BackgroundJob, job_id), db.get(Video, video_id)
        assert (job.status, job.run_token) == ("running", "new-token")
        assert video.display_status == "processing"


def test_recovery_wrong_source_job_does_not_mask_orphan_processing(ctx):
    settings, _source, _digest, job_id, video_id, _ = _queued(ctx)
    with ctx.session_factory() as db:
        video = db.get(Video, video_id)
        video.display_status = "processing"
        video.source_sha256 = "b" * 64
        db.commit()
    worker = DisplayProxyWorker(processor=FakeDisplayProcessor(),
                                session_factory=ctx.session_factory, settings=settings)
    worker.start(); worker.shutdown()
    with ctx.session_factory() as db:
        assert db.get(Video, video_id).display_status == "failed"
        job = db.get(BackgroundJob, job_id)
        assert job.status == "cancelled" and job.run_token is None


def test_recovery_malformed_running_job_fails_safe_and_continues(ctx):
    settings, _source, _digest, malformed_id, malformed_video_id, _ = _queued(ctx)
    _settings, _source, _digest, valid_id, valid_video_id, _ = _queued(ctx)
    worker = DisplayProxyWorker(processor=FakeDisplayProcessor(),
                                session_factory=ctx.session_factory, settings=settings)
    with ctx.session_factory() as db:
        malformed = db.get(BackgroundJob, malformed_id)
        malformed.status, malformed.run_token, malformed.attempts = "running", "stale-token", 1
        malformed.payload = {"video_id": malformed_video_id, "unexpected": "private/path.mp4"}
        db.get(Video, malformed_video_id).display_status = "processing"
        db.commit()

    ready = settings.display_proxies_dir / "already-ready.mp4"
    foreign_temp = settings.display_proxies_dir / ".another-job.mp4.part"
    ready.write_bytes(b"ready")
    foreign_temp.write_bytes(b"foreign")

    worker.start(); worker.shutdown()

    with ctx.session_factory() as db:
        malformed = db.get(BackgroundJob, malformed_id)
        assert malformed.status == "failed" and malformed.run_token is None
        assert malformed.error == "invalid frozen display proxy payload"
        assert "private" not in malformed.error
        assert db.get(BackgroundJob, valid_id).status == "succeeded"
        assert db.get(Video, valid_video_id).display_status == "ready"
    assert ready.read_bytes() == b"ready"
    assert foreign_temp.read_bytes() == b"foreign"


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_async_control_exception_cleans_terminalizes_and_lane_continues(
        ctx, monkeypatch, interrupt):
    settings, _source, _digest, job_id, _video_id, _ = _queued(ctx)
    _settings, _source, _digest, second_id, second_video_id, _ = _queued(ctx)
    settings.display_proxy_synchronous = False

    class InterruptingFirstProcessor(FakeDisplayProcessor):
        def render(self, **kwargs):
            if not self.calls:
                self.calls.append((kwargs["input_path"], kwargs["output_path"]))
                Path(kwargs["output_path"]).write_bytes(b"partial")
                raise interrupt()
            super().render(**kwargs)

    worker = DisplayProxyWorker(processor=InterruptingFirstProcessor(),
                                session_factory=ctx.session_factory, settings=settings)
    futures = {}
    original_done = worker._future_done

    def observe(job_id, future):
        futures[job_id] = future
        original_done(job_id, future)

    monkeypatch.setattr(worker, "_future_done", observe)
    worker.start(); worker.shutdown()

    assert isinstance(futures[job_id].exception(), interrupt)
    with ctx.session_factory() as db:
        job = db.get(BackgroundJob, job_id)
        video = db.get(Video, job.payload["video_id"])
        assert job.status == "failed" and job.run_token is None and job.finished_at is not None
        assert video.display_status == "failed"
        assert db.get(BackgroundJob, second_id).status == "succeeded"
        assert db.get(Video, second_video_id).display_status == "ready"
    assert not list(settings.display_proxies_dir.glob("*.part"))


def test_async_failed_persistence_retries_same_token_and_lane_continues(
        ctx, monkeypatch, caplog):
    settings, _source, _digest, first_id, first_video_id, _ = _queued(ctx)
    _settings, _source, _digest, second_id, second_video_id, _ = _queued(ctx)
    settings.display_proxy_synchronous = False

    class FailFirstProcessor(FakeDisplayProcessor):
        def render(self, **kwargs):
            if not self.calls:
                self.calls.append((kwargs["input_path"], kwargs["output_path"]))
                raise DisplayProxyError("first processing failure")
            super().render(**kwargs)

    worker = DisplayProxyWorker(processor=FailFirstProcessor(),
                                session_factory=ctx.session_factory, settings=settings)
    original_terminalize = worker._terminalize_running
    terminalize_tokens = []

    def fail_once_terminalize(job_id, token):
        terminalize_tokens.append(token)
        if len(terminalize_tokens) == 1:
            raise RuntimeError("injected first terminalization failure")
        return original_terminalize(job_id, token)

    caplog.set_level(logging.ERROR)
    monkeypatch.setattr(worker, "_fail", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("injected fail persistence failure")))
    monkeypatch.setattr(worker, "_terminalize_running", fail_once_terminalize)
    worker.start(); worker.shutdown()

    with ctx.session_factory() as db:
        first, second = db.get(BackgroundJob, first_id), db.get(BackgroundJob, second_id)
        assert first.status == "failed" and first.run_token is None and first.finished_at is not None
        assert db.get(Video, first_video_id).display_status == "failed"
        assert second.status == "succeeded" and second.run_token is None
        assert db.get(Video, second_video_id).display_status == "ready"
    assert len(terminalize_tokens) == 2 and len(set(terminalize_tokens)) == 1
    events = [json.loads(record.message) for record in caplog.records
              if record.message.startswith("{")]
    fallback = [event for event in events
                if event.get("event") == "display_proxy_failed"
                and event.get("error_category") == "worker_uncaught"]
    assert len(fallback) == 1
    rendered = "\n".join(record.message for record in caplog.records)
    assert "injected first terminalization failure" not in rendered
    assert "injected fail persistence failure" not in rendered


def test_terminalization_control_exception_propagates(ctx, monkeypatch):
    settings, _source, _digest, job_id, _video_id, _ = _queued(ctx)
    worker = DisplayProxyWorker(processor=FakeDisplayProcessor(),
                                session_factory=ctx.session_factory, settings=settings)
    monkeypatch.setattr(worker, "_run_claimed", lambda *_args: (_ for _ in ()).throw(
        RuntimeError("processing failed")))
    monkeypatch.setattr(worker, "_terminalize_running", lambda *_args: (_ for _ in ()).throw(
        SystemExit("stop")))

    with pytest.raises(SystemExit, match="stop"):
        worker._run(job_id)


def test_synchronous_terminalization_failure_is_safely_diagnosed(ctx, monkeypatch, caplog):
    settings, source, _digest, job_id, _video_id, _ = _queued(ctx)
    worker = DisplayProxyWorker(processor=FakeDisplayProcessor(fail=True),
                                session_factory=ctx.session_factory, settings=settings)
    monkeypatch.setattr(worker, "_fail", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError(f"private failure {source}")))
    monkeypatch.setattr(worker, "_terminalize_running", lambda *_args: (_ for _ in ()).throw(
        RuntimeError(f"private terminal failure {source}")))
    caplog.set_level(logging.ERROR)

    with pytest.raises(RuntimeError, match="private failure"):
        worker._run(job_id)

    messages = [record.message for record in caplog.records]
    events = [json.loads(message) for message in messages if message.startswith("{")]
    assert sum(event.get("event") == "display_proxy_terminalization_failed"
               for event in events) == 2
    assert str(source) not in "\n".join(messages)


def test_process_lock_rejects_second_owner(tmp_path):
    first, second = ProcessLock(tmp_path / "owner.lock"), ProcessLock(tmp_path / "owner.lock")
    first.acquire()
    try:
        with pytest.raises(ProcessLockError):
            second.acquire()
    finally:
        first.release()
    second.acquire(); second.release()
