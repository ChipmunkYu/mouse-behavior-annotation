"""Minimal archived-video boundary regression."""
from datetime import datetime
import hashlib

import pytest
from sqlalchemy import text

from app.export_jobs import asset_rows, enqueue_export_job
from app.media import MediaCommandError
from app.models import BackgroundJob, Clip, Project, Video
from app.video_write_gate import ARCHIVED_DETAIL
from scripts.report_history_archive import RETIRE_REASON, report
from tests.test_project_export import _approved


def _archive(ctx, video_id):
    with ctx.session_factory() as db:
        db.get(Video, video_id).workflow_status = "archived"
        db.commit()


def _protected_records(ctx):
    with ctx.session_factory() as db:
        return {table: [tuple(row) for row in db.execute(text(f'SELECT * FROM "{table}" ORDER BY id'))]
                for table in ("users", "project_memberships", "annotations", "submissions",
                              "submission_annotations", "reviews", "behavior_review_decisions", "clips")}


def _denied(response):
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == ARCHIVED_DETAIL


def test_archived_video_is_hidden_without_deleting_records(media_ctx):
    ctx = media_ctx
    headers, project, _, video, _ = _approved(ctx)
    base = f"/api/projects/{project['id']}"
    before = _protected_records(ctx)

    _archive(ctx, video["id"])

    assert ctx.client.get(f"{base}/videos", headers=headers).json() == []
    assert ctx.client.get(f"{base}/reviews/queue", headers=headers).json() == []
    assert ctx.client.get(f"{base}/clips", headers=headers).json()["total"] == 0
    assert ctx.client.get(f"{base}/clips/categories", headers=headers).json() == []
    assert all(item["possible_total"] == 0 for item in
               ctx.client.get(f"{base}/behavior-stats", headers=headers).json()["items"])
    assert _protected_records(ctx) == before


def test_shared_gate_and_calibration_reject_archived_writes(media_ctx):
    ctx = media_ctx
    headers, project, categories, video, annotations = _approved(ctx)
    base = f"/api/projects/{project['id']}/videos/{video['id']}"
    before = _protected_records(ctx)
    _archive(ctx, video["id"])

    _denied(ctx.client.post(f"{base}/annotations", headers=headers, json={
        "category_id": categories[0]["id"], "start_time": 0, "end_time": .04,
        "start_frame": 0, "end_frame": 1, "mouse_ids": [1],
    }))
    _denied(ctx.client.patch(f"{base}/annotations/{annotations[0]['id']}",
                             headers=headers, json={"confidence": "certain"}))
    _denied(ctx.client.put(f"{base}/distance-calibration", headers=headers, json={
        "point_a": {"x": 1, "y": 1}, "point_b": {"x": 11, "y": 1}, "distance_cm": 10,
    }))
    assert _protected_records(ctx) == before


def test_existing_direct_assignment_writers_cannot_change_archived_video(ctx):
    bundle = ctx.make_project_with_video()
    project, video, headers = bundle["project"], bundle["video"], bundle["headers"]
    _archive(ctx, video["id"])
    base = f"/api/projects/{project['id']}/videos"

    assert ctx.client.post(f"{base}/{video['id']}/claim", headers=headers).status_code == 409
    assert ctx.client.post(f"{base}/assignments", headers=headers,
                           json={"video_ids": [video["id"]]}).status_code == 409
    with ctx.session_factory() as db:
        stored = db.get(Video, video["id"])
        assert stored.workflow_status == "archived"
        assert stored.assignee_membership_id is None


def test_new_export_excludes_archived_and_frozen_job_rechecks(media_ctx):
    ctx = media_ctx
    headers, project, _, video, _ = _approved(ctx)
    with ctx.session_factory() as db:
        job = enqueue_export_job(db, db.get(Project, project["id"]), None)
        job_id = job.id
    _archive(ctx, video["id"])

    with ctx.session_factory() as db:
        assert asset_rows(db, project["id"], None) == []
        with pytest.raises(MediaCommandError, match="no longer exists"):
            ctx.app.state.export_worker._frozen_rows(
                db, db.get(BackgroundJob, job_id), require_ready=False)
    response = ctx.client.post(f"/api/projects/{project['id']}/export", json={}, headers=headers)
    assert response.status_code == 400


def test_nonarchived_export_subset_still_works(media_ctx):
    ctx = media_ctx
    headers, project, categories, archived, _ = _approved(ctx)
    from tests.test_reviews import (_annotate_with_mouse, _make_metadata_json, _make_tracks_jsonl,
                                    _review, _submit)
    base = f"/api/projects/{project['id']}"
    batch = ctx.client.post(f"{base}/video-import-batches", headers=headers).json()
    for slot, name, content in (("video", "new.mp4", b"FAKE-MP4"),
                                ("tracks", "tracks.jsonl", _make_tracks_jsonl().encode()),
                                ("metadata", "metadata.json", _make_metadata_json().encode())):
        assert ctx.client.put(f"{base}/video-import-batches/{batch['id']}/files/{slot}",
                              files={"file": (name, content)}, headers=headers).status_code == 200
    completed = ctx.client.post(f"{base}/video-import-batches/{batch['id']}/complete",
                                headers=headers).json()
    current = {"id": completed["video_id"]}
    _annotate_with_mouse(ctx, headers, project, current, categories[1]["id"], mouse_ids=[1])
    assert _submit(ctx, headers, project, current).status_code == 200
    assert _review(ctx, headers, project, current, "approved").status_code == 200
    _archive(ctx, archived["id"])

    response = ctx.client.post(f"{base}/export", json={"category_ids": [categories[1]["id"]]},
                               headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "succeeded"


def test_candidate_report_is_readonly_dry_run_without_count_threshold(media_ctx):
    ctx = media_ctx
    ctx.make_project_with_video()
    _, project, _, video, _ = _approved(ctx)
    assert project["id"] == 2
    with ctx.session_factory() as db:
        clip = db.query(Clip).filter_by(project_id=2).one()
        clip.retired_at = datetime(2100, 1, 1)
        clip.retired_reason = RETIRE_REASON
        db.commit()
    path = ctx.app.state.settings.data_dir / "media.db"
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    result = report(path)

    assert result["status"] == "dry-run"
    assert result["readonly"] is True
    assert result["videos"][0]["classification"] == "无法确定"
    assert result["videos"][0]["id"] == video["id"]
    assert "duration" in result["videos"][0]
    assert result["videos"][0]["annotations"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
