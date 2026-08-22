"""验收：新建项目原子得到 owner 与非空、未锁定的完整类别方案。"""
from __future__ import annotations

import hashlib
import json

import pytest

from app.models import BehaviorCategory, CategorySchemeAudit, Project, ProjectMembership

EXPECTED_CATEGORIES = {
    "个体行为": ["奔跑", "行走", "静止"],
    "社交行为": ["一起", "接近", "追逐", "回避", "攻击行为", "鼻头接触", "鼻尾接触"],
    "群体行为": ["扎堆行为", "孤立行为"],
}


def test_create_project_owner_with_initial_unordered_scheme(ctx, login_headers):
    headers = login_headers()
    resp = ctx.client.post(
        "/api/projects", json={
            "name": "测试项目A", "description": "描述", "categories": [{
                "name": "  行走  ", "group": "  个体行为 ", "sort_order": 0,
                "color": "#4CAF50",
                "participant_mode": "unordered", "role_definitions": [],
                "mouse_count_min": 1, "mouse_count_max": 2,
            }],
        }, headers=headers
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "测试项目A"
    assert data["role"] == "owner"
    assert data["status"] == "active"
    project_id = data["id"]

    assert data["category_scheme_version"] == 0
    assert data["category_scheme_locked_at"] is None
    assert data["category_scheme_locked_by"] is None
    assert ctx.client.get(f"/api/projects/{project_id}/categories", headers=headers).status_code == 409
    scheme = ctx.client.get(f"/api/projects/{project_id}/category-scheme", headers=headers)
    assert scheme.status_code == 200
    category = scheme.json()["categories"][0]
    assert category["name"] == "行走"
    assert category["group"] == "个体行为"
    assert category["participant_mode"] == "unordered"
    audit = ctx.client.get(f"/api/projects/{project_id}/category-scheme/audit", headers=headers).json()
    assert len(audit) == 1 and audit[0]["action"] == "replace"
    assert audit[0]["scheme_version"] == 0
    assert audit[0]["before_json"]["categories"] == []
    assert audit[0]["after_json"]["categories"][0]["id"] == category["id"]
    encoded = json.dumps(
        audit[0]["after_json"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert audit[0]["scheme_hash"] == hashlib.sha256(encoded.encode()).hexdigest()


def test_create_project_role_based_then_put_and_lock(ctx, login_headers):
    headers = login_headers()
    created = ctx.raw_client.post("/api/projects", json={
        "name": "角色项目",
        "categories": [{
            "name": "追逐", "group": "社交行为", "sort_order": 0,
            "color": "orange",
            "participant_mode": "role_based", "role_definitions": [
                {"name": "追逐者", "min_count": 1, "max_count": 1, "role_sort_order": 0},
                {"name": "被追逐者", "min_count": 1, "max_count": None, "role_sort_order": 1},
            ],
        }],
    }, headers=headers)
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    scheme = ctx.client.get(f"/api/projects/{pid}/category-scheme", headers=headers).json()
    category = scheme["categories"][0]
    assert scheme["category_scheme_version"] == 0
    assert category["mouse_count_min"] == 2 and category["mouse_count_max"] is None
    assert all(role["key"].startswith("role_") for role in category["role_definitions"])
    replacement = dict(category)
    replacement["name"] = "追逐更新"
    replacement.pop("project_id")
    replacement.pop("mouse_count_min")
    replacement.pop("mouse_count_max")
    response = ctx.client.put(
        f"/api/projects/{pid}/category-scheme",
        json={"expected_version": 0, "categories": [replacement]}, headers=headers,
    )
    assert response.status_code == 200, response.text
    locked = ctx.client.post(
        f"/api/projects/{pid}/category-scheme/lock",
        json={"expected_version": 1}, headers=headers,
    )
    assert locked.status_code == 200


@pytest.mark.parametrize("payload", [
    {"name": "缺失类别"},
    {"name": "空类别", "categories": []},
    {"name": "缺失颜色", "categories": [{"name": "A", "group": "G", "sort_order": 0}]},
    {"name": "空颜色", "categories": [{"name": "A", "group": "G", "color": None, "sort_order": 0}]},
    {"name": "空白颜色", "categories": [{"name": "A", "group": "G", "color": "   ", "sort_order": 0}]},
    {"name": "非法顺序", "categories": [{
        "name": "A", "group": "G", "color": "red", "sort_order": 1,
    }]},
    {"name": "伪造类别", "categories": [{
        "id": 1, "name": "A", "group": "G", "color": "red", "sort_order": 0,
    }]},
    {"name": "伪造角色", "categories": [{
        "name": "A", "group": "G", "color": "red", "sort_order": 0,
        "participant_mode": "role_based",
        "role_definitions": [{
            "key": "role_11111111111111111111111111111111", "name": "R",
            "min_count": 1, "max_count": 1, "role_sort_order": 0,
        }],
    }]},
])
def test_invalid_project_scheme_is_atomic(ctx, login_headers, payload):
    headers = login_headers()
    with ctx.session_factory() as db:
        before = tuple(db.query(model).count() for model in (
            Project, ProjectMembership, BehaviorCategory, CategorySchemeAudit
        ))
    response = ctx.raw_client.post("/api/projects", json=payload, headers=headers)
    assert response.status_code == 422
    with ctx.session_factory() as db:
        after = tuple(db.query(model).count() for model in (
            Project, ProjectMembership, BehaviorCategory, CategorySchemeAudit
        ))
    assert after == before


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
    ctx.configure_and_lock_minimal_scheme(project_b["id"], alice_headers)
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
    """加入项目后可访问 helper 显式建立的十二类锁定方案。"""
    demo_headers = login_headers()
    project = ctx.client.post(
        "/api/projects", json={"name": "共享项目"}, headers=demo_headers
    ).json()
    ctx.configure_and_lock_minimal_scheme(project["id"], demo_headers)
    alice_id = ctx.create_user("alice")
    ctx.add_member(project["id"], alice_id, role="annotator")

    alice_headers = login_headers(username="alice", password="pw123")
    resp = ctx.client.get(f"/api/projects/{project['id']}/categories", headers=alice_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 12
