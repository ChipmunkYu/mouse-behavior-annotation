"""Drain-owned staging uses real publication enqueue and per-entry scheduling."""
import pytest

from app import export_jobs
from app.models import BackgroundJob, Project
from tests.test_project_export import _approved


@pytest.mark.parametrize("interrupt", [None, "export", "error", "shutdown"])
def test_publication_source_owner(media_ctx, monkeypatch, interrupt):
    ctx = media_ctx
    worker = ctx.app.state.export_worker
    settings = ctx.app.state.settings
    settings.context_prewarm_enabled = True
    copies, rendered = [], []
    stage = export_jobs.stage_submission_input
    write = worker._write_item

    def counted(*args, **kwargs):
        path = stage(*args, **kwargs)
        copies.append(path)
        return path

    def item(*args, **kwargs):
        rendered.append(1)
        write(*args, **kwargs)
        if len(rendered) != 1:
            return
        if interrupt == "error":
            raise RuntimeError("prewarm test error")
        if interrupt == "shutdown":
            worker.shutdown()
        if interrupt == "export":
            with ctx.session_factory() as db:
                project = db.query(Project).first()
                export_jobs.enqueue_export_job(db, project, None)

    def build(db, job):
        assert copies and all(not path.exists() for path in copies)
        job.status = "succeeded"
        db.commit()

    monkeypatch.setattr(export_jobs, "stage_submission_input", counted)
    monkeypatch.setattr(worker, "_write_item", item)
    monkeypatch.setattr(worker, "_build", build)
    _approved(ctx, two_categories=True)
    assert all(not path.exists() for path in copies)
    assert worker._prewarm_source is None
    assert len(copies) == (2 if interrupt in {"export", "error"} else 1)
    with ctx.session_factory() as db:
        jobs = db.query(BackgroundJob).filter_by(job_type="context_prewarm").all()
        assert len(jobs) == 2


def test_drain_baseexception_releases_owner(media_ctx, monkeypatch, tmp_path):
    worker = media_ctx.app.state.export_worker
    source = tmp_path / "owned.staging"
    with media_ctx.session_factory() as db:
        db.add(BackgroundJob(job_type="context_prewarm", status="queued",
                             payload={}, dedupe_key="termination-test"))
        db.commit()

    def terminate(job_id):
        source.write_bytes(b"private source")
        worker._prewarm_source = source
        raise SystemExit("termination")

    monkeypatch.setattr(worker, "_run_job", terminate)
    with pytest.raises(SystemExit):
        worker.wake()
    assert not source.exists()
    assert worker._prewarm_source is None
    assert not worker._draining
