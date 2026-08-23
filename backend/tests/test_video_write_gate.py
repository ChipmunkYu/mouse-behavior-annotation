"""Real threaded cross-lifecycle races through the shared Video write gate."""
from __future__ import annotations

import json
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from app import models
from app import video_write_gate as gate_module
from app.routers import detection_imports as imports_router
from app.video_write_gate import video_write_gate
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from tests.test_identity_edits import SAMPLE_METADATA, _setup, _split_payload, make_tracks_jsonl


def _setup_annotation(ctx, login_headers):
    headers, project_id, video_id = _setup(ctx, login_headers)
    categories = ctx.client.get(f"/api/projects/{project_id}/categories", headers=headers).json()
    category = next(item for item in categories if item["mouse_count_min"] == 1)
    annotation = ctx.client.post(
        f"/api/projects/{project_id}/videos/{video_id}/annotations",
        json={
            "category_id": category["id"], "start_time": 0.0, "end_time": 0.16,
            "start_frame": 0, "end_frame": 4, "mouse_ids": [1],
        }, headers=headers,
    )
    assert annotation.status_code == 201
    return headers, project_id, video_id, annotation.json()


def _race(left, right):
    barrier = Barrier(2)
    def run(call):
        barrier.wait()
        return call()
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = [executor.submit(run, call) for call in (left, right)]
        return [future.result(timeout=240) for future in responses]


def _synchronize_gate(monkeypatch):
    barrier = Barrier(2)
    monkeypatch.setattr(gate_module, "_before_video_lock", lambda: barrier.wait(timeout=180))


def test_edit_vs_submit_only_one_commits(ctx, login_headers, monkeypatch):
    headers, project_id, video_id, _annotation = _setup_annotation(ctx, login_headers)
    _synchronize_gate(monkeypatch)
    responses = _race(
        lambda: ctx.client.post(
            f"/api/projects/{project_id}/videos/{video_id}/identity-edits",
            json=_split_payload(), headers=headers,
        ),
        lambda: ctx.client.post(
            f"/api/projects/{project_id}/videos/{video_id}/submit", headers=headers
        ),
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    with ctx.session_factory() as db:
        video = db.get(models.Video, video_id)
        detection_import = db.query(models.DetectionImport).filter_by(video_id=video_id, active=True).one()
        assert (video.workflow_status == "submitted") != (detection_import.edit_version == 1)


def test_edit_vs_replacement_only_one_commits(ctx, login_headers, monkeypatch):
    headers, project_id, video_id = _setup(ctx, login_headers)
    _synchronize_gate(monkeypatch)
    with TestClient(ctx.client.app) as edit_client, TestClient(ctx.client.app) as replace_client:
        responses = _race(
            lambda: edit_client.post(
                f"/api/projects/{project_id}/videos/{video_id}/identity-edits",
                json=_split_payload(), headers=headers,
            ),
            lambda: replace_client.post(
                f"/api/projects/{project_id}/videos/{video_id}/detection-imports",
                files={
                    "tracks_file": ("tracks.jsonl", make_tracks_jsonl().encode()),
                    "metadata_file": ("metadata.json", json.dumps(SAMPLE_METADATA).encode()),
                },
                params={"confirm": True}, headers=headers,
            ),
        )
    assert sorted(response.status_code for response in responses) == [200, 409]
    with ctx.session_factory() as db:
        active = db.query(models.DetectionImport).filter_by(video_id=video_id, active=True).one()
        if active.revision == 2:
            assert active.edit_version == 0
            assert db.query(models.DraftIdentityEdit).count() == 0
        else:
            assert active.edit_version == 1
            assert db.query(models.DraftIdentityEdit).count() == 1


@pytest.mark.parametrize("operation", ["update", "create", "delete"])
def test_annotation_write_vs_submit_only_one_commits(ctx, login_headers, monkeypatch, operation):
    headers, project_id, video_id, annotation = _setup_annotation(ctx, login_headers)
    _synchronize_gate(monkeypatch)
    category_id = annotation["category_id"]
    def write():
        base = f"/api/projects/{project_id}/videos/{video_id}/annotations"
        if operation == "update":
            return ctx.client.patch(f"{base}/{annotation['id']}",
                json={"end_frame": 3, "identity_revision": 0}, headers=headers)
        if operation == "delete":
            return ctx.client.delete(f"{base}/{annotation['id']}", headers=headers)
        return ctx.client.post(base, json={"category_id": category_id, "start_time": 0.04,
            "end_time": 0.08, "start_frame": 1, "end_frame": 2, "mouse_ids": [1]},
            headers=headers)
    responses = _race(
        write,
        lambda: ctx.client.post(
            f"/api/projects/{project_id}/videos/{video_id}/submit", headers=headers
        ),
    )
    success = 201 if operation == "create" else (204 if operation == "delete" else 200)
    statuses = [response.status_code for response in responses]
    assert statuses.count(409) == 1
    # Either contender may win: submit returns 200, while the annotation write
    # returns its operation-specific success status.
    assert next(status for status in statuses if status != 409) in {200, success}
    with ctx.session_factory() as db:
        video = db.get(models.Video, video_id)
        stored = db.get(models.Annotation, annotation["id"])
        if video.workflow_status == "submitted":
            assert stored is not None and stored.end_frame == annotation["end_frame"]
        else:
            assert video.annotation_revision == 3
            if operation == "update": assert stored.end_frame == 3
            elif operation == "delete": assert stored is None
            else: assert db.query(models.Annotation).filter_by(video_id=video_id).count() == 2


def test_submitted_hard_locks_annotation_crud_and_replacement(ctx, login_headers):
    headers, project_id, video_id, annotation = _setup_annotation(ctx, login_headers)
    with ctx.session_factory() as db:
        db.get(models.Video, video_id).workflow_status = "submitted"
        db.commit()
    categories = ctx.client.get(f"/api/projects/{project_id}/categories", headers=headers).json()
    category = next(item for item in categories if item["mouse_count_min"] == 1)
    responses = [
        ctx.client.post(
            f"/api/projects/{project_id}/videos/{video_id}/annotations",
            json={"category_id": category["id"], "start_time": 0, "end_time": 0.1,
                  "start_frame": 0, "end_frame": 1, "mouse_ids": [1]}, headers=headers,
        ),
        ctx.client.patch(
            f"/api/projects/{project_id}/videos/{video_id}/annotations/{annotation['id']}",
            json={"end_frame": 3}, headers=headers,
        ),
        ctx.client.delete(
            f"/api/projects/{project_id}/videos/{video_id}/annotations/{annotation['id']}",
            headers=headers,
        ),
        ctx.client.post(
            f"/api/projects/{project_id}/videos/{video_id}/detection-imports",
            files={
                "tracks_file": ("tracks.jsonl", make_tracks_jsonl().encode()),
                "metadata_file": ("metadata.json", json.dumps(SAMPLE_METADATA).encode()),
            }, params={"confirm": True}, headers=headers,
        ),
    ]
    assert [response.status_code for response in responses] == [409, 409, 409, 409]
    with ctx.session_factory() as db:
        assert db.get(models.Video, video_id).workflow_status == "submitted"
        assert db.query(models.Annotation).filter_by(video_id=video_id).count() == 1
        assert db.query(models.DetectionImport).filter_by(video_id=video_id).count() == 1


def test_future_submitted_submission_hard_locks_annotation_crud_and_replacement(
    ctx, login_headers
):
    headers, project_id, video_id, annotation = _setup_annotation(ctx, login_headers)
    with ctx.session_factory() as db:
        detection_import = db.query(models.DetectionImport).filter_by(
            video_id=video_id, active=True
        ).one()
        snapshot = models.DetectionSnapshot(
            detection_import_id=detection_import.id, source_edit_version=0,
            raw_detection_count=12, override_count=0, schema_version=1,
            fps=25, width=1280, height=720, frame_count=5,
            keypoint_names=["nose"], skeleton_edges=[],
        )
        db.add(snapshot)
        db.flush()
        db.add(models.Submission(
            video_id=video_id, detection_snapshot_id=snapshot.id, attempt_no=1,
            source_annotation_version=1, source_media_revision=1,
            source_video_filename="clip.mp4", source_storage_key="videos/clip.mp4",
            source_video_sha256="a" * 64, status="submitted",
            submitted_at=models.utcnow(),
        ))
        db.commit()
    responses = [
        ctx.client.post(
            f"/api/projects/{project_id}/videos/{video_id}/annotations",
            json={"category_id": annotation["category_id"], "start_time": 0,
                  "end_time": .1, "start_frame": 0, "end_frame": 1,
                  "mouse_ids": [1]}, headers=headers,
        ),
        ctx.client.patch(
            f"/api/projects/{project_id}/videos/{video_id}/annotations/{annotation['id']}",
            json={"end_frame": 3}, headers=headers,
        ),
        ctx.client.delete(
            f"/api/projects/{project_id}/videos/{video_id}/annotations/{annotation['id']}",
            headers=headers,
        ),
        ctx.client.post(
            f"/api/projects/{project_id}/videos/{video_id}/detection-imports",
            files={
                "tracks_file": ("tracks.jsonl", make_tracks_jsonl().encode()),
                "metadata_file": ("metadata.json", json.dumps(SAMPLE_METADATA).encode()),
            }, params={"confirm": True}, headers=headers,
        ),
    ]
    assert [response.status_code for response in responses] == [409, 409, 409, 409]


def test_busy_after_lock_maps_409_and_rolls_back(ctx, login_headers, monkeypatch):
    _headers, project_id, video_id = _setup(ctx, login_headers)
    with ctx.session_factory() as db:
        def fail_after_lock():
            db.get(models.Video, video_id).identity_revision = 99
            raise OperationalError("UPDATE videos", {}, Exception("database is locked"))
        monkeypatch.setattr(gate_module, "_after_video_lock", fail_after_lock)
        with pytest.raises(HTTPException) as exc_info:
            with video_write_gate(db, project_id=project_id, video_id=video_id):
                pass
        assert exc_info.value.status_code == 409
    with ctx.session_factory() as db:
        assert db.get(models.Video, video_id).identity_revision == 0


def test_busy_during_commit_maps_409_and_rolls_back(ctx, login_headers, monkeypatch):
    _headers, project_id, video_id = _setup(ctx, login_headers)
    with ctx.session_factory() as db:
        def busy_commit():
            raise OperationalError("COMMIT", {}, Exception("database is busy"))
        monkeypatch.setattr(db, "commit", busy_commit)
        with pytest.raises(HTTPException) as exc_info:
            with video_write_gate(db, project_id=project_id, video_id=video_id) as state:
                state.video.identity_revision = 77
                db.commit()
        assert exc_info.value.status_code == 409
    with ctx.session_factory() as db:
        assert db.get(models.Video, video_id).identity_revision == 0


@pytest.mark.parametrize("competitor", ["edit", "replacement"])
def test_corrected_export_revalidates_before_publish(
    ctx, login_headers, monkeypatch, competitor
):
    headers, project_id, video_id = _setup(ctx, login_headers)
    candidate_ready = Event()
    release_export = Event()
    def pause_after_candidate():
        candidate_ready.set()
        assert release_export.wait(timeout=60)
    monkeypatch.setattr(imports_router, "_after_corrected_export_candidate", pause_after_candidate)
    with TestClient(ctx.client.app) as export_client:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                lambda: export_client.get(
                    f"/api/projects/{project_id}/videos/{video_id}/detections/export",
                    headers=headers,
                )
            )
            assert candidate_ready.wait(timeout=60)
            if competitor == "edit":
                changed = ctx.client.post(
                    f"/api/projects/{project_id}/videos/{video_id}/identity-edits",
                    json=_split_payload(), headers=headers,
                )
            else:
                changed = ctx.client.post(
                    f"/api/projects/{project_id}/videos/{video_id}/detection-imports",
                    files={
                        "tracks_file": ("tracks.jsonl", make_tracks_jsonl().encode()),
                        "metadata_file": ("metadata.json", json.dumps(SAMPLE_METADATA).encode()),
                    }, params={"confirm": True}, headers=headers,
                )
            assert changed.status_code == 200, changed.text
            release_export.set()
            exported = future.result(timeout=90)
    assert exported.status_code == 409
