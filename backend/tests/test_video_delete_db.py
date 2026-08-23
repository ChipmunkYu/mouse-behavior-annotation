from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.models import (
    Annotation, BackgroundJob, Clip, CorrectedDetectionAssignment, CorrectedTrack,
    DetectionImport, DetectionSnapshot, DetectionSuppression,
    DetectionSnapshotState, DetectionStateOverride, DraftDetectionChange,
    DraftIdentityEdit, IdentityEdit, ProjectMembership, RawDetection, Review, Submission,
    SubmissionAnnotation, SuppressionDetection, User, Video, VideoImportBatch,
)
from app.video_delete_db import (
    VideoDeleteConflictError, VideoDeleteForbiddenError, VideoDeleteIntegrityError,
    delete_frozen_video, freeze_video_delete,
)
from app.video_delete_io import VideoDeleteIO, VideoDeleteIOError


def _settings(tmp_path):
    return Settings(env="test", data_dir=tmp_path,
                    database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}")


def _base(ctx):
    made = ctx.make_project_with_video()
    with ctx.session_factory() as db:
        actor = db.query(User).filter_by(username="demo").one()
        video = db.get(Video, made["video"]["id"])
        video.storage_path = "target.mp4"
        category_id = made["categories"][0]["id"]
        db.commit()
        return made["project"]["id"], video.id, actor.id, category_id


def _full_graph(db, project_id, video_id, actor_id, category_id, *,
                source_storage_key="target.mp4"):
    annotation = Annotation(
        video_id=video_id, annotator_id=actor_id, category_id=category_id,
        start_time=0, end_time=1, start_frame=0, end_frame=1,
        mouse_ids=[1], mouse_id_status="valid", participant_roles={},
    )
    detection_import = DetectionImport(
        video_id=video_id, revision=1, schema_version="1", tracks_path="tracks.jsonl",
        metadata_path="metadata.json", status="imported", active=True,
    )
    batch = VideoImportBatch(
        project_id=project_id, created_video_id=video_id, video_path="target.mp4",
        tracks_path="batch-tracks.jsonl", metadata_path="batch-metadata.json",
    )
    db.add_all([annotation, detection_import, batch]); db.flush()
    raw = RawDetection(
        detection_import_id=detection_import.id, frame_index=0,
        frame_detection_index=0, raw_track_id=1,
    )
    draft = DraftIdentityEdit(
        detection_import_id=detection_import.id, applied_edit_version=1,
        operation="split", params={}, operator_id=actor_id,
    )
    db.add_all([raw, draft]); db.flush()
    track = CorrectedTrack(
        detection_import_id=detection_import.id, display_track_id=1,
        effective_detection_count=1, created_identity_revision=1,
    )
    identity_edit = IdentityEdit(
        video_id=video_id, detection_import_id=detection_import.id, operation="split",
        base_identity_revision=0, result_identity_revision=1, operator_id=actor_id,
    )
    suppression = DetectionSuppression(
        video_id=video_id, detection_import_id=detection_import.id,
        base_identity_revision=0, result_identity_revision=1,
        scope="detection", operator_id=actor_id,
    )
    db.add_all([track, identity_edit, suppression]); db.flush()
    db.add_all([
        CorrectedDetectionAssignment(raw_detection_id=raw.id, corrected_track_id=track.id,
                                     identity_revision=1),
        SuppressionDetection(suppression_id=suppression.id, raw_detection_id=raw.id),
    ])
    db.add(DetectionStateOverride(
        raw_detection_id=raw.id, detection_import_id=detection_import.id,
        display_track_id=1, suppressed=False, updated_edit_version=1,
    ))
    db.add(DraftDetectionChange(
        edit_id=draft.id, raw_detection_id=raw.id, detection_import_id=detection_import.id,
        before_override_exists=False, before_display_track_id=None, before_suppressed=None,
        after_override_exists=True, after_display_track_id=1, after_suppressed=False,
    ))
    snapshot = DetectionSnapshot(
        detection_import_id=detection_import.id, source_edit_version=1,
        raw_detection_count=1, override_count=1, schema_version=1, fps=25,
        width=100, height=100, frame_count=10, keypoint_names=[], skeleton_edges=[],
    )
    db.add(snapshot); db.flush()
    db.add(DetectionSnapshotState(
        snapshot_id=snapshot.id, raw_detection_id=raw.id,
        detection_import_id=detection_import.id, display_track_id=1, suppressed=False,
    ))
    submission = Submission(
        video_id=video_id, detection_snapshot_id=snapshot.id, attempt_no=1,
        source_annotation_version=1, source_media_revision=1,
        source_video_filename="target.mp4", source_storage_key=source_storage_key,
        source_video_sha256="a" * 64, source_file_size=1, source_mtime_ns=1,
        source_device=1, source_inode=1, status="rejected", submitted_by=actor_id,
        submitted_at=datetime.utcnow(), decided_at=datetime.utcnow(),
    )
    db.add(submission); db.flush()
    frozen_annotation = SubmissionAnnotation(
        submission_id=submission.id, source_annotation_id=annotation.id,
        category_id=category_id, category_name="test", category_group="group",
        category_participant_mode="unordered", role_definitions_snapshot=[],
        participant_roles_snapshot={}, start_time=0, end_time=1,
        start_frame=0, end_frame=1, confidence="certain", mouse_ids=[1],
    )
    db.add(frozen_annotation); db.flush()
    db.add_all([
        Review(project_id=project_id, video_id=video_id, result="rejected",
               annotation_revision=1, submission_id=submission.id),
        Clip(project_id=project_id, annotation_id=annotation.id, source_revision=1,
             clip_path="live.mp4", thumbnail_path="live.jpg"),
        Clip(project_id=project_id, submission_annotation_id=frozen_annotation.id,
             media_revision=1, clip_path="frozen.mp4", thumbnail_path="frozen.jpg"),
        BackgroundJob(
            project_id=project_id, job_type="media", status="failed",
            payload={"submission_id": submission.id,
                     "submission_annotation_ids": [frozen_annotation.id]},
        ),
    ])
    db.commit()
    return snapshot.id, frozen_annotation.id


def _freeze(db, settings, project_id, video_id, actor_id):
    return freeze_video_delete(db, project_id=project_id, video_id=video_id,
                               actor_user_id=actor_id, settings=settings)


def _other_graph(db, project_id, actor_id, category_id, *, suffix="other",
                 merged_into_id=None, source_annotation_id=None):
    video = Video(project_id=project_id, filename=f"{suffix}.mp4",
                  storage_path=f"{suffix}.mp4", workflow_status="draft")
    db.add(video); db.flush()
    annotation = Annotation(
        video_id=video.id, annotator_id=actor_id, category_id=category_id,
        start_time=0, end_time=1, start_frame=0, end_frame=1,
        mouse_ids=[1], mouse_id_status="valid", participant_roles={},
    )
    detection_import = DetectionImport(
        video_id=video.id, revision=1, schema_version="1", status="imported", active=True,
    )
    db.add_all([annotation, detection_import]); db.flush()
    raw = RawDetection(detection_import_id=detection_import.id, frame_index=0,
                       frame_detection_index=0, raw_track_id=1)
    track = CorrectedTrack(detection_import_id=detection_import.id, display_track_id=1,
                           merged_into_id=merged_into_id)
    db.add_all([raw, track]); db.flush()
    snapshot = DetectionSnapshot(
        detection_import_id=detection_import.id, source_edit_version=0,
        raw_detection_count=0, override_count=0, schema_version=1, fps=25,
        width=100, height=100, frame_count=1, keypoint_names=[], skeleton_edges=[],
    )
    db.add(snapshot); db.flush()
    submission = Submission(
        video_id=video.id, detection_snapshot_id=snapshot.id, attempt_no=1,
        source_annotation_version=1, source_media_revision=1,
        source_video_filename=f"{suffix}.mp4", source_storage_key=f"{suffix}.mp4",
        source_video_sha256="b" * 64, source_file_size=1, source_mtime_ns=1,
        source_device=1, source_inode=2, status="rejected", submitted_by=actor_id,
        submitted_at=datetime.utcnow(), decided_at=datetime.utcnow(),
    )
    db.add(submission); db.flush()
    frozen_annotation = SubmissionAnnotation(
        submission_id=submission.id,
        source_annotation_id=(annotation.id if source_annotation_id is None
                              else source_annotation_id),
        category_id=category_id, category_name="test", category_group="group",
        category_participant_mode="unordered", role_definitions_snapshot=[],
        participant_roles_snapshot={}, start_time=0, end_time=1,
        start_frame=0, end_frame=1, confidence="certain", mouse_ids=[1],
    )
    db.add(frozen_annotation); db.flush()
    return {
        "video": video, "annotation": annotation, "import": detection_import,
        "raw": raw, "raw_id": raw.id, "track": track, "track_id": track.id,
        "submission": submission, "submission_annotation": frozen_annotation,
    }


def _final(db, session_factory, frozen, settings, hook=None):
    db.rollback()
    delete_frozen_video(session_factory, frozen, settings=settings, fault_hook=hook)
    db.expire_all()


def _export_payload(project_id, pairs):
    category_ids = [1]
    refs = [{
        "submission_id": submission_id, "submission_annotation_id": annotation_id,
        "snapshot_id": 1, "source_media_revision": 1, "source_sha256": "a" * 64,
        "source_file_size": 1, "source_mtime_ns": 1, "source_device": 1,
        "source_inode": 1, "raw_digest": "b" * 64, "state_digest": "c" * 64,
        "metadata_digest": "d" * 64, "opaque_token": "e" * 24,
    } for submission_id, annotation_id in pairs]
    return {
        "contract_version": 1, "project_id": project_id, "category_ids": category_ids,
        "category_directories": {"1": "category"}, "category_tokens": {"1": "f" * 32},
        "submission_ids": sorted({item[0] for item in pairs}),
        "submission_annotation_ids": [item[1] for item in pairs], "refs": refs,
    }


def test_permissions_and_workflow_state_are_typed_and_read_only(ctx, tmp_path):
    project_id, video_id, actor_id, _ = _base(ctx)
    outsider = ctx.create_user("outsider")
    with ctx.session_factory() as db:
        with pytest.raises(VideoDeleteForbiddenError):
            _freeze(db, _settings(tmp_path), project_id, video_id, outsider)
        video = db.get(Video, video_id); video.workflow_status = "approved"; db.commit()
        with pytest.raises(VideoDeleteConflictError, match="draft or rejected"):
            _freeze(db, _settings(tmp_path), project_id, video_id, actor_id)
        assert db.get(Video, video_id).workflow_status == "approved"


@pytest.mark.parametrize("status,payload", [
    ("running", lambda p, v: {"video_id": v, "project_id": p, "revision": 1}),
    ("mystery", lambda p, v: {"video_id": v, "project_id": p, "revision": 1}),
])
def test_active_or_unknown_job_blocks_without_side_effects(ctx, tmp_path, status, payload):
    project_id, video_id, actor_id, _ = _base(ctx)
    with ctx.session_factory() as db:
        job = BackgroundJob(project_id=project_id, job_type="media", status=status,
                            payload=payload(project_id, video_id))
        db.add(job); db.commit(); job_id = job.id
        with pytest.raises(VideoDeleteConflictError):
            _freeze(db, _settings(tmp_path), project_id, video_id, actor_id)
        assert db.get(Video, video_id) is not None
        assert db.get(BackgroundJob, job_id).status == status


def test_full_fk_graph_terminal_job_and_foreign_key_check(ctx, tmp_path):
    project_id, video_id, actor_id, category_id = _base(ctx)
    settings = _settings(tmp_path)
    with ctx.session_factory() as db:
        _full_graph(db, project_id, video_id, actor_id, category_id)
        frozen = _freeze(db, settings, project_id, video_id, actor_id)
        assert frozen.terminal_job_ids
        assert {item.root_kind for item in frozen.paths} == {
            "videos", "detection_imports", "clips", "thumbnails"
        }
        _final(db, ctx.session_factory, frozen, settings)
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
        for table in (
            "corrected_tracks", "corrected_detection_assignments", "identity_edits",
            "detection_suppressions", "suppression_detections",
        ):
            assert db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
        assert db.get(Video, video_id) is None
        assert not db.query(BackgroundJob).filter(
            BackgroundJob.id.in_(frozen.terminal_job_ids)).count()


def test_cross_video_snapshot_and_shared_path_fail_closed(ctx, tmp_path):
    project_id, video_id, actor_id, category_id = _base(ctx)
    settings = _settings(tmp_path)
    with ctx.session_factory() as db:
        snapshot_id, _ = _full_graph(db, project_id, video_id, actor_id, category_id)
        other = Video(project_id=project_id, filename="other.mp4", storage_path="other.mp4",
                      workflow_status="draft")
        db.add(other); db.flush()
        db.add(Submission(
            video_id=other.id, detection_snapshot_id=snapshot_id, attempt_no=1,
            source_annotation_version=1, source_media_revision=1,
            source_video_filename="other.mp4", source_storage_key="other.mp4",
            source_video_sha256="b" * 64, source_file_size=1, source_mtime_ns=1,
            source_device=1, source_inode=2, status="rejected", submitted_by=actor_id,
            submitted_at=datetime.utcnow(), decided_at=datetime.utcnow(),
        )); db.commit()
        with pytest.raises(VideoDeleteConflictError, match="snapshot"):
            _freeze(db, settings, project_id, video_id, actor_id)
        db.query(Submission).filter(Submission.video_id == other.id).delete()
        other.storage_path = "target.mp4"; db.commit()
        with pytest.raises(VideoDeleteConflictError, match="shared"):
            _freeze(db, settings, project_id, video_id, actor_id)


def test_historical_submission_source_key_is_frozen_and_shared_checked(ctx, tmp_path):
    project_id, video_id, actor_id, category_id = _base(ctx)
    settings = _settings(tmp_path)
    with ctx.session_factory() as db:
        _full_graph(db, project_id, video_id, actor_id, category_id,
                    source_storage_key="history/original.mp4")
        frozen = _freeze(db, settings, project_id, video_id, actor_id)
        assert ("videos", "history/original.mp4") in {
            (item.root_kind, item.relative_key) for item in frozen.paths
        }
        other = _other_graph(db, project_id, actor_id, category_id, suffix="shared-history")
        other["video"].storage_path = "history/original.mp4"
        db.commit()
        with pytest.raises(VideoDeleteConflictError, match="shared"):
            _freeze(db, settings, project_id, video_id, actor_id)


@pytest.mark.parametrize("result_kind,expected_root", [
    ("relative-clip", "clips"), ("absolute-clip", "clips"),
    ("absolute-thumbnail", "thumbnails"),
])
def test_legacy_media_result_resolves_exactly_one_candidate_root(
        ctx, tmp_path, result_kind, expected_root):
    project_id, video_id, actor_id, category_id = _base(ctx)
    settings = _settings(tmp_path)
    with ctx.session_factory() as db:
        _, frozen_annotation_id = _full_graph(
            db, project_id, video_id, actor_id, category_id)
        values = {
            "relative-clip": "frozen.mp4",
            "absolute-clip": str(settings.clips_dir / "frozen.mp4"),
            "absolute-thumbnail": str(settings.thumbnails_dir / "frozen.jpg"),
        }
        db.add(BackgroundJob(
            project_id=project_id, job_type="media", status="failed",
            payload={"submission_id": db.query(Submission).filter_by(video_id=video_id).one().id,
                     "submission_annotation_ids": [frozen_annotation_id]},
            result_path=values[result_kind],
        ))
        db.commit()
        frozen = _freeze(db, settings, project_id, video_id, actor_id)
        assert any(item.root_kind == expected_root for item in frozen.paths)


@pytest.mark.parametrize("candidate_count", [0, 2])
def test_legacy_media_result_fails_closed_without_exactly_one_root(
        ctx, tmp_path, candidate_count):
    project_id, video_id, actor_id, category_id = _base(ctx)
    settings = _settings(tmp_path)
    with ctx.session_factory() as db:
        _, frozen_annotation_id = _full_graph(
            db, project_id, video_id, actor_id, category_id)
        result_path = "missing.bin"
        if candidate_count == 2:
            clip = db.query(Clip).filter(Clip.annotation_id.is_not(None)).one()
            clip.clip_path = clip.thumbnail_path = result_path
        submission_id = db.query(Submission).filter_by(video_id=video_id).one().id
        db.add(BackgroundJob(
            project_id=project_id, job_type="media", status="failed",
            payload={"submission_id": submission_id,
                     "submission_annotation_ids": [frozen_annotation_id]},
            result_path=result_path,
        ))
        db.commit()
        with pytest.raises(VideoDeleteIntegrityError, match="unambiguous"):
            _freeze(db, settings, project_id, video_id, actor_id)


def test_terminal_contract_export_with_multiple_videos_deletes_whole_job_only(ctx, tmp_path):
    project_id, video_id, actor_id, category_id = _base(ctx)
    settings = _settings(tmp_path)
    with ctx.session_factory() as db:
        _, target_annotation_id = _full_graph(db, project_id, video_id, actor_id, category_id)
        target_submission_id = db.query(Submission).filter_by(video_id=video_id).one().id
        other = _other_graph(db, project_id, actor_id, category_id, suffix="export-other")
        pairs = ((target_submission_id, target_annotation_id),
                 (other["submission"].id, other["submission_annotation"].id))
        target_job = BackgroundJob(
            project_id=project_id, job_type="export", status="succeeded",
            payload=_export_payload(project_id, pairs), result_path="mixed.zip",
        )
        unrelated_job = BackgroundJob(
            project_id=project_id, job_type="export", status="succeeded",
            payload=_export_payload(project_id, ((other["submission"].id,
                                                  other["submission_annotation"].id),)),
            result_path="other.zip",
        )
        db.add_all([target_job, unrelated_job]); db.commit()
        target_job_id, unrelated_job_id, other_video_id = (
            target_job.id, unrelated_job.id, other["video"].id)
        frozen = _freeze(db, settings, project_id, video_id, actor_id)
        assert target_job_id in frozen.terminal_job_ids
        assert unrelated_job_id not in frozen.terminal_job_ids
        assert ("exports", "mixed.zip") in {
            (item.root_kind, item.relative_key) for item in frozen.paths
        }
        _final(db, ctx.session_factory, frozen, settings)
        assert db.get(BackgroundJob, target_job_id) is None
        assert db.get(BackgroundJob, unrelated_job_id) is not None
        assert db.get(Video, other_video_id) is not None


def test_failed_terminal_jobs_quarantine_all_provable_staging_and_temp_artifacts(ctx, tmp_path):
    project_id, video_id, actor_id, category_id = _base(ctx)
    settings = _settings(tmp_path)
    for root in (settings.videos_dir, settings.exports_dir, settings.clips_dir,
                 settings.thumbnails_dir, settings.detection_imports_dir):
        root.mkdir(parents=True, exist_ok=True)
    with ctx.session_factory() as db:
        _, frozen_annotation_id = _full_graph(db, project_id, video_id, actor_id, category_id)
        submission_id = db.query(Submission).filter_by(video_id=video_id).one().id
        media_job = db.query(BackgroundJob).filter_by(job_type="media").one()
        export_job = BackgroundJob(
            project_id=project_id, job_type="export", status="failed",
            payload=_export_payload(project_id, ((submission_id, frozen_annotation_id),)),
        )
        db.add(export_job); db.commit()
        media_job_id, export_job_id = media_job.id, export_job.id

        staging = settings.exports_dir / f".export-{export_job.id}.staging"
        (staging / "category").mkdir(parents=True)
        (staging / "category" / "tracks.json").write_text("[]", encoding="utf-8")
        export_temp = settings.exports_dir / f".export-{export_job.id}.tmp.zip"
        export_temp.write_bytes(b"zip")
        legacy_staging = settings.exports_dir / f".export_{project_id}_{export_job.id}.staging"
        legacy_staging.mkdir()
        source_staging = settings.videos_dir / (
            f".submission-media-job-{media_job.id}-{'a' * 32}.staging"
        )
        source_staging.write_bytes(b"source")
        clip = db.query(Clip).filter(Clip.submission_annotation_id == frozen_annotation_id).one()
        clip_temp = settings.clips_dir / (
            f".clip_{frozen_annotation_id}_revsub{submission_id}.{'b' * 32}.mp4.part"
        )
        thumb_temp = settings.thumbnails_dir / (
            f".clip_{frozen_annotation_id}_revsub{submission_id}.{'b' * 32}.jpg.part"
        )
        clip_temp.write_bytes(b"clip"); thumb_temp.write_bytes(b"thumb")

        frozen = _freeze(db, settings, project_id, video_id, actor_id)
        paths = {(item.root_kind, item.relative_key, item.path_kind) for item in frozen.paths}
        assert ("exports", staging.name, "directory") in paths
        assert ("exports", legacy_staging.name, "directory") in paths
        assert ("exports", export_temp.name, "file") in paths
        assert ("videos", source_staging.name, "file") in paths
        assert ("clips", clip_temp.name, "file") in paths
        assert ("thumbnails", thumb_temp.name, "file") in paths

        io = VideoDeleteIO(settings)
        manifest = io.quarantine(io.prepare(video_id, frozen.paths, operation_id="terminal-residue"))
        _final(db, ctx.session_factory, frozen, settings)
        if io._descriptor_directory_operations_supported():
            io.purge(manifest)
        else:
            with pytest.raises(VideoDeleteIOError, match="directory-descriptor-operations-unavailable"):
                io.purge(manifest)
            assert (io.quarantine_dir / "terminal-residue").exists()
        assert not any(path.exists() for path in (
            staging, legacy_staging, export_temp, source_staging, clip_temp, thumb_temp,
        ))
        assert db.get(BackgroundJob, media_job_id) is None
        assert db.get(BackgroundJob, export_job_id) is None


def test_suspicious_or_wrong_type_terminal_artifact_fails_closed(ctx, tmp_path):
    project_id, video_id, actor_id, category_id = _base(ctx)
    settings = _settings(tmp_path)
    settings.exports_dir.mkdir(parents=True, exist_ok=True)
    with ctx.session_factory() as db:
        _, annotation_id = _full_graph(db, project_id, video_id, actor_id, category_id)
        submission_id = db.query(Submission).filter_by(video_id=video_id).one().id
        job = BackgroundJob(project_id=project_id, job_type="export", status="failed",
                            payload=_export_payload(project_id, ((submission_id, annotation_id),)))
        db.add(job); db.commit(); job_id = job.id
        suspicious = settings.exports_dir / f".export-{job.id}.staging.unowned"
        suspicious.mkdir()
        with pytest.raises(VideoDeleteConflictError, match="suspicious"):
            _freeze(db, settings, project_id, video_id, actor_id)
        suspicious.rmdir()
        wrong_type = settings.exports_dir / f".export-{job.id}.tmp.zip"
        wrong_type.mkdir()
        frozen = _freeze(db, settings, project_id, video_id, actor_id)
        with pytest.raises(Exception, match="path-kind-mismatch"):
            VideoDeleteIO(settings).prepare(video_id, frozen.paths, operation_id="wrong-type")
        assert db.get(Video, video_id) is not None and wrong_type.is_dir()


def test_database_failure_restores_terminal_staging_directory(ctx, tmp_path):
    project_id, video_id, actor_id, category_id = _base(ctx)
    settings = _settings(tmp_path)
    settings.exports_dir.mkdir(parents=True, exist_ok=True)
    with ctx.session_factory() as db:
        _, annotation_id = _full_graph(db, project_id, video_id, actor_id, category_id)
        submission_id = db.query(Submission).filter_by(video_id=video_id).one().id
        job = BackgroundJob(project_id=project_id, job_type="export", status="failed",
                            payload=_export_payload(project_id, ((submission_id, annotation_id),)))
        db.add(job); db.commit(); job_id = job.id
        staging = settings.exports_dir / f".export-{job.id}.staging"
        staging.mkdir(); (staging / "owned").write_bytes(b"owned")
        frozen = _freeze(db, settings, project_id, video_id, actor_id)
        io = VideoDeleteIO(settings)
        manifest = io.quarantine(io.prepare(video_id, frozen.paths, operation_id="db-failure-dir"))

        def fail(_stage):
            raise RuntimeError("injected database failure")

        with pytest.raises(RuntimeError, match="injected database failure"):
            _final(db, ctx.session_factory, frozen, settings, fail)
        if io._descriptor_directory_operations_supported():
            io.restore(manifest)
            assert (staging / "owned").read_bytes() == b"owned"
        else:
            with pytest.raises(VideoDeleteIOError, match="directory-descriptor-operations-unavailable"):
                io.restore(manifest)
            quarantine = io.quarantine_dir / "db-failure-dir" / "files"
            assert any((item / "owned").exists() for item in quarantine.iterdir() if item.is_dir())
        assert db.get(Video, video_id) is not None and db.get(BackgroundJob, job_id) is not None


def test_freeze_drift_is_rejected(ctx, tmp_path):
    project_id, video_id, actor_id, _ = _base(ctx)
    settings = _settings(tmp_path)
    with ctx.session_factory() as db:
        frozen = _freeze(db, settings, project_id, video_id, actor_id)
        db.rollback()
        db.get(Video, video_id).storage_path = "changed.mp4"; db.commit()
        with pytest.raises(VideoDeleteConflictError, match="changed"):
            delete_frozen_video(ctx.session_factory, frozen, settings=settings)
        assert db.get(Video, video_id) is not None


def test_final_delete_rejects_caller_session_even_with_manual_begin(ctx, tmp_path):
    project_id, video_id, actor_id, _ = _base(ctx)
    settings = _settings(tmp_path)
    with ctx.session_factory() as db:
        frozen = _freeze(db, settings, project_id, video_id, actor_id)
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        with pytest.raises(VideoDeleteIntegrityError, match="factory or engine"):
            delete_frozen_video(db, frozen, settings=settings)
        db.rollback()
        assert db.get(Video, video_id) is not None


@pytest.mark.parametrize("edge", [
    "identity-revert", "suppression-revert", "track-merge",
    "submission-annotation", "suppression-detection-raw",
    "suppression-detection-suppression",
])
def test_external_incoming_edges_block_freeze_without_side_effects(ctx, tmp_path, edge):
    project_id, video_id, actor_id, category_id = _base(ctx)
    settings = _settings(tmp_path)
    with ctx.session_factory() as db:
        _full_graph(db, project_id, video_id, actor_id, category_id)
        target_edit = db.query(IdentityEdit).filter_by(video_id=video_id).one()
        target_suppression = db.query(DetectionSuppression).filter_by(video_id=video_id).one()
        target_track = (db.query(CorrectedTrack).join(DetectionImport)
                        .filter(DetectionImport.video_id == video_id).one())
        target_raw = (db.query(RawDetection).join(DetectionImport)
                      .filter(DetectionImport.video_id == video_id).one())
        target_annotation = db.query(Annotation).filter_by(video_id=video_id).one()
        other = _other_graph(
            db, project_id, actor_id, category_id, suffix=edge,
            merged_into_id=(target_track.id if edge == "track-merge" else None),
            source_annotation_id=(target_annotation.id
                                  if edge == "submission-annotation" else None),
        )
        if edge == "identity-revert":
            db.add(IdentityEdit(
                video_id=other["video"].id, detection_import_id=other["import"].id,
                operation="revert", base_identity_revision=1, result_identity_revision=2,
                reverted_edit_id=target_edit.id,
            ))
        elif edge == "suppression-revert":
            db.add(DetectionSuppression(
                video_id=other["video"].id, detection_import_id=other["import"].id,
                base_identity_revision=1, result_identity_revision=2,
                scope="detection", reverted_suppression_id=target_suppression.id,
            ))
        elif edge in {"track-merge", "submission-annotation"}:
            pass
        elif edge == "suppression-detection-raw":
            external_suppression = DetectionSuppression(
                video_id=other["video"].id, detection_import_id=other["import"].id,
                base_identity_revision=0, result_identity_revision=1, scope="detection",
            )
            db.add(external_suppression); db.flush()
            db.add(SuppressionDetection(suppression_id=external_suppression.id,
                                        raw_detection_id=target_raw.id))
        else:
            db.add(SuppressionDetection(suppression_id=target_suppression.id,
                                        raw_detection_id=other["raw_id"]))
        db.commit()
        before = db.execute(text("SELECT count(*) FROM videos")).scalar_one()
        with pytest.raises(VideoDeleteConflictError):
            _freeze(db, settings, project_id, video_id, actor_id)
        assert db.execute(text("SELECT count(*) FROM videos")).scalar_one() == before
        assert db.get(Video, other["video"].id) is not None


def test_triggers_protect_before_and_after_success(ctx, tmp_path):
    project_id, video_id, actor_id, category_id = _base(ctx)
    settings = _settings(tmp_path)
    with ctx.session_factory() as db:
        _, submission_annotation_id = _full_graph(db, project_id, video_id, actor_id, category_id)
        other = _other_graph(db, project_id, actor_id, category_id, suffix="protected-success")
        protected_id = other["submission_annotation"].id
        db.commit()
        with pytest.raises(IntegrityError, match="submission annotation immutable"):
            db.query(SubmissionAnnotation).filter_by(id=submission_annotation_id).delete()
        db.rollback()
        frozen = _freeze(db, settings, project_id, video_id, actor_id)
        _final(db, ctx.session_factory, frozen, settings)
        names = {row[0] for row in db.execute(text(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ))}
        assert {"trg_annotation_delete", "trg_live_annotation_delete"} <= names
        with pytest.raises(IntegrityError, match="submission annotation immutable"):
            db.query(SubmissionAnnotation).filter_by(id=protected_id).delete()
        db.rollback()
        assert db.get(SubmissionAnnotation, protected_id) is not None


def test_trigger_drop_fault_rolls_back_schema_and_rows(ctx, tmp_path):
    project_id, video_id, actor_id, category_id = _base(ctx)
    settings = _settings(tmp_path)
    with ctx.session_factory() as db:
        protected = _other_graph(db, project_id, actor_id, category_id,
                                 suffix="protected-fault")["submission_annotation"]
        db.commit(); protected_id = protected.id
        frozen = _freeze(db, settings, project_id, video_id, actor_id)
        def fail(stage):
            assert stage == "triggers_dropped"
            raise RuntimeError("injected")
        with pytest.raises(RuntimeError, match="injected"):
            _final(db, ctx.session_factory, frozen, settings, fail)
        assert db.get(Video, video_id) is not None
        names = {row[0] for row in db.execute(text(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ))}
        assert {"trg_annotation_delete", "trg_live_annotation_delete"} <= names
        with pytest.raises(IntegrityError, match="submission annotation immutable"):
            db.query(SubmissionAnnotation).filter_by(id=protected_id).delete()
        db.rollback()
        assert db.get(SubmissionAnnotation, protected_id) is not None
