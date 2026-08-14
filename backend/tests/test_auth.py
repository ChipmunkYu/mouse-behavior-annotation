"""验收：登录。"""
from __future__ import annotations


def test_login_demo_ok(ctx):
    resp = ctx.client.post("/api/auth/login", json={"username": "demo", "password": "demo123"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["access_token"]
    assert data["user"]["username"] == "demo"
    assert data["user"]["id"] == 1


def test_login_wrong_password(ctx):
    resp = ctx.client.post("/api/auth/login", json={"username": "demo", "password": "wrong"})
    assert resp.status_code == 401


def test_login_unknown_user(ctx):
    resp = ctx.client.post("/api/auth/login", json={"username": "nobody", "password": "x"})
    assert resp.status_code == 401


def test_auth_me_returns_current_user(ctx, login_headers):
    resp = ctx.client.get("/api/auth/me", headers=login_headers())
    assert resp.status_code == 200
    assert resp.json()["id"] == 1
    assert resp.json()["username"] == "demo"


def test_auth_me_without_token_returns_401(ctx):
    assert ctx.client.get("/api/auth/me").status_code == 401


def test_auth_me_with_invalid_token_returns_401(ctx):
    resp = ctx.client.get(
        "/api/auth/me", headers={"Authorization": "Bearer invalid-token"}
    )
    assert resp.status_code == 401


def test_projects_requires_auth(ctx):
    assert ctx.client.get("/api/projects").status_code == 401


def test_token_works_for_projects(ctx, login_headers):
    headers = login_headers()
    assert ctx.client.get("/api/projects", headers=headers).status_code == 200
