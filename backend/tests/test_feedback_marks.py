"""Focused acceptance checks for per-feedback '标记已修改' state."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import text

from app.behavior_review import record_feedback_mark
from app.models import FeedbackMark, User
from tests.test_behavior_review import _decide, _review, _setup
from tests.test_reviews import _submit


def _mark_url(project, video, state, snapshot_id):
    return (f"/api/projects/{project['id']}/videos/{video['id']}/submissions/"
            f"{state['submission_id']}/annotations/{snapshot_id}/feedback-mark")


def test_mark_authorization_persistence_and_idempotency(ctx, login_headers):
    headers, reviewer, project, video, _annotations, state = _setup(ctx, login_headers, count=1)
    state = _decide(ctx, reviewer, project, video, state, 0, "rejected", "请修正").json()
    assert _review(ctx, reviewer, project, video, "rejected").status_code == 200
    base = f"/api/projects/{project['id']}/videos/{video['id']}"
    snapshot_id = state["annotations"][0]["id"]
    url = _mark_url(project, video, state, snapshot_id)

    ctx.create_user("mark_outsider")
    outsider = login_headers(username="mark_outsider", password="pw123")
    assert ctx.client.put(url, headers=outsider).status_code == 403

    before = ctx.client.get(base + "/review-state", headers=reviewer).json()["feedback_items"][0]
    assert before["marked"] is False and before["marked_at"] is None

    member_id = ctx.create_user("mark_member")
    ctx.add_member(project["id"], member_id)
    member = login_headers(username="mark_member", password="pw123")
    marked = ctx.client.put(url, headers=member)
    assert marked.status_code == 200, marked.text
    item = marked.json()["feedback_items"][0]
    assert item["marked"] is True and item["marked_at"] is not None
    first_at = item["marked_at"]

    relogin = login_headers(username="mark_member", password="pw123")
    refreshed = ctx.client.get(base + "/review-state", headers=relogin).json()["feedback_items"][0]
    assert refreshed["marked"] is True and refreshed["marked_at"] == first_at

    repeated = ctx.client.put(url, headers=headers)
    assert repeated.status_code == 200
    assert repeated.json()["feedback_items"][0]["marked_at"] == first_at
    with ctx.session_factory() as db:
        assert db.query(FeedbackMark).count() == 1
        row = db.query(FeedbackMark).one()
        assert row.submission_annotation_id == snapshot_id


def test_mark_ordering_unmarked_first_then_by_timestamp(ctx, login_headers):
    headers, reviewer, project, video, _annotations, state = _setup(ctx, login_headers, count=2)
    state = _decide(ctx, reviewer, project, video, state, 0, "rejected", "a").json()
    state = _decide(ctx, reviewer, project, video, state, 1, "rejected", "b").json()
    assert _review(ctx, reviewer, project, video, "rejected").status_code == 200
    first_id = state["annotations"][0]["id"]
    second_id = state["annotations"][1]["id"]

    marked = ctx.client.put(_mark_url(project, video, state, first_id), headers=headers)
    assert marked.status_code == 200, marked.text
    items = marked.json()["feedback_items"]
    assert [item["submission_annotation_id"] for item in items] == [second_id, first_id]
    assert [item["marked"] for item in items] == [False, True]

    time.sleep(0.01)
    both = ctx.client.put(_mark_url(project, video, state, second_id), headers=headers)
    assert both.status_code == 200, both.text
    items = both.json()["feedback_items"]
    assert [item["marked"] for item in items] == [True, True]
    assert items[0]["submission_annotation_id"] == first_id
    assert items[0]["marked_at"] < items[1]["marked_at"]


def test_mark_rejects_non_rejected_and_missing_snapshot(ctx, login_headers):
    headers, reviewer, project, video, _annotations, state = _setup(ctx, login_headers, count=2)
    state = _decide(ctx, reviewer, project, video, state, 0, "approved").json()
    state = _decide(ctx, reviewer, project, video, state, 1, "rejected", "b").json()
    assert _review(ctx, reviewer, project, video, "rejected").status_code == 200
    approved_id = state["annotations"][0]["id"]

    assert ctx.client.put(_mark_url(project, video, state, approved_id), headers=headers).status_code == 409
    assert ctx.client.put(_mark_url(project, video, state, 999999), headers=headers).status_code == 404
    with ctx.session_factory() as db:
        assert db.query(FeedbackMark).count() == 0


def test_mark_accepts_owning_rejected_submission_after_resubmission(ctx, login_headers):
    """A resubmission exposes rejected_submission_id; marking still targets that owner."""
    headers, reviewer, project, video, annotations, state = _setup(ctx, login_headers, count=1)
    state = _decide(ctx, reviewer, project, video, state, 0, "rejected", "请修正").json()
    assert _review(ctx, reviewer, project, video, "rejected").status_code == 200
    rejected_submission_id = state["submission_id"]
    snapshot_id = state["annotations"][0]["id"]
    base = f"/api/projects/{project['id']}/videos/{video['id']}"

    # Change the material so the resubmission clears the rejection blocker.
    assert ctx.client.patch(base + f"/annotations/{annotations[0]['id']}",
                            json={"confidence": "uncertain"}, headers=headers).status_code == 200
    assert _submit(ctx, headers, project, video).status_code == 200

    resubmitted = ctx.client.get(base + "/review-state", headers=reviewer).json()
    assert resubmitted["submission_id"] != rejected_submission_id
    assert resubmitted["rejected_submission_id"] == rejected_submission_id
    assert resubmitted["feedback_items"][0]["submission_annotation_id"] == snapshot_id

    owning_url = (f"{base}/submissions/{resubmitted['rejected_submission_id']}/"
                  f"annotations/{snapshot_id}/feedback-mark")
    marked = ctx.client.put(owning_url, headers=headers)
    assert marked.status_code == 200, marked.text
    assert marked.json()["feedback_items"][0]["marked"] is True

    # The latest (resubmitted) attempt does not own this snapshot; marking stays scoped.
    wrong_url = (f"{base}/submissions/{resubmitted['submission_id']}/"
                 f"annotations/{snapshot_id}/feedback-mark")
    assert ctx.client.put(wrong_url, headers=headers).status_code == 404


def test_duplicate_marks_are_atomic_and_preserve_first_marker(ctx, login_headers):
    """Duplicate/concurrent marks must not race the unique index or overwrite the first."""
    headers, reviewer, project, video, _annotations, state = _setup(ctx, login_headers, count=1)
    state = _decide(ctx, reviewer, project, video, state, 0, "rejected", "请修正").json()
    assert _review(ctx, reviewer, project, video, "rejected").status_code == 200
    snapshot_id = state["annotations"][0]["id"]
    with ctx.session_factory() as db:
        first_user, second_user = [row[0] for row in
                                   db.query(User.id).order_by(User.id).limit(2).all()]

    barrier = Barrier(2)

    def mark(user_id):
        barrier.wait(timeout=30)
        with ctx.session_factory() as db:
            record_feedback_mark(db, snapshot_id, user_id)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(mark, first_user), pool.submit(mark, second_user)]:
            future.result(timeout=60)

    with ctx.session_factory() as db:
        winner = db.query(FeedbackMark).one()
        winner_id, winner_at = winner.marked_by, winner.marked_at

    # A later duplicate must not overwrite the first marker/time.
    with ctx.session_factory() as db:
        record_feedback_mark(db, snapshot_id,
                             first_user if winner_id != first_user else second_user)
        db.commit()
    with ctx.session_factory() as db:
        assert db.query(FeedbackMark).count() == 1
        row = db.query(FeedbackMark).one()
        assert row.marked_by == winner_id
        assert row.marked_at == winner_at


def test_mark_migration_defaults_leave_existing_feedback_unmarked(ctx, login_headers):
    from app.migration import downgrade_to, run_migrations

    headers, reviewer, project, video, _annotations, state = _setup(ctx, login_headers, count=1)
    state = _decide(ctx, reviewer, project, video, state, 0, "rejected", "旧反馈").json()
    assert _review(ctx, reviewer, project, video, "rejected").status_code == 200
    url = ctx.client.app.state.settings.resolved_database_url

    downgrade_to(url, "0017")  # drops feedback_marks, keeps rejected feedback data
    run_migrations(url)  # re-applies 0018 with empty table
    base = f"/api/projects/{project['id']}/videos/{video['id']}"
    items = ctx.client.get(base + "/review-state", headers=reviewer).json()["feedback_items"]
    assert items and items[0]["marked"] is False and items[0]["marked_at"] is None
    with ctx.session_factory() as db:
        assert db.query(FeedbackMark).count() == 0
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
