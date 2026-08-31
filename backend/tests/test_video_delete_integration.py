from __future__ import annotations

import logging
from datetime import datetime

from app.display_proxy_processor import DISPLAY_PROXY_PROFILE_VERSION
from app.models import BackgroundJob, Video
from app.video_delete_db import VideoDeleteConflictError
from app.video_delete_io import VideoDeleteIOError
from app import video_delete_service as service_module


def _made(ctx):
    made = ctx.make_project_with_video()
    return made, made["project"]["id"], made["video"]["id"], made["headers"]


def test_hard_delete_endpoint_removes_video_and_source(ctx):
    made, project_id, video_id, headers = _made(ctx)
    source = ctx.raw_client.app.state.settings.videos_dir / "delete-me.mp4"
    source.write_bytes(b"video")
    with ctx.session_factory() as db:
        video = db.get(Video, video_id)
        video.storage_path = source.name
        db.commit()

    response = ctx.client.delete(
        f"/api/projects/{project_id}/videos/{video_id}", headers=headers,
    )

    assert response.status_code == 204, response.text
    assert not source.exists()
    with ctx.session_factory() as db:
        assert db.get(Video, video_id) is None


def test_hard_delete_removes_ready_proxy_and_duplicate_terminal_result(ctx):
    _made_result, project_id, video_id, headers = _made(ctx)
    settings = ctx.raw_client.app.state.settings
    source = settings.videos_dir / "proxy-source.mp4"
    proxy = settings.display_proxies_dir / "proxy-ready.mp4"
    source.write_bytes(b"video")
    proxy.write_bytes(b"proxy")
    with ctx.session_factory() as db:
        video = db.get(Video, video_id)
        video.storage_path = source.name
        video.source_sha256 = "a" * 64
        video.display_status = "ready"
        video.display_path = proxy.name
        video.display_profile_version = DISPLAY_PROXY_PROFILE_VERSION
        video.display_source_sha256 = video.source_sha256
        video.display_generated_at = datetime.utcnow()
        job = BackgroundJob(
            project_id=project_id, job_type="display_proxy", status="succeeded",
            progress=100, result_path=proxy.name,
            payload={"video_id": video_id, "project_id": project_id,
                     "source_sha256": video.source_sha256,
                     "profile_version": video.display_profile_version},
        )
        db.add(job); db.commit(); job_id = job.id

    response = ctx.client.delete(
        f"/api/projects/{project_id}/videos/{video_id}", headers=headers,
    )
    assert response.status_code == 204, response.text
    assert not source.exists() and not proxy.exists()
    with ctx.session_factory() as db:
        assert db.get(Video, video_id) is None
        assert db.get(BackgroundJob, job_id) is None
        assert db.connection().exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_hard_delete_accepts_terminal_legacy_display_proxy_job(ctx):
    _made_result, project_id, video_id, headers = _made(ctx)
    with ctx.session_factory() as db:
        job = BackgroundJob(
            project_id=project_id, job_type="display_proxy", status="failed",
            payload={"video_id": video_id, "project_id": project_id,
                     "source_sha256": "a" * 64,
                     "profile_version": "candidate-720p-h264-crf28-g30-sar1"},
        )
        db.add(job); db.commit(); job_id = job.id

    response = ctx.client.delete(
        f"/api/projects/{project_id}/videos/{video_id}", headers=headers,
    )

    assert response.status_code == 204, response.text
    with ctx.session_factory() as db:
        assert db.get(Video, video_id) is None
        assert db.get(BackgroundJob, job_id) is None


def test_database_failure_restores_source_and_ready_proxy(ctx, monkeypatch):
    _made_result, project_id, video_id, headers = _made(ctx)
    settings = ctx.raw_client.app.state.settings
    source = settings.videos_dir / "restore-source.mp4"
    proxy = settings.display_proxies_dir / "restore-proxy.mp4"
    source.write_bytes(b"video"); proxy.write_bytes(b"proxy")
    with ctx.session_factory() as db:
        video = db.get(Video, video_id)
        video.storage_path = source.name
        video.source_sha256 = "b" * 64
        video.display_status = "ready"
        video.display_path = proxy.name
        video.display_profile_version = DISPLAY_PROXY_PROFILE_VERSION
        video.display_source_sha256 = video.source_sha256
        video.display_generated_at = datetime.utcnow()
        db.commit()

    def fail_delete(*_args, **_kwargs):
        raise VideoDeleteConflictError("injected")

    monkeypatch.setattr(service_module, "delete_frozen_video", fail_delete)
    response = ctx.client.delete(
        f"/api/projects/{project_id}/videos/{video_id}", headers=headers,
    )
    assert response.status_code == 409
    assert source.read_bytes() == b"video" and proxy.read_bytes() == b"proxy"
    with ctx.session_factory() as db:
        assert db.get(Video, video_id).display_path == proxy.name


def test_success_log_has_safe_terminal_job_ids_and_no_sensitive_data(ctx, caplog):
    _made_result, project_id, video_id, headers = _made(ctx)
    source = ctx.raw_client.app.state.settings.videos_dir / "private-source.mp4"
    source.write_bytes(b"video")
    secret = "do-not-log-payload-secret"
    with ctx.session_factory() as db:
        db.get(Video, video_id).storage_path = source.name
        job = BackgroundJob(
            project_id=project_id, job_type="media", status="succeeded", progress=100,
            payload={"video_id": video_id, "project_id": project_id,
                     "revision": 1, "secret": secret},
        )
        db.add(job)
        db.commit()
        terminal_job_id = job.id

    service_logger = service_module.logger
    old_level, old_disabled = service_logger.level, service_logger.disabled
    service_logger.addHandler(caplog.handler)
    service_logger.setLevel(logging.INFO)
    service_logger.disabled = False
    try:
        response = ctx.client.delete(
            f"/api/projects/{project_id}/videos/{video_id}", headers=headers,
        )
    finally:
        service_logger.removeHandler(caplog.handler)
        service_logger.setLevel(old_level)
        service_logger.disabled = old_disabled

    assert response.status_code == 204, response.text
    events = {
        getattr(record, "event", None): record
        for record in caplog.records
        if getattr(record, "event", "").startswith("video_delete_")
    }
    assert {"video_delete_started", "video_delete_finished"} <= events.keys()
    finished = events["video_delete_finished"]
    assert finished.terminal_job_ids == [terminal_job_id]
    assert finished.terminal_job_count == 1
    assert finished.final_phase == "purged"
    assert isinstance(finished.row_counts, dict)
    serialized_records = repr([record.__dict__ for record in events.values()])
    assert str(source.resolve()) not in serialized_records
    assert secret not in serialized_records
    assert "payload" not in finished.__dict__


def test_hard_delete_permission_and_state_rejections_are_side_effect_free(ctx, login_headers):
    made, project_id, video_id, owner_headers = _made(ctx)
    member_id = ctx.create_user("ordinary")
    ctx.add_member(project_id, member_id, "member")
    member_headers = login_headers("ordinary", "pw123")

    denied = ctx.client.delete(
        f"/api/projects/{project_id}/videos/{video_id}", headers=member_headers,
    )
    assert denied.status_code == 403
    with ctx.session_factory() as db:
        video = db.get(Video, video_id)
        assert video is not None
        video.workflow_status = "submitted"
        db.commit()

    blocked = ctx.client.delete(
        f"/api/projects/{project_id}/videos/{video_id}", headers=owner_headers,
    )
    assert blocked.status_code == 409
    with ctx.session_factory() as db:
        assert db.get(Video, video_id) is not None


def test_active_job_and_busy_gate_block_with_zero_side_effects(ctx):
    made, project_id, video_id, headers = _made(ctx)
    with ctx.session_factory() as db:
        job = BackgroundJob(
            project_id=project_id, job_type="media", status="queued",
            progress=0, payload={"project_id": project_id, "video_id": video_id, "revision": 1},
            dedupe_key=f"media:video:{video_id}:1",
        )
        db.add(job)
        db.commit()
        job_id = job.id

    blocked = ctx.client.delete(
        f"/api/projects/{project_id}/videos/{video_id}", headers=headers,
    )
    assert blocked.status_code == 409
    assert "administrator" in blocked.json()["detail"]
    quarantine = ctx.raw_client.app.state.video_delete_service.io.quarantine_dir
    assert not quarantine.exists()
    with ctx.session_factory() as db:
        assert db.get(Video, video_id) is not None
        assert db.get(BackgroundJob, job_id).status == "queued"

    gate = ctx.raw_client.app.state.video_operation_gate
    with gate.acquire(video_id):
        busy = ctx.client.delete(
            f"/api/projects/{project_id}/videos/{video_id}", headers=headers,
        )
    assert busy.status_code == 409
    with ctx.session_factory() as db:
        assert db.get(Video, video_id) is not None


def test_active_display_proxy_job_returns_conflict(ctx):
    _made_result, project_id, video_id, headers = _made(ctx)
    with ctx.session_factory() as db:
        job = BackgroundJob(
            project_id=project_id, job_type="display_proxy", status="running",
            progress=1, run_token="active-display-proxy",
            payload={"video_id": video_id, "project_id": project_id,
                     "source_sha256": "a" * 64,
                     "profile_version": DISPLAY_PROXY_PROFILE_VERSION},
        )
        db.add(job); db.commit(); job_id = job.id

    response = ctx.client.delete(
        f"/api/projects/{project_id}/videos/{video_id}", headers=headers,
    )
    assert response.status_code == 409
    with ctx.session_factory() as db:
        assert db.get(Video, video_id) is not None
        assert db.get(BackgroundJob, job_id).status == "running"


def test_final_database_conflict_restores_quarantined_source(ctx, monkeypatch):
    made, project_id, video_id, headers = _made(ctx)
    source = ctx.raw_client.app.state.settings.videos_dir / "restore-me.mp4"
    source.write_bytes(b"video")
    with ctx.session_factory() as db:
        db.get(Video, video_id).storage_path = source.name
        db.commit()

    def fail_delete(*_args, **_kwargs):
        raise VideoDeleteConflictError("The frozen video graph changed before final deletion")

    monkeypatch.setattr(service_module, "delete_frozen_video", fail_delete)
    response = ctx.client.delete(
        f"/api/projects/{project_id}/videos/{video_id}", headers=headers,
    )
    assert response.status_code == 409
    assert source.read_bytes() == b"video"
    with ctx.session_factory() as db:
        assert db.get(Video, video_id) is not None
    quarantine = ctx.raw_client.app.state.video_delete_service.io.quarantine_dir
    assert not quarantine.exists() or not list(quarantine.iterdir())


def test_post_commit_purge_failure_is_recovered(ctx, monkeypatch):
    made, project_id, video_id, headers = _made(ctx)
    service = ctx.raw_client.app.state.video_delete_service
    original = service.io.purge

    def fail_purge(_manifest):
        raise VideoDeleteIOError("injected-purge-failure")

    monkeypatch.setattr(service.io, "purge", fail_purge)
    failed = ctx.client.delete(
        f"/api/projects/{project_id}/videos/{video_id}", headers=headers,
    )
    assert failed.status_code == 500
    assert "data was deleted" in failed.json()["detail"]
    with ctx.session_factory() as db:
        assert db.get(Video, video_id) is None
    assert service.io.quarantine_dir.exists()

    monkeypatch.setattr(service.io, "purge", original)
    results = service.recover()
    assert results and results[0].ok and results[0].action == "purged"


def test_existing_video_write_path_uses_same_nonblocking_gate(ctx):
    made, project_id, video_id, headers = _made(ctx)
    gate = ctx.raw_client.app.state.video_operation_gate
    with gate.acquire(video_id):
        response = ctx.client.post(
            f"/api/projects/{project_id}/videos/{video_id}/claim", headers=headers,
        )
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Video is busy with another persistent operation; retry later"
    )
    with ctx.session_factory() as db:
        assert db.get(Video, video_id).assignee_membership_id is None
