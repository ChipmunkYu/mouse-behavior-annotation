from __future__ import annotations

from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
from threading import Event

import pytest

from app.cleanup import run_retention_cleanup
from app.models import ProjectMembership, Video, VideoImportBatch
from app.routers import detection_imports
import app.import_batch_cleanup as batch_cleanup_module


def metadata_json(valid: bool = True) -> str:
    if not valid:
        return "{}"
    return json.dumps({
        "schema_version": "1.0", "video_id": "test", "width": 100,
        "height": 100, "fps": 25, "frame_count": 2,
    })


def tracks_jsonl() -> str:
    return "\n".join(json.dumps({
        "schema_version": "1.0", "video_id": "test", "frame_index": index,
        "detection_count": 0, "detections": [],
    }) for index in range(2)) + "\n"


def make_project(ctx, login_headers):
    headers = login_headers()
    project = ctx.client.post(
        "/api/projects", json={"name": "batch-cleanup"}, headers=headers
    ).json()
    ctx.configure_and_lock_minimal_scheme(project["id"], headers)
    return headers, project["id"]


def make_batch(ctx, project_id, headers):
    response = ctx.client.post(
        f"/api/projects/{project_id}/video-import-batches", headers=headers
    )
    assert response.status_code == 201
    return response.json()


def upload(ctx, project_id, batch_id, role, name, content, headers):
    if isinstance(content, str):
        content = content.encode()
    return ctx.client.put(
        f"/api/projects/{project_id}/video-import-batches/{batch_id}/files/{role}",
        files={"file": (name, content)}, headers=headers,
    )


def cancel(ctx, project_id, batch_id, headers):
    return ctx.client.delete(
        f"/api/projects/{project_id}/video-import-batches/{batch_id}", headers=headers
    )


def login(ctx, username, password="pw123"):
    response = ctx.client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_cancel_requires_active_membership_and_matching_project(ctx, login_headers):
    owner, project_id = make_project(ctx, login_headers)
    batch = make_batch(ctx, project_id, owner)
    outsider_id = ctx.create_user("batch-outsider")
    outsider = login(ctx, "batch-outsider")
    assert cancel(ctx, project_id, batch["id"], outsider).status_code in (403, 404)

    inactive_id = ctx.create_user("batch-inactive")
    ctx.add_member(project_id, inactive_id, "member")
    with ctx.session_factory() as db:
        membership = db.query(ProjectMembership).filter_by(
            project_id=project_id, user_id=inactive_id
        ).one()
        membership.status = "inactive"
        db.commit()
    assert cancel(ctx, project_id, batch["id"], login(ctx, "batch-inactive")).status_code == 403

    other = ctx.client.post("/api/projects", json={"name": "other"}, headers=owner).json()
    ctx.add_member(other["id"], outsider_id, "member")
    assert cancel(ctx, other["id"], batch["id"], outsider).status_code == 404
    assert cancel(ctx, project_id, batch["id"], owner).status_code == 204


@pytest.mark.parametrize("status", ["processing", "ready", "video_only", "validating"])
def test_cancel_rejects_non_cancellable_statuses(ctx, login_headers, status):
    headers, project_id = make_project(ctx, login_headers)
    batch = make_batch(ctx, project_id, headers)
    with ctx.session_factory() as db:
        db.get(VideoImportBatch, batch["id"]).status = status
        db.commit()
    assert cancel(ctx, project_id, batch["id"], headers).status_code == 409
    with ctx.session_factory() as db:
        assert db.get(VideoImportBatch, batch["id"]) is not None


def test_cancel_preflights_then_deletes_only_batch_files_and_row(ctx, login_headers, tmp_path):
    headers, project_id = make_project(ctx, login_headers)
    first = make_batch(ctx, project_id, headers)
    second = make_batch(ctx, project_id, headers)
    paths = []
    for role, name, content in (
        ("video", "clip.mp4", b"video"),
        ("tracks", "tracks.jsonl", tracks_jsonl()),
        ("metadata", "metadata.json", metadata_json()),
    ):
        body = upload(ctx, project_id, first["id"], role, name, content, headers).json()
        root = (ctx.client.app.state.settings.videos_dir if role == "video"
                else ctx.client.app.state.settings.detection_imports_dir)
        paths.append(root / body[f"{role}_path"])
    other = upload(ctx, project_id, second["id"], "video", "other.mp4", b"keep", headers).json()
    other_path = ctx.client.app.state.settings.videos_dir / other["video_path"]
    assert cancel(ctx, project_id, first["id"], headers).status_code == 204
    assert all(not path.exists() for path in paths)
    assert other_path.exists()

    unsafe = make_batch(ctx, project_id, headers)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"keep")
    with ctx.session_factory() as db:
        row = db.get(VideoImportBatch, unsafe["id"])
        row.video_path = "../outside.mp4"
        row.video_upload_state = "uploaded"
        db.commit()
    assert cancel(ctx, project_id, unsafe["id"], headers).status_code == 409
    assert outside.read_bytes() == b"keep"
    with ctx.session_factory() as db:
        assert db.get(VideoImportBatch, unsafe["id"]) is not None


def failed_batch(ctx, project_id, headers):
    batch = make_batch(ctx, project_id, headers)
    video = upload(ctx, project_id, batch["id"], "video", "clip.mp4", b"video", headers).json()
    upload(ctx, project_id, batch["id"], "tracks", "tracks.jsonl", tracks_jsonl(), headers)
    upload(ctx, project_id, batch["id"], "metadata", "metadata.json", metadata_json(False), headers)
    result = ctx.client.post(
        f"/api/projects/{project_id}/video-import-batches/{batch['id']}/complete",
        headers=headers,
    )
    assert result.status_code == 200 and result.json()["status"] == "failed"
    return batch["id"], result.json()["video_id"], video["video_path"]


def test_failed_batch_removes_pristine_video_but_rejects_consumed_video(ctx, login_headers):
    headers, project_id = make_project(ctx, login_headers)
    batch_id, video_id, video_path = failed_batch(ctx, project_id, headers)
    assert cancel(ctx, project_id, batch_id, headers).status_code == 204
    assert not (ctx.client.app.state.settings.videos_dir / video_path).exists()
    with ctx.session_factory() as db:
        assert db.get(Video, video_id) is None

    batch_id, video_id, video_path = failed_batch(ctx, project_id, headers)
    with ctx.session_factory() as db:
        video = db.get(Video, video_id)
        video.workflow_status = "submitted"
        video.submitted_at = datetime.utcnow()
        db.commit()
    assert cancel(ctx, project_id, batch_id, headers).status_code == 409
    assert (ctx.client.app.state.settings.videos_dir / video_path).exists()
    with ctx.session_factory() as db:
        assert db.get(VideoImportBatch, batch_id) is not None
        assert db.get(Video, video_id) is not None


def test_failed_pristine_video_delete_is_atomic_against_user_change(
    monkeypatch, ctx, login_headers
):
    headers, project_id = make_project(ctx, login_headers)
    batch_id, video_id, video_path = failed_batch(ctx, project_id, headers)

    def consume_video_before_delete():
        with ctx.session_factory() as other_db:
            video = other_db.get(Video, video_id)
            video.workflow_status = "submitted"
            video.submitted_at = datetime.utcnow()
            other_db.commit()

    monkeypatch.setattr(
        batch_cleanup_module, "_before_pristine_video_delete", consume_video_before_delete
    )
    assert cancel(ctx, project_id, batch_id, headers).status_code == 409
    assert (ctx.client.app.state.settings.videos_dir / video_path).exists()
    with ctx.session_factory() as db:
        assert db.get(VideoImportBatch, batch_id) is not None
        assert db.get(Video, video_id).workflow_status == "submitted"


def test_upload_slot_claim_closes_complete_and_explicit_cancel_wins(ctx, login_headers):
    headers, project_id = make_project(ctx, login_headers)
    batch = make_batch(ctx, project_id, headers)
    first = upload(
        ctx, project_id, batch["id"], "tracks", "tracks.jsonl", tracks_jsonl(), headers
    ).json()["tracks_path"]
    second = upload(
        ctx, project_id, batch["id"], "tracks", "tracks.jsonl", tracks_jsonl(), headers
    ).json()["tracks_path"]
    root = ctx.client.app.state.settings.detection_imports_dir
    assert first != second and not (root / first).exists() and (root / second).exists()

    with ctx.session_factory() as db:
        db.get(VideoImportBatch, batch["id"]).video_upload_state = "uploading"
        db.commit()
    assert cancel(ctx, project_id, batch["id"], headers).status_code == 204


def test_member_cannot_read_upload_complete_or_delete_another_members_batch(ctx, login_headers):
    owner, project_id = make_project(ctx, login_headers)
    alice_id = ctx.create_user("batch-owner-alice")
    bob_id = ctx.create_user("batch-owner-bob")
    ctx.add_member(project_id, alice_id, "member")
    ctx.add_member(project_id, bob_id, "member")
    alice, bob = login(ctx, "batch-owner-alice"), login(ctx, "batch-owner-bob")
    batch = make_batch(ctx, project_id, alice)
    base = f"/api/projects/{project_id}/video-import-batches/{batch['id']}"

    assert ctx.client.get(base, headers=bob).status_code == 403
    assert upload(ctx, project_id, batch["id"], "video", "x.mp4", b"x", bob).status_code == 403
    assert ctx.client.post(f"{base}/complete", headers=bob).status_code == 403
    assert cancel(ctx, project_id, batch["id"], bob).status_code == 403
    assert ctx.client.get(base, headers=owner).status_code == 200
    assert ctx.client.get(base, headers=alice).json()["created_by"] == alice_id


def test_committed_cancel_claim_makes_late_upload_remove_its_generation(
    monkeypatch, ctx, login_headers
):
    headers, project_id = make_project(ctx, login_headers)
    batch = make_batch(ctx, project_id, headers)
    entered, release = Event(), Event()
    original = detection_imports._atomic_save_async

    async def paused_save(*args, **kwargs):
        entered.set()
        assert await asyncio.to_thread(release.wait, 10)
        return await original(*args, **kwargs)

    monkeypatch.setattr(detection_imports, "_atomic_save_async", paused_save)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            upload, ctx, project_id, batch["id"], "video", "late.mp4", b"late", headers
        )
        assert entered.wait(10)
        assert cancel(ctx, project_id, batch["id"], headers).status_code == 204
        release.set()
        response = future.result(timeout=10)
    assert response.status_code == 409
    assert list(ctx.client.app.state.settings.videos_dir.glob("*.mp4")) == []


def test_upload_failure_restores_slot_state(monkeypatch, ctx, login_headers):
    headers, project_id = make_project(ctx, login_headers)
    batch = make_batch(ctx, project_id, headers)

    async def fail_save(*args, **kwargs):
        raise OSError("injected write failure")

    monkeypatch.setattr(detection_imports, "_atomic_save_async", fail_save)
    response = upload(
        ctx, project_id, batch["id"], "video", "clip.mp4", b"video", headers
    )
    assert response.status_code == 500
    with ctx.session_factory() as db:
        row = db.get(VideoImportBatch, batch["id"])
        assert row.status == "uploading"
        assert row.video_upload_state == "pending"
        assert row.video_path is None


def test_post_database_file_failure_is_recorded(monkeypatch, ctx, login_headers):
    headers, project_id = make_project(ctx, login_headers)
    batch = make_batch(ctx, project_id, headers)
    body = upload(
        ctx, project_id, batch["id"], "video", "clip.mp4", b"video", headers
    ).json()
    path = ctx.client.app.state.settings.videos_dir / body["video_path"]
    monkeypatch.setattr(
        batch_cleanup_module, "remove_checked", lambda *args, **kwargs: (False, "injected")
    )
    assert cancel(ctx, project_id, batch["id"], headers).status_code == 204
    with ctx.session_factory() as db:
        assert db.get(VideoImportBatch, batch["id"]) is None
    assert path.exists()
    log = ctx.client.app.state.settings.cleanup_log.read_text(encoding="utf-8")
    assert "import-batch-delete-failed" in log and "injected" in log


def test_retention_cleanup_uses_24_hour_cutoff_dry_run_and_records_issue(ctx, login_headers):
    headers, project_id = make_project(ctx, login_headers)
    safe = make_batch(ctx, project_id, headers)
    body = upload(ctx, project_id, safe["id"], "video", "clip.mp4", b"video", headers).json()
    failed = make_batch(ctx, project_id, headers)
    unsafe = make_batch(ctx, project_id, headers)
    now = datetime.utcnow()
    with ctx.session_factory() as db:
        db.get(VideoImportBatch, safe["id"]).updated_at = now - timedelta(hours=25)
        failed_row = db.get(VideoImportBatch, failed["id"])
        failed_row.status = "failed"
        failed_row.updated_at = now - timedelta(hours=25)
        bad = db.get(VideoImportBatch, unsafe["id"])
        bad.updated_at = now - timedelta(hours=25)
        bad.video_path = "../outside.mp4"
        bad.video_upload_state = "uploaded"
        db.commit()
        dry = run_retention_cleanup(db, ctx.client.app.state.settings, now=now, dry_run=True)
        assert dry["import_batches_would_delete"] == 2
        assert db.get(VideoImportBatch, safe["id"]) is not None
        report = run_retention_cleanup(db, ctx.client.app.state.settings, now=now)
        assert report["import_batches_deleted"] == 2
        assert db.get(VideoImportBatch, unsafe["id"]) is not None
        assert any(issue["kind"] == "import-batch-cleanup-skipped" for issue in report["issues"])
    assert not (ctx.client.app.state.settings.videos_dir / body["video_path"]).exists()


def test_retention_conservatively_skips_stale_batch_with_active_slot(ctx, login_headers):
    headers, project_id = make_project(ctx, login_headers)
    active = make_batch(ctx, project_id, headers)
    now = datetime.utcnow()
    with ctx.session_factory() as db:
        row = db.get(VideoImportBatch, active["id"])
        row.video_upload_state = "uploading"
        row.updated_at = now - timedelta(hours=25)
        db.commit()
        report = run_retention_cleanup(db, ctx.client.app.state.settings, now=now)
        assert report["import_batches_deleted"] == 0
        assert db.get(VideoImportBatch, active["id"]) is not None
        assert any(issue["kind"] == "import-batch-cleanup-skipped" for issue in report["issues"])
