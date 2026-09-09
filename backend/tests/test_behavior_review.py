"""Focused behavior-level review acceptance checks."""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import (Annotation, BehaviorReviewDecision, BehaviorReviewReopen, DetectionImport,
                        Review, Submission, SubmissionAnnotation, Video)
from tests.test_reviews import (_add_reviewer, _annotate_with_mouse, _review,
                                _setup_video_with_import, _submit)


def _setup(ctx, login_headers, *, count=2):
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)
    category = next(c for c in categories if c["mouse_count_min"] == c["mouse_count_max"] == 1)
    annotations = [_annotate_with_mouse(ctx, headers, project, video, category["id"],
                                        start_time=i * .04, mouse_ids=[1 + i]) for i in range(count)]
    _add_reviewer(ctx, project["id"])
    reviewer = login_headers(username="reviewer1", password="pw123")
    assert _submit(ctx, headers, project, video).status_code == 200
    state = ctx.client.get(f"/api/projects/{project['id']}/videos/{video['id']}/review-state",
                           headers=reviewer).json()
    return headers, reviewer, project, video, annotations, state


def _decide(ctx, reviewer, project, video, state, index, status, feedback=None):
    row = state["annotations"][index]
    return ctx.client.put(
        f"/api/projects/{project['id']}/videos/{video['id']}/submissions/{state['submission_id']}/annotations/{row['id']}/decision",
        json={"status": status, "feedback": feedback,
              "expected_decision_revision": state["decision_revision"]}, headers=reviewer)


def test_final_approval_blocks_create_replace_and_purge_until_reopen(ctx, login_headers):
    from tests.test_reviews import _make_metadata_json, _make_tracks_jsonl
    from app.video_delete_db import VideoDeleteConflictError, freeze_video_delete

    headers, reviewer, project, video, annotations, state = _setup(ctx, login_headers, count=1)
    assert _review(ctx, reviewer, project, video, "approved").status_code == 200
    base = f"/api/projects/{project['id']}/videos/{video['id']}"
    payload = {"category_id": annotations[0]["category_id"], "start_frame": 0,
               "end_frame": 1, "mouse_ids": [1]}
    with ctx.session_factory() as db:
        v = db.get(Video, video["id"])
        before = (v.annotation_revision, v.media_revision, v.detection_import_revision,
                  v.approved_at, v.approved_by)
    created = ctx.client.post(base + "/annotations", json=payload, headers=headers)
    assert created.status_code == 409, created.text
    replaced = ctx.client.post(base + "/detection-imports?confirm=true", headers=headers, files={
        "tracks_file": ("tracks.jsonl", _make_tracks_jsonl().encode()),
        "metadata_file": ("metadata.json", _make_metadata_json().encode())})
    assert replaced.status_code == 409, replaced.text
    assert ctx.client.patch(base + f"/annotations/{annotations[0]['id']}",
                            json={"confidence": "uncertain"}, headers=headers).status_code == 409
    assert ctx.client.delete(base + f"/annotations/{annotations[0]['id']}", headers=headers).status_code == 409
    with ctx.session_factory() as db:
        v = db.get(Video, video["id"])
        assert v.workflow_status == db.get(Submission, state["submission_id"]).status == "approved"
        assert (v.annotation_revision, v.media_revision, v.detection_import_revision,
                v.approved_at, v.approved_by) == before
        assert db.query(Annotation).count() == db.query(DetectionImport).count() == 1
        actor_id = db.get(Annotation, annotations[0]["id"]).annotator_id
        for projection in ("approved", "draft", "rejected"):
            v.workflow_status = projection
            db.commit()
            with pytest.raises(VideoDeleteConflictError):
                freeze_video_delete(db, project_id=project["id"], video_id=video["id"],
                                    actor_user_id=actor_id, settings=ctx.client.app.state.settings)
            db.rollback()
        v.workflow_status = "approved"
        db.commit()
    reopened = ctx.client.post(base + f"/submissions/{state['submission_id']}/reopen",
                               headers=reviewer, json={"reason": "regression"})
    assert reopened.status_code == 200, reopened.text
    assert ctx.client.post(base + "/annotations", json=payload, headers=headers).status_code == 201
    replaced = ctx.client.post(base + "/detection-imports?confirm=true", headers=headers, files={
        "tracks_file": ("tracks.jsonl", _make_tracks_jsonl().encode()),
        "metadata_file": ("metadata.json", _make_metadata_json().encode())})
    assert replaced.status_code == 200, replaced.text


@pytest.mark.parametrize("resolve", [None, "approval", "revoked_approval", "revoked_rejection"])
def test_rejection_lineage_survives_withdraw_and_restore(ctx, login_headers, resolve):
    headers, reviewer, project, video, annotations, state = _setup(ctx, login_headers, count=1)
    state = _decide(ctx, reviewer, project, video, state, 0, "rejected", "fix A").json()
    assert _review(ctx, reviewer, project, video, "rejected").status_code == 200
    if resolve == "revoked_rejection":
        assert _decide(ctx, reviewer, project, video, state, 0, "pending").status_code == 200
    base = f"/api/projects/{project['id']}/videos/{video['id']}"
    annotation_url = base + f"/annotations/{annotations[0]['id']}"
    assert ctx.client.patch(annotation_url, json={"confidence": "uncertain"}, headers=headers).status_code == 200
    assert _submit(ctx, headers, project, video).status_code == 200
    if resolve in {"approval", "revoked_approval"}:
        state = ctx.client.get(base + "/review-state", headers=reviewer).json()
        state = _decide(ctx, reviewer, project, video, state, 0, "approved").json()
        if resolve == "revoked_approval":
            assert _decide(ctx, reviewer, project, video, state, 0, "pending").status_code == 200
    assert ctx.client.post(base + "/withdraw", headers=headers).status_code == 200
    if resolve == "approval":
        assert _decide(ctx, reviewer, project, video, state, 0, "pending").status_code == 200
    assert ctx.client.patch(annotation_url, json={"confidence": "certain"}, headers=headers).status_code == 200
    result = _submit(ctx, headers, project, video)
    assert result.status_code == (409 if resolve is None else 200), result.text
    if resolve is None:
        assert result.json()["detail"]["items"][0]["comparison"] == "reverted"
        assert result.json()["detail"]["items"][0]["submission_annotation_id"] == state["annotations"][0]["id"]
        with ctx.session_factory() as db:
            assert db.query(Submission).count() == 2
        assert ctx.client.patch(annotation_url, json={"confidence": "uncertain"}, headers=headers).status_code == 200
        assert _submit(ctx, headers, project, video).status_code == 200


@pytest.mark.parametrize("result", ["approved", "rejected"])
def test_final_review_requires_current_context_without_state_changes(ctx, login_headers, result):
    headers, reviewer, project, video, _annotations, old = _setup(ctx, login_headers, count=1)
    state = _decide(ctx, reviewer, project, video, old, 0, "approved").json()
    base = f"/api/projects/{project['id']}/videos/{video['id']}"
    valid = {"result": result, "expected_submission_id": state["submission_id"],
             "expected_decision_revision": state["decision_revision"]}
    for field in ("expected_submission_id", "expected_decision_revision"):
        for missing in (True, False):
            payload = dict(valid)
            if missing:
                del payload[field]
            else:
                payload[field] = None
            assert ctx.client.post(base + "/review", json=payload, headers=reviewer).status_code == 422
    stale = ctx.client.post(base + "/review", headers=reviewer,
                            json={**valid, "expected_decision_revision": old["decision_revision"]})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_decision_revision"
    assert ctx.client.get(base + "/review-state", headers=reviewer).json() == state
    with ctx.session_factory() as db:
        assert db.query(Review).count() == 0
        assert db.get(Video, video["id"]).workflow_status == "submitted"
        assert db.get(Submission, state["submission_id"]).status == "submitted"
    assert ctx.client.post(base + "/withdraw", headers=headers).status_code == 200
    assert _submit(ctx, headers, project, video).status_code == 200
    before = ctx.client.get(base + "/review-state", headers=reviewer).json()
    stale = ctx.client.post(base + "/review", json=valid, headers=reviewer)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_submission"
    assert ctx.client.get(base + "/review-state", headers=reviewer).json() == before
    with ctx.session_factory() as db:
        assert db.query(Review).count() == 0
        assert db.get(Video, video["id"]).workflow_status == "submitted"
        assert db.get(Submission, before["submission_id"]).status == "submitted"


def test_decisions_persist_immediately_require_feedback_and_revision(ctx, login_headers):
    _h, reviewer, project, video, _a, state = _setup(ctx, login_headers)
    bad = _decide(ctx, reviewer, project, video, state, 0, "rejected")
    assert bad.status_code == 422
    approved = _decide(ctx, reviewer, project, video, state, 0, "approved")
    assert approved.status_code == 200
    state = approved.json()
    rejected = _decide(ctx, reviewer, project, video, state, 1, "rejected", "时间范围不准")
    assert rejected.status_code == 200
    assert rejected.json()["counts"] == {"pending": 0, "approved": 1, "rejected": 1}
    assert _decide(ctx, reviewer, project, video, state, 1, "approved").status_code == 409
    ctx.create_user("review_outsider")
    outsider = login_headers(username="review_outsider", password="pw123")
    assert _decide(ctx, outsider, project, video, rejected.json(), 0, "pending").status_code == 403
    with ctx.session_factory() as db:
        assert db.query(BehaviorReviewDecision).count() == 2


def test_final_approve_requires_all_but_reject_allows_pending(ctx, login_headers):
    _h, reviewer, project, video, _a, state = _setup(ctx, login_headers)
    denied = ctx.client.post(f"/api/projects/{project['id']}/videos/{video['id']}/review",
        json={"result": "approved", "expected_submission_id": state["submission_id"],
              "expected_decision_revision": state["decision_revision"]}, headers=reviewer)
    assert denied.status_code == 409
    rejected = ctx.client.post(f"/api/projects/{project['id']}/videos/{video['id']}/review",
        json={"result": "rejected", "expected_submission_id": state["submission_id"],
              "expected_decision_revision": state["decision_revision"]}, headers=reviewer)
    assert rejected.status_code == 200


def test_lock_rejection_comparison_carry_and_reopen(ctx, login_headers):
    headers, reviewer, project, video, annotations, state = _setup(ctx, login_headers)
    state = _decide(ctx, reviewer, project, video, state, 0, "approved").json()
    state = _decide(ctx, reviewer, project, video, state, 1, "rejected", "请修改").json()
    assert _review(ctx, reviewer, project, video, "rejected").status_code == 200
    base = f"/api/projects/{project['id']}/videos/{video['id']}/annotations"
    assert ctx.client.patch(f"{base}/{annotations[0]['id']}", json={"confidence": "uncertain"}, headers=headers).status_code == 409
    assert ctx.client.delete(f"{base}/{annotations[0]['id']}", headers=headers).status_code == 409
    blocked = _submit(ctx, headers, project, video)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "rejected_annotations_not_addressed"
    assert ctx.client.patch(f"{base}/{annotations[1]['id']}", json={"confidence": "uncertain"}, headers=headers).status_code == 200
    assert _submit(ctx, headers, project, video).status_code == 200
    carried = ctx.client.get(f"/api/projects/{project['id']}/videos/{video['id']}/review-state", headers=reviewer).json()
    assert carried["counts"] == {"pending": 1, "approved": 1, "rejected": 0}
    assert carried["annotations"][0]["decision"]["origin"] == "carried"
    state = _decide(ctx, reviewer, project, video, carried, 1, "approved").json()
    final = ctx.client.post(f"/api/projects/{project['id']}/videos/{video['id']}/review",
        json={"result": "approved", "expected_submission_id": state["submission_id"],
              "expected_decision_revision": state["decision_revision"]}, headers=reviewer)
    assert final.status_code == 200
    base = f"/api/projects/{project['id']}/videos/{video['id']}"
    suppressed = ctx.client.post(base + "/detection-suppressions", headers=headers, json={
        "scope": "corrected_track", "track_id": 1,
        "base_identity_revision": 0, "base_detection_import_revision": 1})
    assert suppressed.status_code == 200, suppressed.text
    preserved = ctx.client.get(base + "/review-state", headers=reviewer).json()
    assert preserved["submission_status"] == "approved"
    with ctx.session_factory() as db:
        assert db.get(Video, video["id"]).workflow_status == "approved"
    assert preserved["counts"] == {"pending": 0, "approved": 2, "rejected": 0}
    assert preserved["locked_annotation_ids"] == [item["id"] for item in annotations]
    reopened = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/submissions/{state['submission_id']}/reopen",
        json={"reason": "复核"}, headers=reviewer)
    assert reopened.status_code == 200
    assert reopened.json()["submission_status"] == "superseded"
    assert reopened.json()["locked_annotation_ids"] == []
    with ctx.session_factory() as db:
        assert db.query(BehaviorReviewReopen).count() == 1


def test_deleted_rejection_is_addressed_and_history_survives(ctx, login_headers):
    headers, reviewer, project, video, annotations, state = _setup(ctx, login_headers, count=2)
    state = _decide(ctx, reviewer, project, video, state, 0, "rejected", "删除它").json()
    assert _review(ctx, reviewer, project, video, "rejected").status_code == 200
    assert ctx.client.delete(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations/{annotations[0]['id']}",
        headers=headers).status_code == 204
    visible = ctx.client.get(f"/api/projects/{project['id']}/videos/{video['id']}/review-state",
                             headers=reviewer).json()
    assert visible["feedback_items"][0]["comparison"] == "deleted"
    assert _submit(ctx, headers, project, video).status_code == 200
    with ctx.session_factory() as db:
        snapshot = db.query(SubmissionAnnotation).filter_by(
            source_annotation_key=annotations[0]["id"]).one()
        assert snapshot.source_annotation_id is None
        assert snapshot.source_annotation_key == annotations[0]["id"]


def test_track_projection_does_not_address_rejection_but_edit_revert_is_reported(ctx, login_headers):
    headers, reviewer, project, video, annotations, state = _setup(ctx, login_headers, count=1)
    state = _decide(ctx, reviewer, project, video, state, 0, "rejected", "请修正").json()
    assert _review(ctx, reviewer, project, video, "rejected").status_code == 200
    with ctx.session_factory() as db:
        annotation = db.get(Annotation, annotations[0]["id"])
        original_digest, original_revision = annotation.material_digest, annotation.material_revision
        annotation.mouse_ids = [2]  # track correction projection: material evidence deliberately unchanged
        annotation.identity_revision += 1
        db.commit()
        assert (annotation.material_digest, annotation.material_revision) == (original_digest, original_revision)
    blocked = _submit(ctx, headers, project, video)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["items"][0]["comparison"] == "unchanged"

    with ctx.session_factory() as db:
        db.get(Annotation, annotations[0]["id"]).mouse_ids = [1]
        db.commit()

    base = f"/api/projects/{project['id']}/videos/{video['id']}/annotations/{annotations[0]['id']}"
    assert ctx.client.patch(base, json={"confidence": "uncertain"}, headers=headers).status_code == 200
    assert ctx.client.patch(base, json={"confidence": "certain"}, headers=headers).status_code == 200
    state = ctx.client.get(f"/api/projects/{project['id']}/videos/{video['id']}/review-state",
                           headers=reviewer).json()
    assert state["feedback_items"][0]["comparison"] == "reverted"
    blocked = _submit(ctx, headers, project, video)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["items"][0]["comparison"] == "reverted"
    from app.behavior_review import decision_comparison
    with ctx.session_factory() as db:
        snapshot = db.query(SubmissionAnnotation).one()
        live = db.get(Annotation, annotations[0]["id"])
        baseline_id = snapshot.submission.detection_snapshot.detection_import_id
        assert decision_comparison(snapshot, live, current_detection_import_id=baseline_id + 1) == "modified"


def test_deleted_id_reuse_does_not_resurrect_feedback_or_mutate_replacement(ctx, login_headers):
    headers, reviewer, project, video, annotations, state = _setup(ctx, login_headers, count=1)
    state = _decide(ctx, reviewer, project, video, state, 0, "rejected", "remove").json()
    assert _review(ctx, reviewer, project, video, "rejected").status_code == 200
    base = f"/api/projects/{project['id']}/videos/{video['id']}"
    assert ctx.client.delete(f"{base}/annotations/{annotations[0]['id']}", headers=headers).status_code == 204
    replacement = _annotate_with_mouse(ctx, headers, project, video, annotations[0]["category_id"], mouse_ids=[1])
    # SQLite can reuse the last rowid; the nulled FK, not the historical key, proves deletion.
    assert replacement["id"] == annotations[0]["id"]
    visible = ctx.client.get(base + "/review-state", headers=reviewer).json()
    assert visible["feedback_items"][0]["comparison"] == "deleted"
    assert visible["feedback_items"][0]["current"] is None
    assert _submit(ctx, headers, project, video).status_code == 200
    visible = ctx.client.get(base + "/review-state", headers=reviewer).json()
    assert visible["counts"] == {"pending": 1, "approved": 0, "rejected": 0}


def test_real_track_split_undo_preserves_decisions_and_does_not_address_feedback(ctx, login_headers):
    headers, reviewer, project, video, annotations, state = _setup(ctx, login_headers)
    state = _decide(ctx, reviewer, project, video, state, 0, "approved").json()
    state = _decide(ctx, reviewer, project, video, state, 1, "rejected", "fix behavior").json()
    assert _review(ctx, reviewer, project, video, "rejected").status_code == 200
    base = f"/api/projects/{project['id']}/videos/{video['id']}"
    split = ctx.client.post(base + "/identity-edits", headers=headers, json={
        "operation": "split", "track_ids": [1], "frame": 1,
        "base_identity_revision": 0, "base_detection_import_revision": 1})
    assert split.status_code == 200, split.text
    visible = ctx.client.get(base + "/review-state", headers=reviewer).json()
    assert visible["locked_annotation_ids"] == [annotations[0]["id"]]
    assert visible["counts"] == {"pending": 0, "approved": 1, "rejected": 1}
    assert visible["feedback_items"][0]["comparison"] == "unchanged"
    undo = ctx.client.post(base + f"/identity-edits/{split.json()['edit_id']}/revert", headers=headers,
                          json={"base_identity_revision": 1, "base_detection_import_revision": 1})
    assert undo.status_code == 200, undo.text
    blocked = _submit(ctx, headers, project, video)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == "rejected_annotations_not_addressed"
    assert ctx.client.patch(base + f"/annotations/{annotations[1]['id']}", headers=headers,
                            json={"confidence": "uncertain"}).status_code == 200
    assert _submit(ctx, headers, project, video).status_code == 200
    visible = ctx.client.get(base + "/review-state", headers=reviewer).json()
    assert visible["annotations"][0]["decision"]["origin"] == "carried"
    assert visible["locked_annotation_ids"] == [annotations[0]["id"]]


def test_revoke_permissions_stale_context_and_append_only_audit(ctx, login_headers):
    headers, reviewer, project, video, annotations, state = _setup(ctx, login_headers, count=1)
    state = _decide(ctx, reviewer, project, video, state, 0, "approved").json()
    assert _review(ctx, reviewer, project, video, "rejected").status_code == 200
    member_id = ctx.create_user("ordinary_member")
    ctx.add_member(project["id"], member_id)
    member = login_headers(username="ordinary_member", password="pw123")
    assert _decide(ctx, member, project, video, state, 0, "pending").status_code == 403
    revoked = _decide(ctx, reviewer, project, video, state, 0, "pending")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["locked_annotation_ids"] == []
    base = f"/api/projects/{project['id']}/videos/{video['id']}"
    stale = ctx.client.post(base + "/submit", headers=headers, json={
        "expected_submission_id": state["submission_id"],
        "expected_decision_revision": state["decision_revision"]})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_decision_revision"
    assert _submit(ctx, headers, project, video).status_code == 200
    assert _decide(ctx, reviewer, project, video, revoked.json(), 0, "pending").status_code == 409
    with ctx.session_factory() as db:
        for sql in ("UPDATE behavior_review_decisions SET status='pending' WHERE status='approved'",
                    "DELETE FROM behavior_review_decisions"):
            with pytest.raises(IntegrityError, match="append-only"):
                db.execute(text(sql))
            db.rollback()
        assert db.query(BehaviorReviewDecision).count() == 2


@pytest.mark.parametrize("result", ["approved", "rejected"])
def test_0016_legacy_review_migration_is_safe_and_idempotent(ctx, login_headers, result):
    from app.migration import downgrade_to, run_migrations

    headers, reviewer, project, video, annotations, _state = _setup(ctx, login_headers, count=1)
    assert _review(ctx, reviewer, project, video, result).status_code == 200
    url = ctx.client.app.state.settings.resolved_database_url
    downgrade_to(url, "0016")
    run_migrations(url)
    run_migrations(url)
    base = f"/api/projects/{project['id']}/videos/{video['id']}"
    state = ctx.client.get(base + "/review-state", headers=reviewer).json()
    assert state["feedback_items"] == []
    with ctx.session_factory() as db:
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
        assert db.query(BehaviorReviewDecision).count() == (1 if result == "approved" else 0)
    if result == "approved":
        assert state["locked_annotation_ids"] == [annotations[0]["id"]]
        assert state["annotations"][0]["decision"]["origin"] == "legacy"
        assert _decide(ctx, reviewer, project, video, state, 0, "pending").status_code == 409
        reopened = ctx.client.post(base + f"/submissions/{state['submission_id']}/reopen",
                                   headers=reviewer, json={"reason": "legacy check"})
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["locked_annotation_ids"] == []
    assert _submit(ctx, headers, project, video).status_code == 200


def test_0016_superseded_approval_does_not_deadlock_latest_rejection(ctx, login_headers):
    """Real 0016 history: approved s1, ordinary edit, then rejected s2."""
    from app.migration import downgrade_to, run_migrations
    from app.video_delete_db import freeze_video_delete

    headers, reviewer, project, video, annotations, state = _setup(ctx, login_headers, count=1)
    assert _review(ctx, reviewer, project, video, "approved").status_code == 200
    url = ctx.client.app.state.settings.resolved_database_url
    downgrade_to(url, "0016")

    with ctx.session_factory() as legacy_db:
        legacy_db.execute(text(
            "UPDATE videos SET workflow_status='draft',annotation_revision=annotation_revision+1,"
            "submitted_at=NULL,approved_at=NULL,approved_by=NULL WHERE id=:video"
        ), {"video": video["id"]})
        legacy_db.execute(text(
            "UPDATE annotations SET confidence='uncertain',review_status='pending',reviewer_id=NULL,"
            "updated_at=CURRENT_TIMESTAMP WHERE id=:annotation"
        ), {"annotation": annotations[0]["id"]})
        legacy_db.execute(text(
            "INSERT INTO submissions(video_id,detection_snapshot_id,attempt_no,source_annotation_version,"
            "source_media_revision,source_video_filename,source_storage_key,source_video_sha256,"
            "source_file_size,source_mtime_ns,source_device,source_inode,status,submitted_by,submitted_at,"
            "decided_at,legacy_backfill) SELECT video_id,detection_snapshot_id,2,source_annotation_version+1,"
            "source_media_revision,source_video_filename,source_storage_key,source_video_sha256,"
            "source_file_size,source_mtime_ns,source_device,source_inode,'rejected',submitted_by,"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,legacy_backfill FROM submissions WHERE id=:s1"
        ), {"s1": state["submission_id"]})
        s2 = legacy_db.execute(text("SELECT id FROM submissions WHERE video_id=:video AND attempt_no=2"),
                               {"video": video["id"]}).scalar_one()
        legacy_db.execute(text(
            "INSERT INTO submission_annotations(submission_id,source_annotation_id,category_id,category_name,"
            "category_group,category_participant_mode,role_definitions_snapshot,participant_roles_snapshot,"
            "start_time,end_time,start_frame,end_frame,confidence,crop_region,mouse_ids) SELECT :s2,"
            "source_annotation_id,category_id,category_name,category_group,category_participant_mode,"
            "role_definitions_snapshot,participant_roles_snapshot,start_time,end_time,start_frame,end_frame,"
            "'uncertain',crop_region,mouse_ids FROM submission_annotations WHERE submission_id=:s1"
        ), {"s1": state["submission_id"], "s2": s2})
        legacy_db.execute(text(
            "INSERT INTO reviews(project_id,video_id,reviewer_id,result,comment,annotation_revision,"
            "detection_import_revision,identity_revision,submission_id,created_at) SELECT project_id,video_id,"
            "reviewer_id,'rejected','legacy latest rejection',annotation_revision+1,detection_import_revision,"
            "identity_revision,:s2,CURRENT_TIMESTAMP FROM reviews WHERE submission_id=:s1"
        ), {"s1": state["submission_id"], "s2": s2})
        legacy_db.commit()

    run_migrations(url)
    with ctx.session_factory() as db:
        attempts = db.query(Submission).filter_by(video_id=video["id"]).order_by(Submission.attempt_no).all()
        assert [row.status for row in attempts] == ["superseded", "rejected"]
        assert db.query(BehaviorReviewDecision).count() == 1
        frozen = freeze_video_delete(db, project_id=project["id"], video_id=video["id"],
                                     actor_user_id=db.get(Annotation, annotations[0]["id"]).annotator_id,
                                     settings=ctx.client.app.state.settings)
        assert state["submission_id"] in frozen.ids("submissions")

    annotation_url = (f"/api/projects/{project['id']}/videos/{video['id']}/annotations/"
                      f"{annotations[0]['id']}")
    assert ctx.client.patch(annotation_url, json={"confidence": "certain"}, headers=headers).status_code == 200
    assert _submit(ctx, headers, project, video).status_code == 200
    current = ctx.client.get(
        f"/api/projects/{project['id']}/videos/{video['id']}/review-state", headers=reviewer).json()
    current = _decide(ctx, reviewer, project, video, current, 0, "approved").json()
    assert _review(ctx, reviewer, project, video, "approved").status_code == 200
    assert ctx.client.patch(annotation_url, json={"confidence": "uncertain"}, headers=headers).status_code == 409
    reopened = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/submissions/{current['submission_id']}/reopen",
        headers=reviewer, json={"reason": "verify latest approval"})
    assert reopened.status_code == 200, reopened.text
    assert ctx.client.patch(annotation_url, json={"confidence": "uncertain"}, headers=headers).status_code == 200
