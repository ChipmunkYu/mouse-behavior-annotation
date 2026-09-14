"""行为统计表聚合端点（GET /api/projects/{id}/behavior-stats）验收。"""
from __future__ import annotations

import pytest

from app.models import ProjectMembership, Submission, SubmissionAnnotation
from tests.test_reviews import (_add_reviewer, _annotate_with_mouse, _review,
                                _setup_video_with_import, _submit)


def _single_mouse_categories(categories):
    return [c for c in categories if c["mouse_count_min"] == c["mouse_count_max"] == 1]


def _setup(ctx, login_headers):
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)
    singles = _single_mouse_categories(categories)
    _add_reviewer(ctx, project["id"])
    reviewer = login_headers(username="reviewer1", password="pw123")
    return headers, reviewer, project, video, categories, singles


def _decide(ctx, reviewer, project, video, state, index, status, feedback=None):
    row = state["annotations"][index]
    return ctx.client.put(
        f"/api/projects/{project['id']}/videos/{video['id']}/submissions/"
        f"{state['submission_id']}/annotations/{row['id']}/decision",
        json={"status": status, "feedback": feedback,
              "expected_decision_revision": state["decision_revision"]}, headers=reviewer)


def _review_state(ctx, project, video, headers):
    return ctx.client.get(
        f"/api/projects/{project['id']}/videos/{video['id']}/review-state", headers=headers).json()


def _stats(ctx, project, headers):
    return ctx.client.get(f"/api/projects/{project['id']}/behavior-stats", headers=headers)


def _by_id(body):
    return {item["category_id"]: item for item in body["items"]}


def test_counts_split_pending_default_and_zero_categories(ctx, login_headers):
    headers, reviewer, project, video, categories, singles = _setup(ctx, login_headers)
    cat = singles[0]
    for index in range(3):
        _annotate_with_mouse(ctx, headers, project, video, cat["id"],
                             start_time=index * .04, mouse_ids=[1])
    assert _submit(ctx, headers, project, video).status_code == 200
    state = _review_state(ctx, project, video, reviewer)
    state = _decide(ctx, reviewer, project, video, state, 0, "approved").json()
    _decide(ctx, reviewer, project, video, state, 1, "rejected", "fix")
    # index 2 has no decision row -> must count as pending

    body = _stats(ctx, project, headers).json()
    # every project category is returned, not just the ones with behaviors
    assert len(body["items"]) == len(categories)
    assert _by_id(body)[cat["id"]] == {
        "category_id": cat["id"], "category_name": cat["name"], "category_group": cat["group"],
        "approved": 1, "pending": 1, "rejected": 1, "possible_total": 2,
    }
    zero = _by_id(body)[singles[1]["id"]]
    assert zero == {
        "category_id": singles[1]["id"], "category_name": singles[1]["name"],
        "category_group": singles[1]["group"],
        "approved": 0, "pending": 0, "rejected": 0, "possible_total": 0,
    }
    # ascending possible_total, ties broken by canonical sort_order
    assert [item["possible_total"] for item in body["items"]] == sorted(
        item["possible_total"] for item in body["items"])
    assert [item["category_id"] for item in body["items"] if item["possible_total"] == 0] == [
        category["id"] for category in categories if category["id"] != cat["id"]
    ]
    assert body["items"][-1]["category_id"] == cat["id"]


def test_withdrawn_latest_submission_contributes_nothing(ctx, login_headers):
    headers, _reviewer, project, video, _categories, singles = _setup(ctx, login_headers)
    _annotate_with_mouse(ctx, headers, project, video, singles[0]["id"], mouse_ids=[1])
    assert _submit(ctx, headers, project, video).status_code == 200
    assert ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/withdraw", headers=headers
    ).status_code == 200

    body = _stats(ctx, project, headers).json()
    assert all(item["approved"] == item["pending"] == item["rejected"] == 0
               for item in body["items"])


def test_only_latest_attempt_is_counted(ctx, login_headers):
    headers, reviewer, project, video, _categories, singles = _setup(ctx, login_headers)
    cat = singles[0]
    annotation = _annotate_with_mouse(ctx, headers, project, video, cat["id"], mouse_ids=[1])
    assert _submit(ctx, headers, project, video).status_code == 200
    assert _review(ctx, reviewer, project, video, "rejected").status_code == 200
    assert ctx.client.patch(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations/{annotation['id']}",
        json={"confidence": "uncertain"}, headers=headers).status_code == 200
    assert _submit(ctx, headers, project, video).status_code == 200

    with ctx.session_factory() as db:
        attempts = (db.query(Submission).filter_by(video_id=video["id"])
                    .order_by(Submission.attempt_no).all())
        assert [row.status for row in attempts] == ["rejected", "submitted"]
        assert db.query(SubmissionAnnotation).count() == 2

    row = _by_id(_stats(ctx, project, headers).json())[cat["id"]]
    # the superseded rejection must not leak into the current attempt's tally
    assert row["rejected"] == 0 and row["approved"] == 0
    assert row["pending"] == 1 and row["possible_total"] == 1


@pytest.mark.parametrize("member", ["outsider", "inactive"])
def test_behavior_stats_requires_active_membership(member, ctx, login_headers):
    _headers, _reviewer, project, _video, _categories, _singles = _setup(ctx, login_headers)
    user_id = ctx.create_user(member)
    if member == "inactive":
        ctx.add_member(project["id"], user_id)
        with ctx.session_factory() as db:
            db.query(ProjectMembership).filter_by(
                project_id=project["id"], user_id=user_id
            ).update({"status": "inactive"})
            db.commit()
    headers = login_headers(username=member, password="pw123")
    assert _stats(ctx, project, headers).status_code == 403
