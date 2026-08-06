"""验收（批次 3）：提交 / 审核队列 / 审核历史 / 审核裁决。

覆盖契约：
- submit：owner/admin/annotator 可提交；reviewer 403；至少 1 条标注；draft/rejected 可提交，
  submitted/approved 拒绝；提交后 annotations 全部 pending、reviewer null。
- queue：owner/admin/reviewer 可见；只返回 submitted。
- reviews（历史）：项目成员可读；跨项目 404；非成员 403。
- review：owner/admin/reviewer 可裁决；仅 submitted；追加当前修订 Review；
  approved/rejected 同步 Video 与 annotations 审核字段。
"""
from __future__ import annotations

from app.models import Annotation, Review, Video


def _annotate(ctx, headers, project, video, category_id, start_time=1.0):
    resp = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations",
        json={
            "category_id": category_id,
            "start_time": start_time,
            "end_time": start_time + 2.0,
            "start_frame": int(start_time * 25),
            "end_frame": int((start_time + 2.0) * 25),
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _submit(ctx, headers, project, video):
    return ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/submit", headers=headers
    )


def _review(ctx, headers, project, video, result, comment="ok"):
    return ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/review",
        json={"result": result, "comment": comment},
        headers=headers,
    )


def _queue(ctx, headers, project):
    return ctx.client.get(f"/api/projects/{project['id']}/reviews/queue", headers=headers)


def _history(ctx, headers, project, video):
    return ctx.client.get(
        f"/api/projects/{project['id']}/videos/{video['id']}/reviews", headers=headers
    )


def _add_reviewer(ctx, project_id, username="reviewer1"):
    user_id = ctx.create_user(username)
    ctx.add_member(project_id, user_id, role="reviewer")
    return user_id


def _video_state(ctx, video_id) -> dict:
    with ctx.session_factory() as db:
        v = db.get(Video, video_id)
        return {
            "workflow_status": v.workflow_status,
            "annotation_revision": v.annotation_revision,
            "submitted_at": v.submitted_at,
            "approved_at": v.approved_at,
            "approved_by": v.approved_by,
        }


def _annotation_review_fields(ctx, video_id) -> list[tuple[str, int | None]]:
    with ctx.session_factory() as db:
        rows = (
            db.query(Annotation)
            .filter(Annotation.video_id == video_id)
            .order_by(Annotation.id)
            .all()
        )
        return [(a.review_status, a.reviewer_id) for a in rows]


# ---------- submit ----------


def test_submit_requires_at_least_one_annotation(ctx):
    setup = ctx.make_project_with_video()
    headers, project, video = setup["headers"], setup["project"], setup["video"]
    resp = _submit(ctx, headers, project, video)
    assert resp.status_code == 400
    assert "at least one annotation" in resp.json()["detail"].lower()


def test_submit_success_sets_submitted_and_resets_annotations(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    _annotate(ctx, headers, project, video, categories[0]["id"])

    resp = _submit(ctx, headers, project, video)
    assert resp.status_code == 200
    body = resp.json()
    assert body["workflow_status"] == "submitted"
    assert body["submitted_at"] is not None
    assert body["approved_at"] is None
    assert body["approved_by"] is None

    state = _video_state(ctx, video["id"])
    assert state["workflow_status"] == "submitted"
    assert state["annotation_revision"] == 1
    assert state["submitted_at"] is not None
    # 提交后标注全部 pending、reviewer 清空
    assert _annotation_review_fields(ctx, video["id"]) == [("pending", None)]


def test_submit_roles(ctx, login_headers):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    _annotate(ctx, headers, project, video, categories[0]["id"])

    # reviewer 不可提交
    reviewer_id = _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")
    assert _submit(ctx, reviewer_headers, project, video).status_code == 403

    # 非成员不可提交
    outsider_id = ctx.create_user("outsider")
    outsider_headers = login_headers(username="outsider", password="pw123")
    assert _submit(ctx, outsider_headers, project, video).status_code == 403

    # annotator 可提交
    annotator_id = ctx.create_user("annot1")
    ctx.add_member(project["id"], annotator_id, role="annotator")
    annotator_headers = login_headers(username="annot1", password="pw123")
    assert _submit(ctx, annotator_headers, project, video).status_code == 200

    # admin 可提交（先退回 rejected 再验证）
    _review(ctx, reviewer_headers, project, video, "rejected")
    admin_id = ctx.create_user("admin1")
    ctx.add_member(project["id"], admin_id, role="admin")
    admin_headers = login_headers(username="admin1", password="pw123")
    assert _submit(ctx, admin_headers, project, video).status_code == 200


def test_submit_state_gate_draft_rejected_ok_submitted_approved_rejected(ctx, login_headers):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    _annotate(ctx, headers, project, video, categories[0]["id"])
    _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")

    # draft → 可提交（上面已证明）；提交后再提交 → 400
    assert _submit(ctx, headers, project, video).status_code == 200
    assert _submit(ctx, headers, project, video).status_code == 400

    # rejected → 可重新提交；提交中再提交 → 400
    assert _review(ctx, reviewer_headers, project, video, "rejected").status_code == 200
    assert _video_state(ctx, video["id"])["workflow_status"] == "rejected"
    assert _submit(ctx, headers, project, video).status_code == 200
    assert _submit(ctx, headers, project, video).status_code == 400

    # approved → 拒绝提交
    assert _review(ctx, reviewer_headers, project, video, "approved").status_code == 200
    assert _video_state(ctx, video["id"])["workflow_status"] == "approved"
    assert _submit(ctx, headers, project, video).status_code == 400


def test_submit_resubmit_resets_annotation_review_fields(ctx, login_headers):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    ann = _annotate(ctx, headers, project, video, categories[0]["id"])
    _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")

    assert _submit(ctx, headers, project, video).status_code == 200
    assert _review(ctx, reviewer_headers, project, video, "rejected").status_code == 200
    # 审核后标注为 rejected 且 reviewer 已记录
    with ctx.session_factory() as db:
        a = db.get(Annotation, ann["id"])
        assert a.review_status == "rejected"
        assert a.reviewer_id is not None

    # 重新提交 → 标注回到 pending、reviewer 清空
    assert _submit(ctx, headers, project, video).status_code == 200
    assert _annotation_review_fields(ctx, video["id"]) == [("pending", None)]


def test_submit_cross_project_video_404(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    other = ctx.client.post("/api/projects", json={"name": "别的项目"}, headers=headers).json()
    resp = _submit(ctx, headers, other, video)
    assert resp.status_code == 404


# ---------- queue ----------


def test_queue_roles_and_submitted_only(ctx, login_headers):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    _annotate(ctx, headers, project, video, categories[0]["id"])

    video2 = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "s2.mp4"},
        headers=headers,
    ).json()
    _annotate(ctx, headers, project, video2, categories[1]["id"], start_time=5.0)

    # 提交一个、另一个保持 draft
    assert _submit(ctx, headers, project, video).status_code == 200

    # 非成员 403、annotator 403
    ctx.create_user("outsider")
    outsider_headers = login_headers(username="outsider", password="pw123")
    assert _queue(ctx, outsider_headers, project).status_code == 403
    annot_id = ctx.create_user("annot2")
    ctx.add_member(project["id"], annot_id, role="annotator")
    annot_headers = login_headers(username="annot2", password="pw123")
    assert _queue(ctx, annot_headers, project).status_code == 403

    # owner / reviewer / admin 可见，且只含 submitted
    _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")
    queue = _queue(ctx, headers, project)
    assert queue.status_code == 200
    ids = [v["id"] for v in queue.json()]
    assert ids == [video["id"]]  # draft 的 video2 不在队列

    # reviewer 视角一致
    assert [v["id"] for v in _queue(ctx, reviewer_headers, project).json()] == ids

    admin_id = ctx.create_user("admin3")
    ctx.add_member(project["id"], admin_id, role="admin")
    admin_headers = login_headers(username="admin3", password="pw123")
    assert [v["id"] for v in _queue(ctx, admin_headers, project).json()] == ids


def test_queue_excludes_rejected_and_approved(ctx, login_headers):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    _annotate(ctx, headers, project, video, categories[0]["id"])
    _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")

    assert _submit(ctx, headers, project, video).status_code == 200
    assert _queue(ctx, headers, project).json()  # submitted 在队列

    assert _review(ctx, reviewer_headers, project, video, "rejected").status_code == 200
    assert _queue(ctx, headers, project).json() == []  # rejected 不在队列

    assert _submit(ctx, headers, project, video).status_code == 200
    assert _review(ctx, reviewer_headers, project, video, "approved").status_code == 200
    assert _queue(ctx, headers, project).json() == []  # approved 不在队列


def test_queue_cross_project_isolation(ctx, login_headers):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    other = ctx.client.post("/api/projects", json={"name": "隔离项目"}, headers=headers).json()
    other_video = ctx.client.post(
        f"/api/projects/{other['id']}/videos", json={"filename": "o.mp4"}, headers=headers
    ).json()
    other_cat = ctx.client.get(
        f"/api/projects/{other['id']}/categories", headers=headers
    ).json()[0]
    _annotate(ctx, headers, other, other_video, other_cat["id"])
    assert _submit(ctx, headers, other, other_video).status_code == 200

    # A 项目的队列看不到 B 项目的 submitted 视频
    assert _queue(ctx, headers, project).json() == []


# ---------- history ----------


def test_review_history_member_readable_and_order(ctx, login_headers):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    _annotate(ctx, headers, project, video, categories[0]["id"])
    _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")

    assert _history(ctx, headers, project, video).json() == []

    assert _submit(ctx, headers, project, video).status_code == 200
    assert _review(ctx, reviewer_headers, project, video, "rejected", comment="改一下").status_code == 200

    history = _history(ctx, headers, project, video)
    assert history.status_code == 200
    rows = history.json()
    assert len(rows) == 1
    assert rows[0]["result"] == "rejected"
    assert rows[0]["comment"] == "改一下"
    assert rows[0]["annotation_revision"] == 1
    assert rows[0]["reviewer"] == "reviewer1"

    # 成员（annotator）也可读历史
    annot_id = ctx.create_user("annot4")
    ctx.add_member(project["id"], annot_id, role="annotator")
    annot_headers = login_headers(username="annot4", password="pw123")
    assert _history(ctx, annot_headers, project, video).status_code == 200

    # 非成员 403；跨项目 404
    ctx.create_user("outsider")
    outsider_headers = login_headers(username="outsider", password="pw123")
    assert _history(ctx, outsider_headers, project, video).status_code == 403
    other = ctx.client.post("/api/projects", json={"name": "另一个"}, headers=headers).json()
    assert _history(ctx, headers, other, video).status_code == 404


def test_review_history_accumulates_across_revisions(ctx, login_headers):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    ann = _annotate(ctx, headers, project, video, categories[0]["id"])
    _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")

    # 第一轮：提交 → 驳回（revision 1）
    assert _submit(ctx, headers, project, video).status_code == 200
    assert _review(ctx, reviewer_headers, project, video, "rejected").status_code == 200

    # 修改标注 → 视频失效回 draft、revision 2
    patch = ctx.client.patch(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations/{ann['id']}",
        json={"end_time": 9.0},
        headers=headers,
    )
    assert patch.status_code == 200
    assert _video_state(ctx, video["id"])["annotation_revision"] == 2

    # 第二轮：重新提交 → 通过（revision 2）
    assert _submit(ctx, headers, project, video).status_code == 200
    assert _review(ctx, reviewer_headers, project, video, "approved").status_code == 200

    rows = _history(ctx, headers, project, video).json()
    assert len(rows) == 2
    assert [r["annotation_revision"] for r in rows] == [1, 2]
    assert [r["result"] for r in rows] == ["rejected", "approved"]


# ---------- review ----------


def test_review_approve_syncs_video_and_annotations(ctx, login_headers):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    ann = _annotate(ctx, headers, project, video, categories[0]["id"])
    reviewer_id = _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")

    assert _submit(ctx, headers, project, video).status_code == 200
    resp = _review(ctx, reviewer_headers, project, video, "approved", comment="通过")
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "approved"
    assert body["comment"] == "通过"
    assert body["annotation_revision"] == 1
    assert body["reviewer"] == "reviewer1"

    state = _video_state(ctx, video["id"])
    assert state["workflow_status"] == "approved"
    assert state["approved_at"] is not None
    assert state["approved_by"] == reviewer_id
    assert state["submitted_at"] is not None

    with ctx.session_factory() as db:
        a = db.get(Annotation, ann["id"])
        assert a.review_status == "approved"
        assert a.reviewer_id == reviewer_id


def test_review_reject_syncs_video_and_annotations(ctx, login_headers):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    ann = _annotate(ctx, headers, project, video, categories[0]["id"])
    reviewer_id = _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")

    assert _submit(ctx, headers, project, video).status_code == 200
    assert _review(ctx, reviewer_headers, project, video, "rejected", comment="要改").status_code == 200

    state = _video_state(ctx, video["id"])
    assert state["workflow_status"] == "rejected"
    assert state["approved_at"] is None
    assert state["approved_by"] is None
    assert state["submitted_at"] is not None

    with ctx.session_factory() as db:
        a = db.get(Annotation, ann["id"])
        assert a.review_status == "rejected"
        assert a.reviewer_id == reviewer_id


def test_review_roles_and_state_gate(ctx, login_headers):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    _annotate(ctx, headers, project, video, categories[0]["id"])
    _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")

    # 非成员 403
    ctx.create_user("outsider")
    outsider_headers = login_headers(username="outsider", password="pw123")
    assert _review(ctx, outsider_headers, project, video, "approved").status_code == 403

    # annotator 不可审核
    annot_id = ctx.create_user("annot5")
    ctx.add_member(project["id"], annot_id, role="annotator")
    annot_headers = login_headers(username="annot5", password="pw123")
    assert _review(ctx, annot_headers, project, video, "approved").status_code == 403

    # 未提交（draft）不可审核
    assert _review(ctx, reviewer_headers, project, video, "approved").status_code == 400

    # 提交后审核 → 通过；再审核 → 400（已非 submitted）
    assert _submit(ctx, headers, project, video).status_code == 200
    assert _review(ctx, reviewer_headers, project, video, "approved").status_code == 200
    assert _review(ctx, reviewer_headers, project, video, "rejected").status_code == 400

    # 跨项目视频 404（owner 同时是两项目成员 → 通过项目权限后按视频归属返回 404）
    other = ctx.client.post("/api/projects", json={"name": "别的"}, headers=headers).json()
    assert _review(ctx, headers, other, video, "approved").status_code == 404
