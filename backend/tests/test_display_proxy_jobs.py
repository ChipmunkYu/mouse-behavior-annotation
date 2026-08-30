import hashlib
from pathlib import Path

import pytest

from app.display_proxy_jobs import DisplayProxyWorker, enqueue_display_proxy
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
    claim = worker._claim(job_id)
    assert claim is not None
    token, payload = claim
    assert token
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


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_worker_does_not_capture_process_control_exceptions(ctx, interrupt):
    settings, _source, _digest, job_id, _video_id, _ = _queued(ctx)

    class InterruptingProcessor:
        def render(self, **_kwargs):
            raise interrupt()

    worker = DisplayProxyWorker(processor=InterruptingProcessor(),
                                session_factory=ctx.session_factory, settings=settings)
    with pytest.raises(interrupt):
        worker._run(job_id)
    with ctx.session_factory() as db:
        job = db.get(BackgroundJob, job_id)
        assert job.status == "running" and job.run_token


def test_process_lock_rejects_second_owner(tmp_path):
    first, second = ProcessLock(tmp_path / "owner.lock"), ProcessLock(tmp_path / "owner.lock")
    first.acquire()
    try:
        with pytest.raises(ProcessLockError):
            second.acquire()
    finally:
        first.release()
    second.acquire(); second.release()
