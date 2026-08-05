"""验收：新建项目得到 owner 与 12 类。"""
from __future__ import annotations

EXPECTED_CATEGORIES = {
    "个体行为": ["奔跑", "行走", "静止"],
    "社交行为": ["一起", "接近", "追逐", "回避", "攻击行为", "鼻头接触", "鼻尾接触"],
    "群体行为": ["扎堆行为", "孤立行为"],
}


def test_create_project_owner_and_12_categories(ctx, login_headers):
    headers = login_headers()
    resp = ctx.client.post(
        "/api/projects", json={"name": "测试项目A", "description": "描述"}, headers=headers
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "测试项目A"
    assert data["role"] == "owner"
    assert data["status"] == "active"
    project_id = data["id"]

    cats = ctx.client.get(f"/api/projects/{project_id}/categories", headers=headers)
    assert cats.status_code == 200
    items = cats.json()
    assert len(items) == 12
    assert all(c["is_active"] for c in items)
    assert [c["sort_order"] for c in items] == list(range(12))

    by_group: dict[str, list[str]] = {}
    for c in items:
        by_group.setdefault(c["group"], []).append(c["name"])
    assert by_group == EXPECTED_CATEGORIES


def test_list_projects_with_role(ctx, login_headers):
    headers = login_headers()
    ctx.client.post("/api/projects", json={"name": "P1"}, headers=headers)
    resp = ctx.client.get("/api/projects", headers=headers)
    assert resp.status_code == 200
    projects = resp.json()
    assert any(p["name"] == "P1" and p["role"] == "owner" for p in projects)


def test_create_project_empty_name_rejected(ctx, login_headers):
    headers = login_headers()
    resp = ctx.client.post("/api/projects", json={"name": "   "}, headers=headers)
    assert resp.status_code in (400, 422)


def test_empty_project_list(ctx, login_headers):
    headers = login_headers()
    resp = ctx.client.get("/api/projects", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_project_not_found(ctx, login_headers):
    headers = login_headers()
    assert (
        ctx.client.get("/api/projects/9999/categories", headers=headers).status_code == 404
    )


def test_cross_project_access_rejected(ctx, login_headers):
    """验收：跨项目访问拒绝。"""
    demo_headers = login_headers()
    project_a = ctx.client.post(
        "/api/projects", json={"name": "项目A"}, headers=demo_headers
    ).json()

    alice_id = ctx.create_user("alice")
    alice_headers = login_headers(username="alice", password="pw123")

    # alice 未加入项目A：类别 / 视频 / 标注均被拒绝
    pa = project_a["id"]
    assert ctx.client.get(f"/api/projects/{pa}/categories", headers=alice_headers).status_code == 403
    assert ctx.client.get(f"/api/projects/{pa}/videos", headers=alice_headers).status_code == 403
    assert (
        ctx.client.get(f"/api/projects/{pa}/videos/1/annotations", headers=alice_headers).status_code
        == 403
    )

    # alice 自己的项目可访问
    project_b = ctx.client.post(
        "/api/projects", json={"name": "项目B"}, headers=alice_headers
    ).json()
    assert (
        ctx.client.get(f"/api/projects/{project_b['id']}/categories", headers=alice_headers).status_code
        == 200
    )

    # demo 也不能访问 alice 的项目
    assert (
        ctx.client.get(f"/api/projects/{project_b['id']}/categories", headers=demo_headers).status_code
        == 403
    )


def test_member_can_access_after_joining(ctx, login_headers):
    """加入项目后获得访问权限。"""
    demo_headers = login_headers()
    project = ctx.client.post(
        "/api/projects", json={"name": "共享项目"}, headers=demo_headers
    ).json()
    alice_id = ctx.create_user("alice")
    ctx.add_member(project["id"], alice_id, role="annotator")

    alice_headers = login_headers(username="alice", password="pw123")
    resp = ctx.client.get(f"/api/projects/{project['id']}/categories", headers=alice_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 12
