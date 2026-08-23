"""Phase 3 immutable Submission/Snapshot lifecycle evidence."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import os
import time
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import (Annotation, BackgroundJob, BehaviorCategory, Clip, DetectionImport,
                         DetectionSnapshot, DetectionSnapshotState, RawDetection, Submission,
                         SubmissionAnnotation, User)
from app import video_write_gate as gate_module
from app import media_jobs as media_jobs_module
from app.submission_service import delete_raw_baseline
from fastapi.testclient import TestClient
from fastapi import HTTPException
from .conftest import auth_headers
from .test_reviews import (_add_reviewer, _annotate_with_mouse, _review, _setup_video_with_import,
                           _submit)
from app.submission_media_plan import build_submission_media_plan
from app.frame_intervals import canonical_frame_interval
from app.file_identity import hash_file_handle


def _ready(ctx, login_headers):
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)
    category = next(c for c in categories if c["mouse_count_min"] == 1 and c["mouse_count_max"] == 1)
    annotation = _annotate_with_mouse(ctx, headers, project, video, category["id"], mouse_ids=[1])
    return headers, project, video, annotation


def _race(monkeypatch, left, right):
    barrier = Barrier(2)
    monkeypatch.setattr(gate_module, "_before_video_lock", lambda: barrier.wait(timeout=180))
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(call) for call in (left, right)]
        return [future.result(timeout=240) for future in futures]


def test_submit_vs_submit_real_thread_race_has_one_clean_winner(ctx, login_headers, monkeypatch):
    headers, project, video, _annotation = _ready(ctx, login_headers)
    url = f"/api/projects/{project['id']}/videos/{video['id']}/submit"
    with TestClient(ctx.client.app) as left, TestClient(ctx.client.app) as right:
        responses = _race(monkeypatch, lambda: left.post(url, headers=headers),
                          lambda: right.post(url, headers=headers))
    assert sorted(response.status_code for response in responses) == [200, 409]
    with ctx.session_factory() as db:
        assert db.query(Submission).count() == 1
        assert db.query(SubmissionAnnotation).count() == 1
        assert db.query(DetectionSnapshot).count() == 1
        assert db.query(DetectionSnapshotState).count() == 0
        assert db.query(Clip).count() == db.query(BackgroundJob).count() == 0
        assert db.query(Submission).one().attempt_no == 1


def test_withdraw_vs_review_real_thread_race_has_one_clean_winner(ctx, login_headers, monkeypatch):
    headers, project, video, _annotation = _ready(ctx, login_headers)
    _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")
    assert _submit(ctx, headers, project, video).status_code == 200
    withdraw_url = f"/api/projects/{project['id']}/videos/{video['id']}/withdraw"
    review_url = f"/api/projects/{project['id']}/videos/{video['id']}/review"
    with TestClient(ctx.client.app) as left, TestClient(ctx.client.app) as right:
        responses = _race(monkeypatch, lambda: left.post(withdraw_url, headers=headers),
                          lambda: right.post(review_url, json={"result": "approved"},
                                             headers=reviewer_headers))
    assert sorted(response.status_code for response in responses) == [200, 409]
    with ctx.session_factory() as db:
        submission = db.query(Submission).one()
        assert submission.status in {"withdrawn", "approved"}
        expected = 1 if submission.status == "approved" else 0
        assert db.query(Clip).count() == db.query(BackgroundJob).count() == expected
        assert submission.attempt_no == 1


def test_review_vs_review_real_thread_race_has_one_clean_winner(ctx, login_headers, monkeypatch):
    headers, project, video, _annotation = _ready(ctx, login_headers)
    _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")
    assert _submit(ctx, headers, project, video).status_code == 200
    url = f"/api/projects/{project['id']}/videos/{video['id']}/review"
    with TestClient(ctx.client.app) as left, TestClient(ctx.client.app) as right:
        responses = _race(monkeypatch,
                          lambda: left.post(url, json={"result": "approved"}, headers=reviewer_headers),
                          lambda: right.post(url, json={"result": "rejected"}, headers=reviewer_headers))
    assert sorted(response.status_code for response in responses) == [200, 409]
    with ctx.session_factory() as db:
        submission = db.query(Submission).one()
        assert submission.status in {"approved", "rejected"}
        assert submission.attempt_no == 1
        assert submission.review is not None
        expected = 1 if submission.status == "approved" else 0
        assert db.query(Clip).count() == db.query(BackgroundJob).count() == expected


def test_submit_creates_immutable_authority_and_reuses_snapshot(ctx, login_headers):
    headers, project, video, annotation = _ready(ctx, login_headers)
    assert _submit(ctx, headers, project, video).status_code == 200
    assert ctx.client.post(f"/api/projects/{project['id']}/videos/{video['id']}/withdraw", headers=headers).status_code == 200
    assert _submit(ctx, headers, project, video).status_code == 200
    with ctx.session_factory() as db:
        attempts = db.query(Submission).order_by(Submission.attempt_no).all()
        assert [row.status for row in attempts] == ["withdrawn", "submitted"]
        assert [row.attempt_no for row in attempts] == [1, 2]
        assert attempts[0].detection_snapshot_id == attempts[1].detection_snapshot_id
        assert db.query(DetectionSnapshot).count() == 1
        assert db.query(SubmissionAnnotation).count() == 2
        copies = db.query(SubmissionAnnotation).order_by(SubmissionAnnotation.id).all()
        assert copies[0].source_annotation_id == copies[1].source_annotation_id == annotation["id"]


def test_submit_renormalizes_live_times_from_authoritative_frames(ctx, login_headers):
    headers, project, video, annotation = _ready(ctx, login_headers)
    with ctx.session_factory() as db:
        source = db.get(Annotation, annotation["id"])
        source.start_time = 7.75
        source.end_time = 8.25
        db.commit()
    response = _submit(ctx, headers, project, video)
    assert response.status_code == 200, response.text
    with ctx.session_factory() as db:
        source = db.get(Annotation, annotation["id"])
        frozen = db.query(SubmissionAnnotation).one()
        assert source.start_time == frozen.start_time == pytest.approx(source.start_frame / 25)
        assert source.end_time == frozen.end_time == pytest.approx((source.end_frame + 1) / 25)


def test_review_uses_copy_and_approval_enqueues_atomically(ctx, login_headers):
    headers, project, video, annotation = _ready(ctx, login_headers)
    reviewer_id = _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")
    assert _submit(ctx, headers, project, video).status_code == 200
    with ctx.session_factory() as db:
        source = db.get(__import__("app.models", fromlist=["Annotation"]).Annotation, annotation["id"])
        source.start_time = 99.0
        db.commit()
    response = _review(ctx, reviewer_headers, project, video, "approved")
    assert response.status_code == 200
    with ctx.session_factory() as db:
        submission = db.query(Submission).filter_by(video_id=video["id"], status="approved").one()
        copy = db.query(SubmissionAnnotation).filter_by(submission_id=submission.id).one()
        assert copy.start_time == 0.0
        clips = db.query(Clip).all()
        assert len(clips) == 1 and clips[0].submission_annotation_id == copy.id
        assert clips[0].annotation_id is None and clips[0].project_id is None
        job = db.query(BackgroundJob).one()
        assert job.payload == {"submission_id": submission.id, "submission_annotation_ids": [copy.id]}


def test_submission_worker_passes_identical_crop_to_clip_and_thumbnail(media_ctx):
    ctx = media_ctx
    login = lambda username="demo", password="demo123": auth_headers(ctx.client, username, password)
    headers, project, video, annotation = _ready(ctx, login)
    crop = {"x": 10, "y": 20, "w": 100, "h": 80}
    response = ctx.client.patch(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations/{annotation['id']}",
        json={"crop_region": crop}, headers=headers,
    )
    assert response.status_code == 200
    _add_reviewer(ctx, project["id"])
    assert _submit(ctx, headers, project, video).status_code == 200
    response = _review(ctx, login(username="reviewer1", password="pw123"),
                       project, video, "approved")
    assert response.status_code == 200
    assert ctx.processor.clip_crops[-1] == (10, 20, 100, 80)
    assert ctx.processor.thumb_crops[-1] == (10, 20, 100, 80)


def test_raw_baseline_damage_blocks_review(ctx, login_headers):
    headers, project, video, _annotation = _ready(ctx, login_headers)
    _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")
    assert _submit(ctx, headers, project, video).status_code == 200
    with ctx.session_factory() as db:
        raw = db.query(RawDetection).first()
        raw_id = raw.id
        with pytest.raises(IntegrityError):
            db.execute(RawDetection.__table__.delete().where(RawDetection.id == raw_id))
        db.rollback()
        assert db.get(RawDetection, raw_id) is not None
    with ctx.session_factory() as db:
        assert db.query(Submission).filter_by(status="submitted").count() == 1
        assert db.query(BackgroundJob).count() == db.query(Clip).count() == 0


def test_raw_insert_is_rejected_after_snapshot_but_allowed_without_snapshot(ctx, login_headers):
    headers, project, video, _annotation = _ready(ctx, login_headers)
    with ctx.session_factory() as db:
        imp = db.query(DetectionImport).filter_by(video_id=video["id"], active=True).one()
        db.add(RawDetection(detection_import_id=imp.id, frame_index=99,
                            frame_detection_index=0, raw_track_id=99))
        db.commit()
    assert _submit(ctx, headers, project, video).status_code == 409  # count metadata caught added row
    with ctx.session_factory() as db:
        imp = db.query(DetectionImport).filter_by(video_id=video["id"], active=True).one()
        imp.detection_count += 1
        db.commit()
    assert _submit(ctx, headers, project, video).status_code == 200
    with ctx.session_factory() as db:
        imp = db.query(DetectionImport).filter_by(video_id=video["id"], active=True).one()
        db.add(RawDetection(detection_import_id=imp.id, frame_index=100,
                            frame_detection_index=0, raw_track_id=100))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        with pytest.raises(IntegrityError):
            db.execute(text(
                "INSERT INTO raw_detections "
                "(detection_import_id,frame_index,frame_detection_index,raw_track_id) "
                "VALUES (:import_id,101,0,101)"
            ), {"import_id": imp.id})
        db.rollback()


def test_hash_file_handle_binds_identity_to_opened_descriptor(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    source.write_bytes(b"verified-bytes")
    digest, identity = hash_file_handle(source, chunk_size=2)
    assert digest == __import__("hashlib").sha256(b"verified-bytes").hexdigest()
    assert identity.size == len(b"verified-bytes")


def test_shared_raw_baseline_delete_guard_blocks_snapshot_reference(ctx, login_headers):
    headers, project, video, _annotation = _ready(ctx, login_headers)
    assert _submit(ctx, headers, project, video).status_code == 200
    with ctx.session_factory() as db:
        snapshot = db.query(DetectionSnapshot).one()
        before = db.query(RawDetection).filter_by(
            detection_import_id=snapshot.detection_import_id).count()
        try:
            delete_raw_baseline(db, detection_import_id=snapshot.detection_import_id)
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("snapshot-referenced raw baseline deletion was not rejected")
        db.rollback()
        assert db.query(RawDetection).filter_by(
            detection_import_id=snapshot.detection_import_id).count() == before


def test_snapshot_same_count_content_tamper_blocks_review(ctx, login_headers):
    headers, project, video, _annotation = _ready(ctx, login_headers)
    _add_reviewer(ctx, project["id"])
    assert _submit(ctx, headers, project, video).status_code == 200
    with ctx.session_factory() as db:
        raw = db.query(RawDetection).first()
        original_track_id = raw.raw_track_id
        raw.raw_track_id += 100
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert db.get(RawDetection, raw.id).raw_track_id == original_track_id


@pytest.mark.parametrize("fps", [25.0, 30.0, 60.0])
def test_submission_media_plan_inclusive_frames_and_crop(fps):
    end_frame = int(fps) + 9
    plan = build_submission_media_plan(start_time=1.0, end_time=1.4, start_frame=25,
        end_frame=end_frame, fps=fps, frame_count=int(fps) * 3, width=1280, height=720,
        crop_region={"x": 10, "y": 20, "w": 300, "h": 200})
    assert plan.start == pytest.approx(25 / fps)
    assert plan.end == pytest.approx((end_frame + 1) / fps)
    assert plan.thumbnail_at == pytest.approx((plan.start + plan.end) / 2)
    assert plan.crop == (10, 20, 300, 200)
    assert (plan.output_width, plan.output_height) == (300, 200)


@pytest.mark.parametrize("fps", [25.0, 30.0, 60.0])
def test_frame_authority_first_last_and_single_frame(fps):
    frame_count = int(fps) * 2
    interval = canonical_frame_interval(
        start_frame=0, end_frame=frame_count - 1, fps=fps, frame_count=frame_count
    )
    assert interval.start_time == 0.0
    assert interval.end_time == pytest.approx(2.0)
    assert interval.frame_count == frame_count
    with pytest.raises(ValueError):
        canonical_frame_interval(start_frame=0, end_frame=0, fps=fps, frame_count=frame_count)
    with pytest.raises(ValueError):
        canonical_frame_interval(
            start_frame=frame_count - 1, end_frame=frame_count, fps=fps,
            frame_count=frame_count,
        )


@pytest.mark.parametrize("kwargs", [
    dict(start_time=9.0, end_time=1.4, start_frame=25, end_frame=25),
    dict(start_time=1.0, end_time=1.4, start_frame=25, end_frame=100),
])
def test_submission_media_plan_rejects_invalid_frame_boundaries(kwargs):
    with pytest.raises(ValueError):
        build_submission_media_plan(**kwargs, fps=25.0, frame_count=100,
                                    width=1280, height=720, crop_region=None)


def test_submit_rejects_metadata_sha256_mismatch(ctx, login_headers):
    headers, project, video, _annotation = _ready(ctx, login_headers)
    with ctx.session_factory() as db:
        imp = db.query(DetectionImport).filter_by(video_id=video["id"], active=True).one()
        imp.metadata_sha256 = "0" * 64
        db.commit()
    response = _submit(ctx, headers, project, video)
    assert response.status_code == 409
    assert "metadata sha-256" in response.json()["detail"].lower()
    with ctx.session_factory() as db:
        assert db.query(Submission).count() == 0


def _queue_submission_media(ctx, login, monkeypatch, *, annotations=1):
    headers, project, video, first = _ready(ctx, login)
    category_id = first["category_id"]
    for _ in range(annotations - 1):
        _annotate_with_mouse(ctx, headers, project, video, category_id, mouse_ids=[1])
    _add_reviewer(ctx, project["id"])
    assert _submit(ctx, headers, project, video).status_code == 200
    monkeypatch.setattr(ctx.app.state.media_worker, "schedule", lambda _job_id: None)
    reviewer = login(username="reviewer1", password="pw123")
    assert _review(ctx, reviewer, project, video, "approved").status_code == 200
    with ctx.session_factory() as db:
        return headers, reviewer, project, video, db.query(BackgroundJob).one().id


def test_submission_worker_rejects_replaced_source_and_leaves_clip_not_ready(media_ctx, monkeypatch):
    ctx = media_ctx
    login = lambda username="demo", password="demo123": auth_headers(ctx.client, username, password)
    _headers, _reviewer, _project, _video, job_id = _queue_submission_media(ctx, login, monkeypatch)
    with ctx.session_factory() as db:
        submission = db.query(Submission).one()
        source = ctx.app.state.settings.videos_dir / submission.source_storage_key
        source.write_bytes(b"REPLACED-SOURCE")
    ctx.app.state.media_worker._run_job(job_id)
    with ctx.session_factory() as db:
        assert db.get(BackgroundJob, job_id).status == "failed"
        clip = db.query(Clip).one()
        assert clip.status == "failed" and clip.clip_path is None and clip.thumbnail_path is None


def test_submission_worker_rejects_same_size_replacement_with_restored_mtime(media_ctx, monkeypatch):
    ctx = media_ctx
    login = lambda username="demo", password="demo123": auth_headers(ctx.client, username, password)
    _headers, _reviewer, _project, _video, job_id = _queue_submission_media(ctx, login, monkeypatch)
    with ctx.session_factory() as db:
        submission = db.query(Submission).one()
        source = ctx.app.state.settings.videos_dir / submission.source_storage_key
        original = source.read_bytes()
        replacement = source.with_suffix(".replacement")
        replacement.write_bytes(bytes((byte + 1) % 256 for byte in original))
        os.utime(replacement, ns=(submission.source_mtime_ns, submission.source_mtime_ns))
        os.replace(replacement, source)
        os.utime(source, ns=(submission.source_mtime_ns, submission.source_mtime_ns))
        stat = source.stat()
        assert stat.st_size == submission.source_file_size
        assert stat.st_mtime_ns == submission.source_mtime_ns
    ctx.app.state.media_worker._run_job(job_id)
    with ctx.session_factory() as db:
        assert db.get(BackgroundJob, job_id).status == "failed"
        assert db.query(Clip).one().status == "failed"


def test_submission_worker_uses_verified_staging_after_source_path_replacement(media_ctx, monkeypatch):
    ctx = media_ctx
    login = lambda username="demo", password="demo123": auth_headers(ctx.client, username, password)
    _headers, _reviewer, _project, _video, job_id = _queue_submission_media(ctx, login, monkeypatch)
    with ctx.session_factory() as db:
        submission = db.query(Submission).one()
        source = ctx.app.state.settings.videos_dir / submission.source_storage_key
        original = source.read_bytes()
    def replace_after_stage(stage):
        if stage == "submission_source_staged":
            source.write_bytes(b"X" * len(original))
    monkeypatch.setattr(media_jobs_module, "_fault", replace_after_stage)
    ctx.app.state.media_worker._run_job(job_id)
    staged = Path(ctx.processor.clip_calls[0][0])
    assert staged != source and not staged.exists()
    assert all(Path(call[0]) == staged for call in ctx.processor.clip_calls)
    with ctx.session_factory() as db:
        assert db.get(BackgroundJob, job_id).status == "succeeded"


def test_submission_worker_staging_crash_cleans_and_retry_is_idempotent(media_ctx, monkeypatch):
    ctx = media_ctx
    login = lambda username="demo", password="demo123": auth_headers(ctx.client, username, password)
    _headers, _reviewer, _project, _video, job_id = _queue_submission_media(ctx, login, monkeypatch)
    monkeypatch.setattr(media_jobs_module, "_fault", lambda stage: (_ for _ in ()).throw(
        RuntimeError("crash after staging")) if stage == "submission_source_staged" else None)
    ctx.app.state.media_worker._run_job(job_id)
    assert list(ctx.app.state.settings.videos_dir.glob(".submission-media-job-*.staging")) == []
    monkeypatch.setattr(media_jobs_module, "_fault", lambda _stage: None)
    with ctx.session_factory() as db:
        job = db.get(BackgroundJob, job_id)
        job.status = "queued"
        db.commit()
    ctx.app.state.media_worker._run_job(job_id)
    with ctx.session_factory() as db:
        assert db.get(BackgroundJob, job_id).status == "succeeded"
        assert db.query(Clip).one().status == "ready"
    assert list(ctx.app.state.settings.videos_dir.glob(".submission-media-job-*.staging")) == []


def test_submission_media_startup_recovers_processing_and_duplicate_claim_is_idempotent(media_ctx, monkeypatch):
    ctx = media_ctx
    login = lambda username="demo", password="demo123": auth_headers(ctx.client, username, password)
    _headers, _reviewer, _project, _video, job_id = _queue_submission_media(ctx, login, monkeypatch)
    with ctx.session_factory() as db:
        job = db.get(BackgroundJob, job_id)
        job.status, job.attempts = "running", 1
        db.query(Clip).update({"status": "processing"})
        db.commit()
    ctx.app.state.media_worker._recover_interrupted()
    ctx.app.state.media_worker._run_job(job_id)
    calls = len(ctx.processor.clip_calls)
    ctx.app.state.media_worker._run_job(job_id)
    with ctx.session_factory() as db:
        assert db.get(BackgroundJob, job_id).status == "succeeded"
        assert db.query(Clip).one().status == "ready"
    assert len(ctx.processor.clip_calls) == calls == 1


def test_submission_media_partial_retry_and_missing_ready_rebuild(media_ctx, monkeypatch):
    ctx = media_ctx
    login = lambda username="demo", password="demo123": auth_headers(ctx.client, username, password)
    _headers, _reviewer, _project, _video, job_id = _queue_submission_media(
        ctx, login, monkeypatch, annotations=2)
    with ctx.session_factory() as db:
        ids = [row[0] for row in db.query(SubmissionAnnotation.id).order_by(SubmissionAnnotation.id)]
    ctx.processor.fail_clips.add(ids[1])
    ctx.app.state.media_worker._run_job(job_id)
    with ctx.session_factory() as db:
        assert [c.status for c in db.query(Clip).order_by(Clip.id)] == ["ready", "failed"]
        job = db.get(BackgroundJob, job_id); job.status = "queued"; db.commit()
    calls = len(ctx.processor.clip_calls)
    ctx.processor.fail_clips.clear()
    ctx.app.state.media_worker._run_job(job_id)
    assert len(ctx.processor.clip_calls) == calls + 1
    with ctx.session_factory() as db:
        clip = db.query(Clip).order_by(Clip.id).first()
        (ctx.app.state.settings.thumbnails_dir / clip.thumbnail_path).unlink()
        job = db.get(BackgroundJob, job_id); job.status = "queued"; db.commit()
    ctx.app.state.media_worker._run_job(job_id)
    with ctx.session_factory() as db:
        assert all(c.status == "ready" for c in db.query(Clip).all())


@pytest.mark.parametrize("payload", [
    {"submission_id": 1, "submission_annotation_ids": []},
    {"submission_id": 1, "submission_annotation_ids": [1, 1]},
])
def test_submission_media_invalid_or_duplicate_payload_hard_fails(media_ctx, monkeypatch, payload):
    ctx = media_ctx
    login = lambda username="demo", password="demo123": auth_headers(ctx.client, username, password)
    _headers, _reviewer, _project, _video, job_id = _queue_submission_media(ctx, login, monkeypatch)
    with ctx.session_factory() as db:
        submission = db.query(Submission).one()
        payload["submission_id"] = submission.id
        job = db.get(BackgroundJob, job_id); job.payload = payload; db.commit()
    ctx.app.state.media_worker._run_job(job_id)
    with ctx.session_factory() as db:
        assert db.get(BackgroundJob, job_id).status == "failed"
        assert db.query(Clip).one().status == "pending"


def test_submission_media_rename_before_commit_fault_cleans_and_retries(media_ctx, monkeypatch):
    ctx = media_ctx
    login = lambda username="demo", password="demo123": auth_headers(ctx.client, username, password)
    _headers, _reviewer, _project, _video, job_id = _queue_submission_media(ctx, login, monkeypatch)
    monkeypatch.setattr(media_jobs_module, "_fault", lambda stage: (_ for _ in ()).throw(
        RuntimeError("fault after rename")) if stage == "submission_files_renamed" else None)
    ctx.app.state.media_worker._run_job(job_id)
    assert list(ctx.app.state.settings.clips_dir.glob("*.mp4")) == []
    assert list(ctx.app.state.settings.thumbnails_dir.glob("*.jpg")) == []
    monkeypatch.setattr(media_jobs_module, "_fault", lambda _stage: None)
    with ctx.session_factory() as db:
        job = db.get(BackgroundJob, job_id); job.status = "queued"; db.commit()
    ctx.app.state.media_worker._run_job(job_id)
    with ctx.session_factory() as db:
        assert db.get(BackgroundJob, job_id).status == "succeeded"


def test_manual_generate_schedule_exception_returns_queued_not_500(media_ctx, monkeypatch):
    ctx = media_ctx
    login = lambda username="demo", password="demo123": auth_headers(ctx.client, username, password)
    headers, _reviewer, project, video, _job_id = _queue_submission_media(ctx, login, monkeypatch)
    monkeypatch.setattr(ctx.app.state.media_worker, "schedule", lambda _job_id: (_ for _ in ()).throw(
        RuntimeError("executor unavailable")))
    response = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/media/generate", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "queued"


def test_raw_baseline_delete_guard_allows_unreferenced_import(ctx, login_headers):
    _headers, _project, video, _annotation = _ready(ctx, login_headers)
    with ctx.session_factory() as db:
        imp = db.query(DetectionImport).filter_by(video_id=video["id"], active=True).one()
        expected = db.query(RawDetection).filter_by(detection_import_id=imp.id).count()
        assert expected > 0 and delete_raw_baseline(db, detection_import_id=imp.id) == expected
        db.commit()
        assert db.query(RawDetection).filter_by(detection_import_id=imp.id).count() == 0


def test_submit_validation_sql_count_is_bounded_for_120_annotations(ctx, login_headers):
    headers, project, video, annotation = _ready(ctx, login_headers)
    with ctx.session_factory() as db:
        source = db.get(Annotation, annotation["id"])
        for _ in range(119):
            db.add(Annotation(video_id=source.video_id, category_id=source.category_id,
                              annotator_id=source.annotator_id, start_time=source.start_time,
                              end_time=source.end_time, start_frame=source.start_frame,
                              end_frame=source.end_frame, confidence=source.confidence,
                              mouse_ids=[1], mouse_id_status="valid"))
        db.commit()
    count = 0
    def before_cursor(*_args):
        nonlocal count
        count += 1
    engine = ctx.session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", before_cursor)
    started = time.perf_counter()
    try:
        response = _submit(ctx, headers, project, video)
    finally:
        elapsed = time.perf_counter() - started
        event.remove(engine, "before_cursor_execute", before_cursor)
    assert response.status_code == 200, response.text
    assert count <= 35
    assert elapsed < 10.0, f"120-annotation submit took {elapsed:.3f}s"
    with ctx.session_factory() as db:
        assert db.query(SubmissionAnnotation).count() == 120


def test_new_clip_copy_survives_source_annotation_and_submitter_delete(media_ctx, monkeypatch):
    ctx = media_ctx
    login = lambda username="demo", password="demo123": auth_headers(ctx.client, username, password)
    headers, project, categories, video = _setup_video_with_import(ctx, login)
    submitter_id = ctx.create_user("temporary_submitter")
    ctx.add_member(project["id"], submitter_id, role="annotator")
    submitter_headers = login(username="temporary_submitter", password="pw123")
    category = next(c for c in categories if c["mouse_count_min"] == c["mouse_count_max"] == 1)
    annotation = _annotate_with_mouse(
        ctx, submitter_headers, project, video, category["id"], mouse_ids=[1])
    _add_reviewer(ctx, project["id"])
    assert _submit(ctx, submitter_headers, project, video).status_code == 200
    monkeypatch.setattr(ctx.app.state.media_worker, "schedule", lambda _job_id: None)
    assert _review(ctx, login(username="reviewer1", password="pw123"),
                   project, video, "approved").status_code == 200
    with ctx.session_factory() as db:
        job_id = db.query(BackgroundJob).one().id
        copy = db.query(SubmissionAnnotation).one()
        frozen_name = copy.category_name
        source = db.get(Annotation, annotation["id"])
        db.delete(source)
        submitter = db.get(User, submitter_id)
        db.delete(submitter)
        db.commit()
        assert db.query(SubmissionAnnotation).one().category_name == frozen_name
        assert db.query(SubmissionAnnotation).one().source_annotation_id is None
        assert db.query(Submission).one().submitted_by is None
    ctx.app.state.media_worker._run_job(job_id)
    response = ctx.client.get(f"/api/projects/{project['id']}/clips", headers=headers)
    assert response.status_code == 200
    assert response.json()["items"][0]["category_name"] == frozen_name
    assert response.json()["items"][0]["annotator_name"] is None


def test_clip_library_mixes_legacy_and_submission_authority_per_video(media_ctx, monkeypatch):
    ctx = media_ctx
    login = lambda username="demo", password="demo123": auth_headers(ctx.client, username, password)
    headers, _reviewer, project, _video, job_id = _queue_submission_media(ctx, login, monkeypatch)
    ctx.app.state.media_worker._run_job(job_id)
    with ctx.session_factory() as db:
        category_id = db.query(SubmissionAnnotation.category_id).scalar()
    legacy_video = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "legacy.mp4", "duration": 5.0, "fps": 25.0}, headers=headers,
    ).json()
    legacy_annotation = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{legacy_video['id']}/annotations",
        json={"category_id": category_id, "start_time": 0.0, "end_time": 0.04,
              "start_frame": 0, "end_frame": 1}, headers=headers,
    ).json()
    with ctx.session_factory() as db:
        video_row = db.get(__import__("app.models", fromlist=["Video"]).Video, legacy_video["id"])
        video_row.workflow_status = "approved"
        annotation_row = db.get(Annotation, legacy_annotation["id"])
        annotation_row.review_status = "approved"
        db.add(Clip(project_id=project["id"], annotation_id=annotation_row.id,
                    source_revision=video_row.media_revision, media_revision=video_row.media_revision,
                    status="ready", clip_path="legacy.mp4", thumbnail_path="legacy.jpg"))
        db.commit()
    response = ctx.client.get(f"/api/projects/{project['id']}/clips", headers=headers)
    assert response.status_code == 200
    assert {item["video_id"] for item in response.json()["items"]} == {
        _video["id"], legacy_video["id"]}
