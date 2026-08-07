"""验收（批次 5）：生产跨视频片段库。

覆盖契约：
- 仅聚合审核通过的标注与对应 ready 的 Clip；pending/rejected 隔离，失效残留排除。
- Clip 非 ready（缺失/pending/failed）时 clip_path/thumbnail_path 为 null；
  ready 时返回相对路径（与批次 4 产物命名一致）。
- 分页默认 20 / 上限 100 / page≥1；排序 start_time + id 稳定分页。
- 筛选：category_id / video_id / annotator_id / search（类别名或视频文件名）。
- 跨项目隔离；不同视频片段聚合；ClipItem 字段完整性。
- 成员权限：非成员 403、项目不存在 404、未登录 401；review_status 仅允许 approved。
- 类别统计接口（仅审核通过片段）。
"""
from __future__ import annotations

from datetime import datetime

from app.models import Annotation, Clip, User, Video

from .conftest import auth_headers


# ---------- 辅助 ----------


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


def _new_video(ctx, project, headers, name="v.mp4"):
    return ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": name, "duration": 30.0, "fps": 25.0},
        headers=headers,
    ).json()


def _library(ctx, project_id, headers, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"/api/projects/{project_id}/clips"
    if query:
        url += f"?{query}"
    return ctx.client.get(url, headers=headers)


def _categories(ctx, project_id, headers):
    return ctx.client.get(f"/api/projects/{project_id}/clips/categories", headers=headers)


def _cat_id(categories, name) -> int:
    for c in categories:
        if c["name"] == name:
            return c["id"]
    raise AssertionError(f"category not found: {name}")


def _set_video_approved(ctx, video_id) -> None:
    with ctx.session_factory() as db:
        v = db.get(Video, video_id)
        v.workflow_status = "approved"
        v.approved_at = datetime.utcnow()
        db.commit()


def _set_annotation_status(ctx, annotation_id, status) -> None:
    with ctx.session_factory() as db:
        a = db.get(Annotation, annotation_id)
        a.review_status = status
        db.commit()


def _add_clip(ctx, project_id, annotation_id, status="ready", rev=1) -> None:
    with ctx.session_factory() as db:
        clip = Clip(
            project_id=project_id,
            annotation_id=annotation_id,
            source_revision=rev,
            status=status,
        )
        if status == "ready":
            clip.clip_path = f"clip_{annotation_id}_rev{rev}.mp4"
            clip.thumbnail_path = f"clip_{annotation_id}_rev{rev}.jpg"
        db.add(clip)
        db.commit()


def _approve(ctx, project, video) -> None:
    """提交并 approve（media_ctx 同步 worker 自动生成 ready clips）。"""
    with ctx.session_factory() as db:
        n = db.query(User).count()
    name = f"rev{n}"
    user_id = ctx.create_user(name)
    ctx.add_member(project["id"], user_id, role="reviewer")
    rev_headers = auth_headers(ctx.client, name, "pw123")
    sub = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/submit",
        headers=auth_headers(ctx.client),
    )
    assert sub.status_code == 200, sub.text
    resp = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/review",
        json={"result": "approved", "comment": "ok"},
        headers=rev_headers,
    )
    assert resp.status_code == 200, resp.text


# ---------- 隔离：审核状态 / 视频状态 ----------


def test_library_excludes_pending_and_rejected(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    video2 = _new_video(ctx, project, headers, "b.mp4")
    ann_ok = _annotate(ctx, headers, project, video, categories[0]["id"], start_time=1.0)
    ann_pending = _annotate(ctx, headers, project, video2, categories[1]["id"], start_time=5.0)

    # 两个视频都置 approved；video2 的标注保持 pending → 隔离
    _set_video_approved(ctx, video["id"])
    _set_video_approved(ctx, video2["id"])
    _set_annotation_status(ctx, ann_ok["id"], "approved")
    _add_clip(ctx, project["id"], ann_ok["id"], "ready")

    body = _library(ctx, project["id"], headers).json()
    assert body["total"] == 1
    assert body["items"][0]["annotation_id"] == ann_ok["id"]

    # 再让第二张 approved → 入库；随后把第一张置 rejected → 仍隔离
    _set_annotation_status(ctx, ann_pending["id"], "approved")
    _add_clip(ctx, project["id"], ann_pending["id"], "ready")
    assert _library(ctx, project["id"], headers).json()["total"] == 2

    _set_annotation_status(ctx, ann_ok["id"], "rejected")
    assert _library(ctx, project["id"], headers).json()["total"] == 1
    assert _library(ctx, project["id"], headers).json()["items"][0]["annotation_id"] == ann_pending["id"]


def test_library_excludes_approved_annotation_in_draft_video(ctx):
    """失效窗口残留：标注 review_status=approved 但视频已回 draft → 不入库。"""
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    ann = _annotate(ctx, headers, project, video, categories[0]["id"])
    _set_annotation_status(ctx, ann["id"], "approved")
    # 视频保持 draft（模拟失效回 draft 后的残留 approved 标注）
    body = _library(ctx, project["id"], headers).json()
    assert body == {"items": [], "total": 0, "pages": 0}


def test_empty_library_zero_pages(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    _annotate(ctx, headers, project, video, categories[0]["id"])
    body = _library(ctx, project["id"], headers).json()
    assert body == {"items": [], "total": 0, "pages": 0}


# ---------- ready / 非 ready ----------


def test_non_ready_clip_paths_are_null(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    a_ready = _annotate(ctx, headers, project, video, categories[0]["id"], start_time=1.0)
    a_pending = _annotate(ctx, headers, project, video, categories[1]["id"], start_time=5.0)
    a_failed = _annotate(ctx, headers, project, video, categories[2]["id"], start_time=9.0)
    a_noclip = _annotate(ctx, headers, project, video, categories[3]["id"], start_time=13.0)

    _set_video_approved(ctx, video["id"])
    for a in (a_ready, a_pending, a_failed, a_noclip):
        _set_annotation_status(ctx, a["id"], "approved")
    _add_clip(ctx, project["id"], a_ready["id"], "ready")
    _add_clip(ctx, project["id"], a_pending["id"], "pending")
    _add_clip(ctx, project["id"], a_failed["id"], "failed")

    body = _library(ctx, project["id"], headers).json()
    items = {i["annotation_id"]: i for i in body["items"]}
    assert set(items) == {a_ready["id"], a_pending["id"], a_failed["id"], a_noclip["id"]}
    assert items[a_ready["id"]]["clip_path"] == f"clip_{a_ready['id']}_rev1.mp4"
    assert items[a_ready["id"]]["thumbnail_path"] == f"clip_{a_ready['id']}_rev1.jpg"
    for a in (a_pending, a_failed, a_noclip):
        assert items[a["id"]]["clip_path"] is None
        assert items[a["id"]]["thumbnail_path"] is None


def test_ready_paths_match_media_generation(media_ctx):
    """真实 media 流程：approve 后 Clip ready，库返回批次 4 产物的相对路径。"""
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project = ctx.client.post("/api/projects", json={"name": "片段库媒体"}, headers=headers).json()
    categories = ctx.client.get(f"/api/projects/{project['id']}/categories", headers=headers).json()
    src = ctx.app.state.settings.videos_dir / "src.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"FAKE-SRC")
    video = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={
            "filename": "src.mp4",
            "storage_path": "src.mp4",
            "status": "uploaded",
            "duration": 30.0,
            "fps": 25.0,
        },
        headers=headers,
    ).json()
    anns = [
        _annotate(ctx, headers, project, video, categories[i]["id"], start_time=1.0 + i * 3.0)
        for i in range(2)
    ]
    _approve(ctx, project, video)

    body = _library(ctx, project["id"], headers).json()
    assert body["total"] == 2
    # items 按 start_time 排序，与 anns 创建顺序一致
    for item, ann in zip(body["items"], anns):
        assert item["clip_path"] == f"clip_{ann['id']}_rev1.mp4"
        assert item["thumbnail_path"] == f"clip_{ann['id']}_rev1.jpg"


# ---------- 分页 / 排序 ----------


def test_pagination_default_size_and_pages(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    anns = [
        _annotate(ctx, headers, project, video, categories[i % 3]["id"], start_time=i * 0.5 + 1.0)
        for i in range(25)
    ]
    _set_video_approved(ctx, video["id"])
    for a in anns:
        _set_annotation_status(ctx, a["id"], "approved")

    body = _library(ctx, project["id"], headers).json()
    assert body["total"] == 25
    assert body["pages"] == 2
    assert len(body["items"]) == 20  # 默认 page_size=20

    page2 = _library(ctx, project["id"], headers, page=2).json()
    assert page2["total"] == 25
    assert page2["pages"] == 2
    assert len(page2["items"]) == 5

    # 两页拼接 = 全量、无重复
    ids = [i["annotation_id"] for i in body["items"] + page2["items"]]
    assert len(ids) == 25 and len(set(ids)) == 25

    # 越界页 → 空 items
    empty = _library(ctx, project["id"], headers, page=3).json()
    assert empty["items"] == []
    assert empty["total"] == 25
    assert empty["pages"] == 2

    # 自定义 page_size
    body5 = _library(ctx, project["id"], headers, page_size=10).json()
    assert body5["pages"] == 3
    assert len(body5["items"]) == 10


def test_pagination_page_size_limits(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    assert _library(ctx, project["id"], headers, page=0).status_code == 422
    assert _library(ctx, project["id"], headers, page_size=0).status_code == 422
    assert _library(ctx, project["id"], headers, page_size=101).status_code == 422


def test_stable_order_by_start_time_then_id(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    # 3 张标注 start_time 相同（1.0），id 递增 → 以 id 稳定排序
    a1 = _annotate(ctx, headers, project, video, categories[0]["id"], start_time=1.0)
    a2 = _annotate(ctx, headers, project, video, categories[1]["id"], start_time=1.0)
    a3 = _annotate(ctx, headers, project, video, categories[2]["id"], start_time=1.0)
    _set_video_approved(ctx, video["id"])
    for a in (a1, a2, a3):
        _set_annotation_status(ctx, a["id"], "approved")

    ids = [i["annotation_id"] for i in _library(ctx, project["id"], headers).json()["items"]]
    assert ids == [a1["id"], a2["id"], a3["id"]]


# ---------- 筛选 ----------


def test_filters_category_video_annotator_search(ctx, login_headers):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    video2 = _new_video(ctx, project, headers, "social.mp4")

    # 另一个标注者
    other_id = ctx.create_user("annot_b")
    ctx.add_member(project["id"], other_id, role="annotator")
    other_headers = login_headers(username="annot_b", password="pw123")

    atk = _cat_id(categories, "攻击行为")
    together = _cat_id(categories, "一起")
    a1 = _annotate(ctx, headers, project, video, atk, start_time=1.0)
    a2 = _annotate(ctx, other_headers, project, video2, atk, start_time=2.0)
    a3 = _annotate(ctx, other_headers, project, video2, together, start_time=3.0)

    _set_video_approved(ctx, video["id"])
    _set_video_approved(ctx, video2["id"])
    for a in (a1, a2, a3):
        _set_annotation_status(ctx, a["id"], "approved")

    # 全量 3
    assert _library(ctx, project["id"], headers).json()["total"] == 3
    # category_id
    body = _library(ctx, project["id"], headers, category_id=atk).json()
    assert body["total"] == 2
    assert {i["annotation_id"] for i in body["items"]} == {a1["id"], a2["id"]}
    # video_id
    body = _library(ctx, project["id"], headers, video_id=video2["id"]).json()
    assert body["total"] == 2
    assert all(i["video_filename"] == "social.mp4" for i in body["items"])
    # annotator_id
    body = _library(ctx, project["id"], headers, annotator_id=other_id).json()
    assert body["total"] == 2
    assert {i["annotation_id"] for i in body["items"]} == {a2["id"], a3["id"]}
    # search 类别名
    body = _library(ctx, project["id"], headers, search="攻击").json()
    assert body["total"] == 2
    assert {i["annotation_id"] for i in body["items"]} == {a1["id"], a2["id"]}
    # search 文件名
    body = _library(ctx, project["id"], headers, search="social").json()
    assert body["total"] == 2
    assert all(i["video_filename"] == "social.mp4" for i in body["items"])
    # search 无命中
    assert _library(ctx, project["id"], headers, search="不存在xyz").json()["total"] == 0
    # 组合筛选
    body = _library(ctx, project["id"], headers, video_id=video2["id"], category_id=atk).json()
    assert body["total"] == 1
    assert body["items"][0]["annotation_id"] == a2["id"]


def test_review_status_param_rejects_non_approved(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    assert _library(ctx, project["id"], headers, review_status="pending").status_code == 422
    assert _library(ctx, project["id"], headers, review_status="rejected").status_code == 422


# ---------- 跨项目 / 聚合 ----------


def test_cross_project_isolation(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    other = ctx.client.post("/api/projects", json={"name": "别的项目"}, headers=headers).json()
    other_video = _new_video(ctx, other, headers, "other.mp4")
    other_categories = ctx.client.get(f"/api/projects/{other['id']}/categories", headers=headers).json()

    a = _annotate(ctx, headers, project, video, categories[0]["id"])
    oa = _annotate(ctx, headers, other, other_video, other_categories[0]["id"])
    _set_video_approved(ctx, video["id"])
    _set_video_approved(ctx, other_video["id"])
    _set_annotation_status(ctx, a["id"], "approved")
    _set_annotation_status(ctx, oa["id"], "approved")

    body = _library(ctx, project["id"], headers).json()
    assert body["total"] == 1
    assert body["items"][0]["annotation_id"] == a["id"]
    # 另一项目同样只看到自己的片段
    other_body = _library(ctx, other["id"], headers).json()
    assert other_body["total"] == 1
    assert other_body["items"][0]["annotation_id"] == oa["id"]


def test_aggregates_across_multiple_videos(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    video2 = _new_video(ctx, project, headers, "video2.mp4")
    video3 = _new_video(ctx, project, headers, "video3.mp4")
    a1 = _annotate(ctx, headers, project, video, categories[0]["id"], start_time=1.0)
    a2 = _annotate(ctx, headers, project, video2, categories[1]["id"], start_time=5.0)
    a3 = _annotate(ctx, headers, project, video3, categories[2]["id"], start_time=9.0)
    for v in (video, video2, video3):
        _set_video_approved(ctx, v["id"])
    for a in (a1, a2, a3):
        _set_annotation_status(ctx, a["id"], "approved")

    body = _library(ctx, project["id"], headers).json()
    assert body["total"] == 3
    by_ann = {i["annotation_id"]: i for i in body["items"]}
    assert by_ann[a1["id"]]["video_filename"] == "session1.mp4"
    assert by_ann[a2["id"]]["video_filename"] == "video2.mp4"
    assert by_ann[a3["id"]]["video_filename"] == "video3.mp4"
    # 跨视频按 start_time 聚合排序
    assert [i["annotation_id"] for i in body["items"]] == [a1["id"], a2["id"], a3["id"]]


# ---------- 权限 ----------


def test_member_permissions(ctx, login_headers):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    ctx.create_user("outsider")
    outsider_headers = login_headers(username="outsider", password="pw123")
    assert _library(ctx, project["id"], outsider_headers).status_code == 403
    assert _categories(ctx, project["id"], outsider_headers).status_code == 403
    # 未登录
    assert ctx.client.get(f"/api/projects/{project['id']}/clips").status_code == 401
    assert ctx.client.get(f"/api/projects/{project['id']}/clips/categories").status_code == 401
    # 项目不存在
    assert _library(ctx, 999999, headers).status_code == 404


# ---------- 类别统计 ----------


def test_categories_counts_and_order(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    atk = _cat_id(categories, "攻击行为")
    together = _cat_id(categories, "一起")
    a1 = _annotate(ctx, headers, project, video, atk, start_time=1.0)
    a2 = _annotate(ctx, headers, project, video, atk, start_time=4.0)
    a3 = _annotate(ctx, headers, project, video, together, start_time=7.0)
    a4 = _annotate(ctx, headers, project, video, together, start_time=10.0)
    _set_video_approved(ctx, video["id"])
    for a in (a1, a2, a3):
        _set_annotation_status(ctx, a["id"], "approved")
    # a4 保持 pending → 不计

    resp = _categories(ctx, project["id"], headers)
    assert resp.status_code == 200
    counts = {c["category_id"]: c for c in resp.json()}
    assert counts[atk]["category_name"] == "攻击行为"
    assert counts[atk]["count"] == 2
    assert counts[together]["count"] == 1
    assert len(resp.json()) == 2  # 计数为 0 的类别不出现
    # 按 sort_order 排序（一起=3 在 攻击行为=7 之前）
    assert [c["category_id"] for c in resp.json()] == [together, atk]


def test_categories_isolated_per_project_and_approved_only(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    other = ctx.client.post("/api/projects", json={"name": "类别隔离项目"}, headers=headers).json()
    other_video = _new_video(ctx, other, headers, "o.mp4")
    other_categories = ctx.client.get(f"/api/projects/{other['id']}/categories", headers=headers).json()

    a = _annotate(ctx, headers, project, video, categories[0]["id"])
    oa = _annotate(ctx, headers, other, other_video, other_categories[0]["id"])
    _set_video_approved(ctx, video["id"])
    _set_video_approved(ctx, other_video["id"])
    _set_annotation_status(ctx, a["id"], "approved")
    _set_annotation_status(ctx, oa["id"], "approved")

    assert _categories(ctx, project["id"], headers).json() == [
        {"category_id": categories[0]["id"], "category_name": categories[0]["name"], "count": 1}
    ]


# ---------- ClipItem 字段完整性 ----------


def test_clipitem_field_completeness(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    ann = _annotate(ctx, headers, project, video, categories[0]["id"], start_time=1.0)
    _set_video_approved(ctx, video["id"])
    _set_annotation_status(ctx, ann["id"], "approved")
    _add_clip(ctx, project["id"], ann["id"], "ready")

    item = _library(ctx, project["id"], headers).json()["items"][0]
    assert {
        k: v for k, v in item.items() if k != "created_at"
    } == {
        "annotation_id": ann["id"],
        "video_id": video["id"],
        "video_filename": "session1.mp4",
        "category_id": categories[0]["id"],
        "category_name": categories[0]["name"],
        "start_time": 1.0,
        "end_time": 3.0,
        "start_frame": 25,
        "end_frame": 75,
        "confidence": "certain",
        "clip_path": f"clip_{ann['id']}_rev1.mp4",
        "thumbnail_path": f"clip_{ann['id']}_rev1.jpg",
        "annotator_name": "demo",
        "review_status": "approved",
    }
    assert item["created_at"] == ann["created_at"]
