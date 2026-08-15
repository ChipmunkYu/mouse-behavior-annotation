"""Phase 4: immutable Submission project ZIP and independent consumer contract."""
from __future__ import annotations

import json
import re
import zipfile

import pytest

from app.export_contract import FILES, safe_part, transform_detection
from app.media_jobs import reset_interrupted_job_clips
from app.export_jobs import export_dedupe_key
from app.models import (Annotation, BackgroundJob, BehaviorCategory, Clip, Project, Submission,
                        SubmissionAnnotation)
from tests.conftest import auth_headers
from tests.test_reviews import (_add_reviewer, _annotate_with_mouse, _review,
                                _setup_video_with_import, _submit)


def _approved(ctx, *, two_categories=False):
    login = lambda username="demo", password="demo123": auth_headers(ctx.client, username, password)
    headers, project, categories, video = _setup_video_with_import(ctx, login)
    suitable = [c for c in categories if c["mouse_count_min"] <= 1 <= c["mouse_count_max"]]
    first = _annotate_with_mouse(ctx, headers, project, video, suitable[0]["id"], mouse_ids=[1])
    annotations = [first]
    if two_categories:
        annotations.append(_annotate_with_mouse(
            ctx, headers, project, video, suitable[1]["id"], start_time=0.08, mouse_ids=[1]))
    _add_reviewer(ctx, project["id"])
    assert _submit(ctx, headers, project, video).status_code == 200
    assert _review(ctx, login("reviewer1", "pw123"), project, video, "approved").status_code == 200
    return headers, project, suitable, video, annotations


def _export(ctx, project, headers, category_ids=None):
    body = {} if category_ids is None else {"category_ids": category_ids}
    response = ctx.client.post(f"/api/projects/{project['id']}/export", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _archive(ctx, job):
    return ctx.app.state.settings.exports_dir / job["result_path"]


def _clip_dirs(archive):
    with zipfile.ZipFile(archive) as zf:
        files = [name for name in zf.namelist() if not name.endswith("/")]
    directories = sorted({name.rsplit("/", 1)[0] for name in files})
    return directories, files


def _remove_submission_clips(ctx):
    with ctx.session_factory() as db:
        clips = db.query(Clip).filter(Clip.submission_annotation_id.is_not(None)).all()
        ids = [clip.submission_annotation_id for clip in clips]
        for clip in clips:
            for stored, root in ((clip.clip_path, ctx.app.state.settings.clips_dir),
                                 (clip.thumbnail_path, ctx.app.state.settings.thumbnails_dir)):
                if stored:
                    (root / stored).unlink(missing_ok=True)
            db.delete(clip)
        db.commit()
    return ids


def test_export_schedule_exception_fails_job_releases_key_and_allows_retry(media_ctx, monkeypatch):
    ctx = media_ctx
    headers, project, _categories, _video, _annotations = _approved(ctx)
    worker = ctx.app.state.export_worker
    original_schedule = worker.schedule
    calls = 0

    def fail_once(job_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("executor unavailable")
        return original_schedule(job_id)

    monkeypatch.setattr(worker, "schedule", fail_once)

    response = ctx.client.post(
        f"/api/projects/{project['id']}/export", json={}, headers=headers)

    assert response.status_code == 503
    with ctx.session_factory() as db:
        failed = db.query(BackgroundJob).filter_by(job_type="export").one()
        failed_id = failed.id
        assert failed.status == "failed"
        assert failed.finished_at is not None
        assert "executor unavailable" in failed.error
        assert failed.dedupe_key != export_dedupe_key(project["id"])
        assert db.query(BackgroundJob).filter_by(
            dedupe_key=export_dedupe_key(project["id"])).count() == 0

    retry = ctx.client.post(
        f"/api/projects/{project['id']}/export", json={}, headers=headers)
    assert retry.status_code == 201, retry.text
    assert retry.json()["id"] != failed_id
    assert retry.json()["status"] == "succeeded"


def test_export_schedule_exception_preserves_job_claimed_by_worker(media_ctx, monkeypatch):
    ctx = media_ctx
    headers, project, _categories, _video, _annotations = _approved(ctx)
    active_key = export_dedupe_key(project["id"])

    def claim_then_fail(job_id):
        with ctx.session_factory() as db:
            claimed = db.query(BackgroundJob).filter_by(
                id=job_id, status="queued", dedupe_key=active_key).update(
                    {"status": "running", "attempts": BackgroundJob.attempts + 1},
                    synchronize_session=False,
                )
            assert claimed == 1
            db.commit()
        raise RuntimeError("schedule failed after worker claim")

    monkeypatch.setattr(ctx.app.state.export_worker, "schedule", claim_then_fail)

    response = ctx.client.post(
        f"/api/projects/{project['id']}/export", json={}, headers=headers)

    assert response.status_code == 503
    with ctx.session_factory() as db:
        running = db.query(BackgroundJob).filter_by(job_type="export").one()
        assert running.status == "running"
        assert running.error is None
        assert running.finished_at is None
        assert running.dedupe_key == active_key

    duplicate = ctx.client.post(
        f"/api/projects/{project['id']}/export", json={}, headers=headers)
    assert duplicate.status_code == 409


def test_single_category_is_flat_and_has_exact_four_files(media_ctx):
    ctx = media_ctx
    headers, project, categories, _video, _annotations = _approved(ctx)
    job = _export(ctx, project, headers)
    assert job["status"] == "succeeded", repr(job)
    assert job["payload"]["category_ids"] == [categories[0]["id"]]
    directories, files = _clip_dirs(_archive(ctx, job))
    assert len(directories) == 1 and "/" not in directories[0]
    assert {name.rsplit("/", 1)[1] for name in files} == FILES
    assert not any("annotations.json" in name or "manifest" in name or "corrected_tracks" in name
                   for name in files)


def test_multiple_categories_use_snapshot_category_directories(media_ctx):
    ctx = media_ctx
    headers, project, _categories, _video, _annotations = _approved(ctx, two_categories=True)
    job = _export(ctx, project, headers)
    directories, files = _clip_dirs(_archive(ctx, job))
    assert len(directories) == 2 and all(directory.count("/") == 1 for directory in directories)
    with zipfile.ZipFile(_archive(ctx, job)) as zf:
        annotations = [json.loads(zf.read(name)) for name in files if name.endswith("annotation.json")]
    assert {directory.split("/")[0] for directory in directories} == {
        item["behavior"] for item in annotations}


def test_payload_freezes_submission_annotation_category_and_snapshot_ids(media_ctx):
    ctx = media_ctx
    headers, project, categories, _video, _annotations = _approved(ctx)
    worker = ctx.app.state.export_worker; worker.synchronous = False
    with ctx.session_factory() as db:
        job = __import__("app.export_jobs", fromlist=["enqueue_export_job"]).enqueue_export_job(
            db, db.get(Project, project["id"]), [])
        payload = job.payload
        assert payload["category_ids"] == [categories[0]["id"]]
        assert len(payload["submission_ids"]) == len(payload["submission_annotation_ids"]) == 1
        assert payload["refs"][0]["snapshot_id"]


def test_superseded_frozen_submission_completes_without_current_draft_reads(media_ctx):
    ctx = media_ctx
    headers, project, _categories, _video, source_annotations = _approved(ctx)
    worker = ctx.app.state.export_worker; original = worker.synchronous; worker.synchronous = False
    try:
        with ctx.session_factory() as db:
            job = __import__("app.export_jobs", fromlist=["enqueue_export_job"]).enqueue_export_job(
                db, db.get(Project, project["id"]), [])
            job_id = job.id
            submission = db.query(Submission).filter_by(status="approved").one()
            submission.status = "superseded"
            db.get(Annotation, source_annotations[0]["id"]).start_time = 0.12
            db.get(BehaviorCategory, source_annotations[0]["category_id"]).name = "RENAMED"
            db.commit()
        worker._run_job(job_id)
        with ctx.session_factory() as db:
            job = db.get(BackgroundJob, job_id); assert job.status == "succeeded"
        with zipfile.ZipFile(_archive(ctx, {"result_path": job.result_path})) as zf:
            annotation = json.loads(zf.read(next(n for n in zf.namelist() if n.endswith("annotation.json"))))
        assert annotation["behavior"] != "RENAMED"
        assert annotation["time_range"]["start"] == 0.0
    finally:
        worker.synchronous = original


def test_independent_consumer_relative_frames_empty_frames_and_no_forbidden_fields(media_ctx):
    ctx = media_ctx
    headers, project, _categories, _video, _annotations = _approved(ctx)
    job = _export(ctx, project, headers)
    with zipfile.ZipFile(_archive(ctx, job)) as zf:
        directory = next(name.rsplit("/", 1)[0] for name in zf.namelist() if name.endswith("tracks.json"))
        tracks = json.loads(zf.read(f"{directory}/tracks.json"))
        annotation = json.loads(zf.read(f"{directory}/annotation.json"))
        metadata = json.loads(zf.read(f"{directory}/metadata.json"))
    assert [frame["frame"] for frame in tracks] == list(range(len(tracks)))
    assert all(frame["time"] == pytest.approx(frame["frame"] / metadata["clip"]["fps"])
               for frame in tracks)
    assert annotation["frame_range"] == {"start": 0, "end": len(tracks) - 1}
    serialized = json.dumps([annotation, tracks, metadata]).lower()
    assert not any(term in serialized for term in ("annotation_id", "submission_id", "reviewer",
                                                    "annotator", "storage_key", "sha256"))


@pytest.mark.parametrize("value,expected", [
    ("../攻击", "_攻击"), ("CON", "_CON"), ("  ", "untitled"),
    ("a/b\\c:*?", "a_b_c___"), ("Ａ", "A"),
])
def test_safe_directory_components(value, expected):
    assert safe_part(value) == expected


def test_windows_device_basename_with_extension_is_prefixed():
    assert safe_part("CON.txt") == "_CON.txt"
    assert safe_part("COM1.foo") == "_COM1.foo"


def test_safe_part_bounds_unicode_and_casefold_collisions():
    assert len(safe_part("鼠" * 200, limit=37)) == 37
    assert safe_part("ｅvil") == "e_vil"


def test_explicit_unrepresented_category_preserves_multi_category_layout(media_ctx):
    ctx = media_ctx
    headers, project, categories, _video, _annotations = _approved(ctx)
    job = _export(ctx, project, headers, [categories[0]["id"], categories[1]["id"]])
    assert job["payload"]["category_ids"] == [categories[0]["id"], categories[1]["id"]]
    directories, _files = _clip_dirs(_archive(ctx, job))
    assert len(directories) == 1 and directories[0].count("/") == 1


def test_no_eligible_rows_returns_400_without_job(media_ctx):
    ctx = media_ctx
    headers, project, categories, _video, _annotations = _approved(ctx)
    response = ctx.client.post(f"/api/projects/{project['id']}/export",
                               json={"category_ids": [categories[1]["id"]]}, headers=headers)
    assert response.status_code == 400
    with ctx.session_factory() as db:
        assert db.query(BackgroundJob).filter_by(job_type="export").count() == 0


def test_opaque_token_is_high_entropy_and_retry_stable(media_ctx):
    ctx = media_ctx
    headers, project, _categories, _video, _annotations = _approved(ctx)
    worker = ctx.app.state.export_worker; worker.synchronous = False
    with ctx.session_factory() as db:
        job = __import__("app.export_jobs", fromlist=["enqueue_export_job"]).enqueue_export_job(
            db, db.get(Project, project["id"]), [])
        token = job.payload["refs"][0]["opaque_token"]
        assert re.fullmatch(r"[0-9a-f]{32}", token)
        assert token != format(job.payload["refs"][0]["submission_annotation_id"], "032x")
        db.refresh(job)
        assert job.payload["refs"][0]["opaque_token"] == token


def test_crop_policy_excludes_outside_clamps_intersection_and_invalidates_keypoint():
    class Raw:
        detection_confidence = .9; class_id = 0
        box = {"x1": 5, "y1": 15, "x2": 25, "y2": 35}
        keypoints = [{"x_px": 5, "y_px": 15, "confidence": .8},
                     {"x_px": 20, "y_px": 30, "confidence": .7}]
    result = transform_detection(Raw(), 9, (10, 20, 20, 20), 20, 20)
    assert result["track_id"] == 9 and result["box"] == [0.0, 0.0, 15.0, 15.0]
    assert result["keypoints"][0] == [0.0, 0.0, 0.0]
    Raw.box = {"x1": 0, "y1": 0, "x2": 5, "y2": 5}
    assert transform_detection(Raw(), 9, (10, 20, 20, 20), 20, 20) is None


def test_probe_mismatch_and_render_failure_publish_no_final(media_ctx):
    ctx = media_ctx
    headers, project, _categories, _video, _annotations = _approved(ctx)
    original = ctx.processor.probe_clip
    ctx.processor.probe_clip = lambda path, expected=None: {
        **expected, "frame_count": 999, "duration": expected["frame_count"] / expected["fps"]}
    job = _export(ctx, project, headers)
    assert job["status"] == "failed" and job["result_path"] is None
    assert list(ctx.app.state.settings.exports_dir.glob("*.zip")) == []
    assert list(ctx.app.state.settings.exports_dir.glob(".export-*")) == []
    ctx.processor.probe_clip = original


def test_missing_submission_clip_is_generated_before_packaging(media_ctx):
    ctx = media_ctx
    headers, project, _categories, _video, _annotations = _approved(ctx)
    _remove_submission_clips(ctx)
    calls = len(ctx.processor.clip_calls)
    job = _export(ctx, project, headers)
    assert job["status"] == "succeeded"
    assert len(ctx.processor.clip_calls) == calls + 1
    with ctx.session_factory() as db:
        clip = db.query(Clip).filter(Clip.submission_annotation_id.is_not(None)).one()
        assert clip.status == "ready"


def test_current_export_startup_recovers_processing_clip_and_reruns(media_ctx):
    ctx = media_ctx
    headers, project, _categories, _video, _annotations = _approved(ctx)
    worker = ctx.app.state.export_worker
    original = worker.synchronous
    worker.synchronous = False
    try:
        with ctx.session_factory() as db:
            job = __import__("app.export_jobs", fromlist=["enqueue_export_job"]).enqueue_export_job(
                db, db.get(Project, project["id"]), [])
            job_id = job.id
            job.status, job.attempts = "running", 1
            clip = db.query(Clip).filter(Clip.submission_annotation_id.is_not(None)).one()
            clip.status = "processing"
            db.commit()
        worker._recover_interrupted()
        with ctx.session_factory() as db:
            assert db.get(BackgroundJob, job_id).status == "queued"
            assert db.query(Clip).filter(Clip.submission_annotation_id.is_not(None)).one().status == "pending"
        worker._run_job(job_id)
        with ctx.session_factory() as db:
            assert db.get(BackgroundJob, job_id).status == "succeeded"
            assert db.query(Clip).filter(Clip.submission_annotation_id.is_not(None)).one().status == "ready"
    finally:
        worker.synchronous = original


def test_exhausted_submission_media_recovery_allows_export_to_reclaim_clip(media_ctx):
    ctx = media_ctx
    headers, project, _categories, _video, _annotations = _approved(ctx)
    with ctx.session_factory() as db:
        submission = db.query(Submission).filter_by(status="approved").one()
        annotation = db.query(SubmissionAnnotation).filter_by(submission_id=submission.id).one()
        clip = db.query(Clip).filter_by(submission_annotation_id=annotation.id).one()
        media_job = db.query(BackgroundJob).filter(
            BackgroundJob.job_type == "media",
            BackgroundJob.payload["submission_id"].as_integer() == submission.id,
        ).one()
        for stored, root in ((clip.clip_path, ctx.app.state.settings.clips_dir),
                             (clip.thumbnail_path, ctx.app.state.settings.thumbnails_dir)):
            if stored:
                (root / stored).unlink(missing_ok=True)
        media_job.status = "running"
        media_job.attempts = ctx.app.state.settings.media_max_attempts
        clip.status = "processing"
        db.commit()
        exhausted_job_id, clip_id = media_job.id, clip.id

    ctx.app.state.media_worker._recover_interrupted()

    with ctx.session_factory() as db:
        exhausted = db.get(BackgroundJob, exhausted_job_id)
        assert exhausted.status == "failed"
        assert "retry limit" in exhausted.error
        assert db.get(Clip, clip_id).status == "pending"

    calls = len(ctx.processor.clip_calls)
    replacement = _export(ctx, project, headers)
    assert replacement["status"] == "succeeded"
    assert len(ctx.processor.clip_calls) == calls + 1
    with ctx.session_factory() as db:
        clip = db.get(Clip, clip_id)
        assert clip.status == "ready"
        assert (ctx.app.state.settings.clips_dir / clip.clip_path).is_file()


def test_legacy_export_payload_processing_clip_recovery_compatibility(media_ctx):
    ctx = media_ctx
    _headers, project, _categories, video, annotations = _approved(ctx)
    with ctx.session_factory() as db:
        source = db.get(Annotation, annotations[0]["id"])
        current_video = db.get(__import__("app.models", fromlist=["Video"]).Video, video["id"])
        clip = Clip(project_id=project["id"], annotation_id=source.id,
                    source_revision=current_video.media_revision, status="processing")
        job = BackgroundJob(project_id=project["id"], job_type="export", status="running",
                            payload={"annotation_ids": [source.id],
                                     "video_revisions": {str(video["id"]): current_video.annotation_revision}})
        db.add_all([clip, job]); db.commit()
        assert reset_interrupted_job_clips(db, job) == 1
        db.commit(); db.refresh(clip)
        assert clip.status == "pending"


def test_render_failure_cleans_staging_and_never_publishes_final(media_ctx):
    ctx = media_ctx
    headers, project, _categories, _video, _annotations = _approved(ctx)
    submission_annotation_id = _remove_submission_clips(ctx)[0]
    ctx.processor.fail_clips.add(submission_annotation_id)
    job = _export(ctx, project, headers)
    assert job["status"] == "failed" and job["result_path"] is None
    assert list(ctx.app.state.settings.exports_dir.iterdir()) == []


def test_colliding_directory_names_receive_stable_unique_suffix(media_ctx, monkeypatch):
    ctx = media_ctx
    headers, project, _categories, _video, _annotations = _approved(ctx, two_categories=True)
    monkeypatch.setattr("app.export_jobs.clip_directory_name", lambda annotation, submission: "same")
    job = _export(ctx, project, headers)
    directories, _files = _clip_dirs(_archive(ctx, job))
    assert len(directories) == 2
    assert len({directory.casefold() for directory in directories}) == 2


def test_colliding_category_directories_use_frozen_opaque_tokens(media_ctx, monkeypatch):
    ctx = media_ctx
    headers, project, categories, _video, _annotations = _approved(ctx, two_categories=True)
    with ctx.session_factory() as db:
        db.get(BehaviorCategory, categories[0]["id"]).name = "A"
        db.get(BehaviorCategory, categories[1]["id"]).name = "Ａ"
        db.commit()
    worker = ctx.app.state.export_worker; worker.synchronous = False
    with ctx.session_factory() as db:
        job = __import__("app.export_jobs", fromlist=["enqueue_export_job"]).enqueue_export_job(
            db, db.get(Project, project["id"]), [])
        directories = list(job.payload["category_directories"].values())
        tokens = job.payload["category_tokens"]
        assert len({name.casefold() for name in directories}) == 2
        assert all(re.fullmatch(r"[0-9a-f]{32}", token) for token in tokens.values())
        assert any(token[:12] in name for token in tokens.values() for name in directories)


def test_track_export_streams_query_without_materializing_all(media_ctx, monkeypatch):
    ctx = media_ctx
    headers, project, _categories, _video, _annotations = _approved(ctx)
    from app import export_jobs

    original = export_jobs.effective_detection_query
    observed = {"yield_per": None}

    class StreamingOnly:
        def __init__(self, query):
            self.query = query

        def order_by(self, *args):
            self.query = self.query.order_by(*args)
            return self

        def yield_per(self, count):
            observed["yield_per"] = count
            return self.query.yield_per(count)

        def all(self):  # pragma: no cover - this is the forbidden regression path
            raise AssertionError("track export must not materialize the detection stream with .all()")

    monkeypatch.setattr(export_jobs, "effective_detection_query",
                        lambda *args, **kwargs: StreamingOnly(original(*args, **kwargs)))
    job = _export(ctx, project, headers)
    assert job["status"] == "succeeded"
    assert observed["yield_per"] == 500


def test_active_dedupe_download_and_retry_history_compatibility(media_ctx):
    ctx = media_ctx
    headers, project, _categories, _video, _annotations = _approved(ctx)
    first = _export(ctx, project, headers); second = _export(ctx, project, headers)
    assert first["id"] != second["id"]
    response = ctx.client.get(f"/api/projects/{project['id']}/export/download", headers=headers)
    assert response.status_code == 200 and zipfile.is_zipfile(__import__("io").BytesIO(response.content))
