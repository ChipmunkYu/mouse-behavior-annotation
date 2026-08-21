"""验收：有效/无效标注、更新与删除。"""
from __future__ import annotations


def _base_payload(category_id: int) -> dict:
    return {
        "category_id": category_id,
        "start_time": 1.0,
        "end_time": 3.0,
        "start_frame": 25,
        "end_frame": 75,
    }


def test_create_valid_annotation(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    cat = categories[0]
    resp = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations",
        json=_base_payload(cat["id"]),
        headers=headers,
    )
    assert resp.status_code == 201
    ann = resp.json()
    assert ann["annotator"] == "demo"
    assert ann["annotator_id"] == 1
    assert ann["category_name"] == cat["name"]
    assert ann["review_status"] == "pending"
    assert ann["confidence"] == "certain"
    assert ann["crop_region"] is None


def test_create_annotation_with_crop_region_and_confidence(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = setup["headers"], setup["project"], setup["categories"], setup["video"]
    payload = _base_payload(categories[0]["id"])
    payload.update(
        {
            "confidence": "uncertain",
            "crop_region": {"x": 10, "y": 20, "w": 50, "h": 30},
        }
    )
    resp = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 201
    ann = resp.json()
    assert ann["confidence"] == "uncertain"
    assert ann["crop_region"] == {"x": 10, "y": 20, "w": 50, "h": 30}


def test_create_annotation_invalid_interval(ctx):
    """end<=start / 帧号顺序错误 应被拒绝。"""
    setup = ctx.make_project_with_video()
    headers, project, categories, video = setup["headers"], setup["project"], setup["categories"], setup["video"]
    url = f"/api/projects/{project['id']}/videos/{video['id']}/annotations"

    # 时间顺序错误
    payload = _base_payload(categories[0]["id"])
    payload["end_time"] = 0.5
    assert ctx.client.post(url, json=payload, headers=headers).status_code == 400

    # 帧号顺序错误
    payload = _base_payload(categories[0]["id"])
    payload["end_frame"] = 10
    assert ctx.client.post(url, json=payload, headers=headers).status_code == 400

    # 负数帧（Pydantic ge=0 校验，422）
    payload = _base_payload(categories[0]["id"])
    payload["start_frame"] = -1
    assert ctx.client.post(url, json=payload, headers=headers).status_code in (400, 422)


def test_create_annotation_cross_project_category_rejected(ctx):
    """类别必须与视频属于同一项目。"""
    setup = ctx.make_project_with_video("项目X")
    headers, project, categories, video = setup["headers"], setup["project"], setup["categories"], setup["video"]

    other = ctx.client.post(
        "/api/projects", json={"name": "项目Y"}, headers=headers
    ).json()
    other_cat = ctx.configure_and_lock_minimal_scheme(other["id"], headers)[0]

    resp = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations",
        json=_base_payload(other_cat["id"]),
        headers=headers,
    )
    assert resp.status_code == 400


def test_create_annotation_non_member_rejected(ctx, login_headers):
    """标注者必须为项目成员。"""
    setup = ctx.make_project_with_video()
    project, video, categories = setup["project"], setup["video"], setup["categories"]

    alice_id = ctx.create_user("alice")
    alice_headers = login_headers(username="alice", password="pw123")
    resp = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations",
        json=_base_payload(categories[0]["id"]),
        headers=alice_headers,
    )
    assert resp.status_code == 403


def test_update_and_delete_annotation(ctx, login_headers):
    """验收：更新与删除。"""
    setup = ctx.make_project_with_video()
    headers, project, categories, video = setup["headers"], setup["project"], setup["categories"], setup["video"]
    base_url = f"/api/projects/{project['id']}/videos/{video['id']}/annotations"

    created = ctx.client.post(
        base_url, json=_base_payload(categories[0]["id"]), headers=headers
    ).json()
    ann_id = created["id"]

    # PATCH 更新时间/类别
    resp = ctx.client.patch(
        f"{base_url}/{ann_id}",
        json={"end_time": 5.0, "end_frame": 125, "category_id": categories[1]["id"]},
        headers=headers,
    )
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["end_time"] == 5.0
    assert updated["end_frame"] == 125
    assert updated["category_name"] == categories[1]["name"]

    # PATCH 产生非法区间 → 400
    resp = ctx.client.patch(
        f"{base_url}/{ann_id}", json={"end_time": 0.1}, headers=headers
    )
    assert resp.status_code == 400

    # PATCH 改成别的项目类别 → 400
    other = ctx.client.post(
        "/api/projects", json={"name": "别的项目"}, headers=headers
    ).json()
    other_cat = ctx.configure_and_lock_minimal_scheme(other["id"], headers)[0]
    resp = ctx.client.patch(
        f"{base_url}/{ann_id}", json={"category_id": other_cat["id"]}, headers=headers
    )
    assert resp.status_code == 400

    # 非标注者成员（annotator 角色）修改 → 403
    bob_id = ctx.create_user("bob")
    ctx.add_member(project["id"], bob_id, role="annotator")
    bob_headers = login_headers(username="bob", password="pw123")
    assert (
        ctx.client.patch(f"{base_url}/{ann_id}", json={"end_time": 6.0}, headers=bob_headers).status_code
        == 403
    )
    assert ctx.client.delete(f"{base_url}/{ann_id}", headers=bob_headers).status_code == 403

    # owner 修改/删除 → 200 / 204
    assert (
        ctx.client.patch(f"{base_url}/{ann_id}", json={"end_time": 6.0}, headers=headers).status_code
        == 200
    )
    assert ctx.client.delete(f"{base_url}/{ann_id}", headers=headers).status_code == 204

    # 删除后列表为空
    assert ctx.client.get(base_url, headers=headers).json() == []


def test_annotation_not_found_in_video(ctx):
    setup = ctx.make_project_with_video()
    headers, project, video = setup["headers"], setup["project"], setup["video"]
    url = f"/api/projects/{project['id']}/videos/{video['id']}/annotations"
    assert ctx.client.patch(f"{url}/9999", json={"end_time": 9.0}, headers=headers).status_code == 404
    assert ctx.client.delete(f"{url}/9999", headers=headers).status_code == 404


def test_annotation_cross_video_rejected(ctx):
    """标注属于另一视频时，在当前视频路径下应 404。"""
    setup = ctx.make_project_with_video()
    headers, project, categories, video = setup["headers"], setup["project"], setup["categories"], setup["video"]
    base_url = f"/api/projects/{project['id']}/videos/{video['id']}/annotations"
    ann = ctx.client.post(
        base_url, json=_base_payload(categories[0]["id"]), headers=headers
    ).json()

    video2 = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "session2.mp4"},
        headers=headers,
    ).json()
    url2 = f"/api/projects/{project['id']}/videos/{video2['id']}/annotations"
    assert ctx.client.patch(f"{url2}/{ann['id']}", json={"end_time": 9.0}, headers=headers).status_code == 404


def test_update_stale_annotation_revision_409(ctx, login_headers):
    """Fix 8: Stale detection_import_revision or identity_revision on update returns 409."""
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"], setup["project"], setup["categories"], setup["video"]
    )
    base_url = f"/api/projects/{project['id']}/videos/{video['id']}/annotations"

    created = ctx.client.post(
        base_url, json=_base_payload(categories[0]["id"]), headers=headers
    ).json()
    ann_id = created["id"]

    resp = ctx.client.patch(
        f"{base_url}/{ann_id}",
        json={"detection_import_revision": 1, "end_time": 3.0},
        headers=headers,
    )
    assert resp.status_code == 409
    assert "detection_import_revision mismatch" in resp.json()["detail"].lower()

    resp2 = ctx.client.patch(
        f"{base_url}/{ann_id}",
        json={"identity_revision": 1, "end_time": 3.0},
        headers=headers,
    )
    assert resp2.status_code == 409
    assert "identity_revision mismatch" in resp2.json()["detail"].lower()


def test_create_annotation_ignores_client_revisions(ctx, login_headers):
    """Fix 8: Create ignores client-supplied detection_import_revision/identity_revision."""
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"], setup["project"], setup["categories"], setup["video"]
    )
    base_url = f"/api/projects/{project['id']}/videos/{video['id']}/annotations"

    payload = _base_payload(categories[0]["id"])
    payload["detection_import_revision"] = 999
    payload["identity_revision"] = 888

    resp = ctx.client.post(base_url, json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    ann = resp.json()
    assert ann["detection_import_revision"] == 0
    assert ann["identity_revision"] == 0


def test_export_matches_export_event_contract(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"], setup["project"], setup["categories"], setup["video"]
    )
    created = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations",
        json=_base_payload(categories[0]["id"]),
        headers=headers,
    ).json()
    response = ctx.client.get(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations/export",
        headers=headers,
    )
    assert response.status_code == 200
    event = response.json()[0]
    assert event["annotation_id"] == created["id"]
    assert event["mouse_ids"] == []
    assert event["detection_import_revision"] == 0
    assert event["identity_revision"] == 0
    assert event["clip_file"] is None
