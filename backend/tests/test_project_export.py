"""批次 6：项目级分类 ZIP 导出、权限、隔离、补生成与下载安全。"""
from __future__ import annotations

import io
import json
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.export_jobs import enqueue_export_job, export_dedupe_key
from app.media import MediaCommandError
from app.media_jobs import claim_and_render_clip, media_dedupe_key
from app.models import Annotation, BackgroundJob, Clip, Project, Video
from tests.conftest import auth_headers


def _setup(ctx, *, count: int = 2, ready: int = 0):
    setup = ctx.make_project_with_video("导出测试")
    headers = setup["headers"]
    project = setup["project"]
    video = setup["video"]
    categories = setup["categories"][:count]
    source = ctx.app.state.settings.videos_dir / "export-source.mp4"
    source.write_bytes(b"SOURCE")
    annotation_ids = []
    for index, category in enumerate(categories):
        response = ctx.client.post(
            f"/api/projects/{project['id']}/videos/{video['id']}/annotations",
            json={
                "category_id": category["id"],
                "start_time": float(index + 1),
                "end_time": float(index + 2),
                "start_frame": (index + 1) * 25,
                "end_frame": (index + 2) * 25,
            },
            headers=headers,
        )
        assert response.status_code == 201
        annotation_ids.append(response.json()["id"])
    with ctx.session_factory() as db:
        row = db.get(Video, video["id"])
        row.storage_path = source.name
        row.workflow_status = "approved"
        for index, annotation_id in enumerate(annotation_ids):
            annotation = db.get(Annotation, annotation_id)
            annotation.review_status = "approved"
            if index < ready:
                name = f"existing_{annotation_id}.mp4"
                thumb_name = f"existing_{annotation_id}.jpg"
                (ctx.app.state.settings.clips_dir / name).write_bytes(b"EXISTING")
                (ctx.app.state.settings.thumbnails_dir / thumb_name).write_bytes(b"THUMB")
                db.add(
                    Clip(
                        project_id=project["id"],
                        annotation_id=annotation_id,
                        source_revision=row.annotation_revision,
                        status="ready",
                        clip_path=name,
                        thumbnail_path=thumb_name,
                    )
                )
        db.commit()
    return headers, project, video, categories, annotation_ids


def _post(ctx, project_id: int, headers: dict, category_ids=None):
    body = {} if category_ids is None else {"category_ids": category_ids}
    return ctx.client.post(f"/api/projects/{project_id}/export", json=body, headers=headers)


def test_first_export_generates_real_zip_annotations_and_download(media_ctx):
    ctx = media_ctx
    headers, project, _video, categories, annotation_ids = _setup(ctx)

    response = _post(ctx, project["id"], headers)
    assert response.status_code == 201
    job = response.json()
    assert job["status"] == "succeeded"
    assert job["result_path"] == f"export_project_{project['id']}_{job['id']}.zip"
    assert len(ctx.processor.clip_calls) == 2

    archive = ctx.app.state.settings.exports_dir / job["result_path"]
    assert archive.is_file()
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        assert "annotations.json" in names
        assert sum(name.endswith(".mp4") for name in names) == 2
        for category in categories:
            assert any(
                name.startswith(f"{category['group']}/{category['name']}/") for name in names
            )
        events = json.loads(zf.read("annotations.json"))
        assert len(events) == 2
        assert {event["behavior"] for event in events} == {c["name"] for c in categories}
        assert all(event["review_status"] == "approved" for event in events)

    status = ctx.client.get(
        f"/api/projects/{project['id']}/export/status", headers=headers
    ).json()
    assert status["latest_job"]["id"] == job["id"]
    assert (status["exportable_count"], status["ready_count"], status["missing_count"]) == (
        2,
        2,
        0,
    )
    download = ctx.client.get(
        f"/api/projects/{project['id']}/export/download", headers=headers
    )
    assert download.status_code == 200
    assert download.content == archive.read_bytes()
    assert zipfile.is_zipfile(io.BytesIO(download.content))
    assert set(annotation_ids) == {
        row.annotation_id
        for row in _clips(ctx, project["id"])
    }


def _clips(ctx, project_id: int):
    with ctx.session_factory() as db:
        return db.query(Clip).filter(Clip.project_id == project_id).all()


def test_category_filter_controls_zip_and_latest_status_scope(media_ctx):
    ctx = media_ctx
    headers, project, _video, categories, _annotation_ids = _setup(ctx)
    selected = categories[1]
    response = _post(ctx, project["id"], headers, [selected["id"]])
    assert response.status_code == 201
    job = response.json()
    assert job["payload"]["category_ids"] == [selected["id"]]

    status = ctx.client.get(
        f"/api/projects/{project['id']}/export/status", headers=headers
    ).json()
    assert status["exportable_count"] == status["ready_count"] == 1
    with zipfile.ZipFile(ctx.app.state.settings.exports_dir / job["result_path"]) as zf:
        events = json.loads(zf.read("annotations.json"))
        assert [event["behavior"] for event in events] == [selected["name"]]
        assert sum(name.endswith(".mp4") for name in zf.namelist()) == 1


def test_export_active_job_is_exclusive_and_rerun_keeps_history(media_ctx):
    ctx = media_ctx
    headers, project, _video, _categories, _annotation_ids = _setup(ctx, count=1)
    first = _post(ctx, project["id"], headers).json()
    second_response = _post(ctx, project["id"], headers)
    assert second_response.status_code == 201
    second = second_response.json()
    assert second["id"] != first["id"]
    assert second["result_path"] != first["result_path"]
    assert (ctx.app.state.settings.exports_dir / first["result_path"]).is_file()
    assert (ctx.app.state.settings.exports_dir / second["result_path"]).is_file()
    with ctx.session_factory() as db:
        history = (
            db.query(BackgroundJob)
            .filter(
                BackgroundJob.project_id == project["id"],
                BackgroundJob.job_type == "export",
            )
            .all()
        )
        assert len(history) == 2
        db.add(
            BackgroundJob(
                project_id=project["id"],
                job_type="export",
                status="running",
                dedupe_key=export_dedupe_key(project["id"]),
                payload={"project_id": project["id"], "category_ids": []},
            )
        )
        db.commit()
    assert _post(ctx, project["id"], headers).status_code == 409


def test_concurrent_sessions_create_only_one_active_export(media_ctx):
    ctx = media_ctx
    _headers, project, _video, _categories, _annotation_ids = _setup(ctx, count=1)
    barrier = threading.Barrier(2)
    results = []

    def enqueue():
        with ctx.session_factory() as db:
            target = db.get(Project, project["id"])
            barrier.wait()
            results.append(enqueue_export_job(db, target, []))

    threads = [threading.Thread(target=enqueue) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert sum(job is not None for job in results) == 1


def test_media_and_export_workers_share_one_clip_render_owner(media_ctx):
    ctx = media_ctx
    _headers, project, video, _categories, annotation_ids = _setup(ctx, count=1)
    entered = threading.Event()
    release = threading.Event()
    original_render = ctx.processor.render_clip

    def blocking_render(**kwargs):
        entered.set()
        assert release.wait(5)
        original_render(**kwargs)

    ctx.processor.render_clip = blocking_render
    with ctx.session_factory() as db:
        clip = Clip(
            project_id=project["id"],
            annotation_id=annotation_ids[0],
            source_revision=1,
            status="pending",
        )
        db.add(clip)
        media_job = BackgroundJob(
            project_id=project["id"],
            job_type="media",
            status="queued",
            dedupe_key=media_dedupe_key(video["id"], 1),
            payload={"video_id": video["id"], "project_id": project["id"], "revision": 1},
        )
        export_job = BackgroundJob(
            project_id=project["id"],
            job_type="export",
            status="queued",
            dedupe_key=export_dedupe_key(project["id"]),
            payload={
                "project_id": project["id"],
                "category_ids": [],
                "annotation_ids": annotation_ids,
                "video_revisions": {str(video["id"]): 1},
            },
        )
        db.add_all([media_job, export_job])
        db.commit()
        media_job_id, export_job_id = media_job.id, export_job.id

    media_thread = threading.Thread(
        target=ctx.app.state.media_worker._run_job, args=(media_job_id,)
    )
    export_thread = threading.Thread(
        target=ctx.app.state.export_worker._run_job, args=(export_job_id,)
    )
    media_thread.start()
    assert entered.wait(5)
    export_thread.start()
    time.sleep(0.2)
    assert len(ctx.processor.clip_calls) == 0
    release.set()
    media_thread.join(timeout=10)
    export_thread.join(timeout=10)
    assert not media_thread.is_alive() and not export_thread.is_alive()
    assert len(ctx.processor.clip_calls) == 1
    with ctx.session_factory() as db:
        clip = db.query(Clip).one()
        assert clip.status == "ready"
        assert (ctx.app.state.settings.clips_dir / clip.clip_path).is_file()
        assert (ctx.app.state.settings.thumbnails_dir / clip.thumbnail_path).is_file()
        assert db.get(BackgroundJob, media_job_id).status == "succeeded"
        assert db.get(BackgroundJob, export_job_id).status == "succeeded"


def test_clip_wait_timeout_does_not_release_another_render_owners_claim(media_ctx):
    ctx = media_ctx
    _headers, project, video, _categories, annotation_ids = _setup(ctx, count=1)
    entered = threading.Event()
    release = threading.Event()
    owner_result = []
    owner_errors = []
    original_render = ctx.processor.render_clip

    def blocking_render(**kwargs):
        original_render(**kwargs)
        entered.set()
        assert release.wait(5)

    ctx.processor.render_clip = blocking_render
    with ctx.session_factory() as db:
        clip = Clip(
            project_id=project["id"],
            annotation_id=annotation_ids[0],
            source_revision=1,
            status="pending",
        )
        db.add(clip)
        db.commit()
        clip_id = clip.id

    def render_as_owner():
        try:
            with ctx.session_factory() as db:
                owner_result.append(
                    claim_and_render_clip(
                        db,
                        ctx.processor,
                        ctx.app.state.settings,
                        video["id"],
                        annotation_ids[0],
                        clip_id,
                        wait_seconds=1,
                        poll_seconds=0.005,
                    )
                )
        except Exception as exc:  # pragma: no cover - asserted below
            owner_errors.append(exc)

    owner_thread = threading.Thread(target=render_as_owner)
    owner_thread.start()
    assert entered.wait(5)

    with ctx.session_factory() as db:
        with pytest.raises(MediaCommandError, match="timed out waiting"):
            claim_and_render_clip(
                db,
                ctx.processor,
                ctx.app.state.settings,
                video["id"],
                annotation_ids[0],
                clip_id,
                wait_seconds=0.02,
                poll_seconds=0.005,
            )
    with ctx.session_factory() as db:
        clip = db.get(Clip, clip_id)
        assert clip.status == "processing"
        assert clip.clip_path is None
        assert clip.thumbnail_path is None
        assert clip.error is None
    assert len(ctx.processor.clip_calls) == 1

    release.set()
    owner_thread.join(timeout=5)
    assert not owner_thread.is_alive()
    assert owner_errors == []
    assert len(owner_result) == 1
    with ctx.session_factory() as db:
        clip = db.get(Clip, clip_id)
        assert clip.status == "ready"
        assert (ctx.app.state.settings.clips_dir / clip.clip_path).is_file()
        assert (ctx.app.state.settings.thumbnails_dir / clip.thumbnail_path).is_file()
        reused_path, created = claim_and_render_clip(
            db,
            ctx.processor,
            ctx.app.state.settings,
            video["id"],
            annotation_ids[0],
            clip_id,
            wait_seconds=0,
            poll_seconds=0.005,
        )
        assert reused_path.is_file()
        assert created == []
    assert len(ctx.processor.clip_calls) == 1


def test_restart_requeues_export_and_recovers_its_processing_clip(media_ctx):
    ctx = media_ctx
    _headers, project, video, _categories, annotation_ids = _setup(ctx, count=1)
    with ctx.session_factory() as db:
        clip = Clip(
            project_id=project["id"],
            annotation_id=annotation_ids[0],
            source_revision=1,
            status="processing",
        )
        job = BackgroundJob(
            project_id=project["id"],
            job_type="export",
            status="running",
            dedupe_key=export_dedupe_key(project["id"]),
            payload={
                "project_id": project["id"],
                "category_ids": [],
                "annotation_ids": annotation_ids,
                "video_revisions": {str(video["id"]): 1},
            },
        )
        db.add_all([clip, job])
        db.commit()
        clip_id, job_id = clip.id, job.id

    ctx.app.state.export_worker.start()

    with ctx.session_factory() as db:
        assert db.get(Clip, clip_id).status == "ready"
        recovered = db.get(BackgroundJob, job_id)
        assert recovered.status == "succeeded"
        assert recovered.attempts == 2
        assert (ctx.app.state.settings.exports_dir / recovered.result_path).is_file()
    assert len(ctx.processor.clip_calls) == 1


def test_export_rechecks_revision_at_publish_and_does_not_publish(media_ctx):
    ctx = media_ctx
    _headers, project, video, _categories, annotation_ids = _setup(ctx, count=1)
    with ctx.session_factory() as db:
        job = enqueue_export_job(db, db.get(Project, project["id"]), [])
        job_id = job.id

    entered = threading.Event()
    release = threading.Event()

    def before_publish():
        entered.set()
        assert release.wait(5)

    ctx.app.state.export_worker.before_publish_hook = before_publish
    thread = threading.Thread(target=ctx.app.state.export_worker._run_job, args=(job_id,))
    thread.start()
    assert entered.wait(5)
    with ctx.session_factory() as db:
        row = db.get(Video, video["id"])
        row.annotation_revision += 1
        row.workflow_status = "draft"
        db.get(Annotation, annotation_ids[0]).review_status = "pending"
        db.commit()
    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    with ctx.session_factory() as db:
        job = db.get(BackgroundJob, job_id)
        assert job.status == "failed"
        assert job.result_path is None
    assert list(ctx.app.state.settings.exports_dir.glob("*.zip")) == []


def test_export_permissions_project_category_and_job_isolation(media_ctx):
    ctx = media_ctx
    headers, project, _video, _categories, _annotation_ids = _setup(ctx, count=1)
    other = ctx.make_project_with_video("其他导出项目")
    annotator_id = ctx.create_user("export-annotator")
    ctx.add_member(project["id"], annotator_id, "annotator")
    annotator_headers = auth_headers(ctx.client, "export-annotator", "pw123")
    assert _post(ctx, project["id"], annotator_headers).status_code == 403
    assert ctx.client.get(
        f"/api/projects/{project['id']}/export/status", headers=annotator_headers
    ).status_code == 403

    foreign_category = other["categories"][0]["id"]
    assert _post(ctx, project["id"], headers, [foreign_category]).status_code == 400
    job = _post(ctx, project["id"], headers).json()
    assert ctx.client.get(
        f"/api/projects/{project['id']}/jobs/{job['id']}", headers=headers
    ).status_code == 200
    assert ctx.client.get(
        f"/api/projects/{project['id']}/jobs/{job['id']}", headers=annotator_headers
    ).status_code == 403
    assert ctx.client.get(
        f"/api/projects/{other['project']['id']}/jobs/{job['id']}",
        headers=other["headers"],
    ).status_code == 404


def test_status_requires_current_ready_file_and_reports_missing(media_ctx):
    ctx = media_ctx
    headers, project, _video, categories, annotation_ids = _setup(ctx, ready=1)
    with ctx.session_factory() as db:
        db.add(
            Clip(
                project_id=project["id"],
                annotation_id=annotation_ids[1],
                source_revision=1,
                status="ready",
                clip_path="../outside.mp4",
            )
        )
        db.commit()
    status = ctx.client.get(
        f"/api/projects/{project['id']}/export/status", headers=headers
    ).json()
    assert status["exportable_count"] == 2
    assert status["ready_count"] == 1
    assert status["missing_count"] == 1
    assert status["missing_clips"] == [
        {
            "annotation_id": annotation_ids[1],
            "category_name": categories[1]["name"],
            "video_filename": "session1.mp4",
        }
    ]
    valid = ctx.app.state.settings.clips_dir / f"existing_{annotation_ids[0]}.mp4"
    valid.unlink()
    status = ctx.client.get(
        f"/api/projects/{project['id']}/export/status", headers=headers
    ).json()
    assert status["ready_count"] == 0
    assert status["missing_count"] == 2


def test_missing_clip_failure_does_not_publish_archive(media_ctx):
    ctx = media_ctx
    headers, project, _video, _categories, annotation_ids = _setup(ctx, count=1)
    ctx.processor.fail_clips.add(annotation_ids[0])
    response = _post(ctx, project["id"], headers)
    assert response.status_code == 201
    job = response.json()
    assert job["status"] == "failed"
    assert job["result_path"] is None
    assert list(ctx.app.state.settings.exports_dir.glob("*.zip")) == []
    assert ctx.client.get(
        f"/api/projects/{project['id']}/export/download", headers=headers
    ).status_code == 404


def test_download_rejects_expired_missing_and_out_of_bounds_results(media_ctx):
    ctx = media_ctx
    headers, project, _video, _categories, _annotation_ids = _setup(ctx, count=1)
    job_id = _post(ctx, project["id"], headers).json()["id"]
    with ctx.session_factory() as db:
        job = db.get(BackgroundJob, job_id)
        job.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    url = f"/api/projects/{project['id']}/export/download"
    assert ctx.client.get(url, headers=headers).status_code == 404

    with ctx.session_factory() as db:
        job = db.get(BackgroundJob, job_id)
        job.expires_at = datetime.utcnow() + timedelta(days=1)
        job.result_path = "../outside.zip"
        db.commit()
    assert ctx.client.get(url, headers=headers).status_code == 404
    with ctx.session_factory() as db:
        job = db.get(BackgroundJob, job_id)
        job.result_path = "missing.zip"
        db.commit()
    assert ctx.client.get(url, headers=headers).status_code == 404


def test_only_approved_annotation_and_approved_video_are_exported(media_ctx):
    ctx = media_ctx
    headers, project, video, _categories, annotation_ids = _setup(ctx)
    with ctx.session_factory() as db:
        db.get(Annotation, annotation_ids[1]).review_status = "pending"
        db.commit()
    first = _post(ctx, project["id"], headers).json()
    with zipfile.ZipFile(ctx.app.state.settings.exports_dir / first["result_path"]) as zf:
        assert len(json.loads(zf.read("annotations.json"))) == 1
    with ctx.session_factory() as db:
        db.get(Video, video["id"]).workflow_status = "draft"
        db.commit()
    second = _post(ctx, project["id"], headers).json()
    with zipfile.ZipFile(ctx.app.state.settings.exports_dir / second["result_path"]) as zf:
        assert json.loads(zf.read("annotations.json")) == []
