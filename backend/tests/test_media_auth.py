from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.auth import create_access_token
from app.config import Settings
from app.media_auth import (
    BINDING_KEY_LABEL,
    RAW_BEARER_KEY_LABEL,
    TICKET_KEY_LABEL,
    MediaKeys,
    bearer_binding,
    encode_media_jwt,
    raw_cookie_values,
)
from app.models import Video


def _enable(ctx) -> Settings:
    settings = ctx.raw_client.app.state.settings
    settings.media_ticket_enabled = True
    return settings


def _video(ctx, tmp_path, headers, content=b"0123456789abcdef"):
    project = ctx.client.post("/api/projects", json={"name": "ticket-media"}, headers=headers).json()
    path = tmp_path / "videos" / "ticket.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    video = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "ticket.mp4"}, headers=headers,
    ).json()
    with ctx.session_factory() as db:
        db.get(Video, video["id"]).storage_path = str(path)
        db.commit()
    return project, video


def _cookie_value(response, name: str) -> str:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(name + "="):
            return header.split(";", 1)[0].split("=", 1)[1]
    raise AssertionError(f"missing cookie {name}")


def _set_cookie_headers(response) -> list[str]:
    return response.headers.get_list("set-cookie")


def _raw_ticket_request(ctx, url: str, bearer: str, cookie_fields: list[str]):
    ctx.raw_client.cookies.clear()
    return ctx.raw_client.post(
        url,
        headers=[("Authorization", f"Bearer {bearer}")] + [("Cookie", value) for value in cookie_fields],
    )


def _ticket_pair(ctx, tmp_path):
    settings = _enable(ctx)
    login = ctx.client.post("/api/auth/login", json={"username": "demo", "password": "demo123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    _, video = _video(ctx, tmp_path, headers)
    issued = ctx.client.post(f"/api/videos/{video['id']}/stream-ticket", headers=headers)
    assert issued.status_code == 200, issued.text
    return settings, token, video, issued


def test_crypto_fixed_vectors_and_domain_separation():
    settings = Settings(media_master_secret="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8")
    keys = MediaKeys.from_settings(settings)
    assert keys.ticket.hex() == "0fe0e737dbf56e9ba09d6a1a6145a70533a2329a7753425fb299046e57139c2d"
    assert keys.binding_jwt.hex() == "460a34250ea99dd66c60582db4913b23506f41aa9681f741180bb78f8d40cd7c"
    assert keys.raw_bearer.hex() == "66e760c94ed0e90ba6f8352e483f36b384b0d355de98de2233a33a612022818a"
    assert bearer_binding("fixed-bearer", keys.raw_bearer) == "AN8i0FMtxTqFUIqareXO7kZdexAMr5M3GFj1pGYgtm4"
    ticket_payload = {
        "sub": "7", "video_id": 11, "binding": "fixed", "aud": "video-stream",
        "typ": "media-ticket", "iat": 1700000000, "exp": 1700007200,
    }
    binding_payload = {
        "sub": "7", "binding": "fixed", "aud": "video-stream-binding",
        "typ": "media-binding", "iat": 1700000000, "exp": 1700007200,
    }
    assert encode_media_jwt(ticket_payload, keys.ticket) == (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI3IiwidmlkZW9faWQiOjExLCJiaW5kaW5nIjoiZml4ZWQiLCJhdWQiOiJ2aWRlby1zdHJlYW0iLCJ0eXAiOiJtZWRpYS10aWNrZXQiLCJpYXQiOjE3MDAwMDAwMDAsImV4cCI6MTcwMDAwNzIwMH0.5E6yIOtK4B8SCnHWURoB0v1dzNXNZ3Q_AEJAxnFL3nQ"
    )
    assert encode_media_jwt(binding_payload, keys.binding_jwt) == (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI3IiwiYmluZGluZyI6ImZpeGVkIiwiYXVkIjoidmlkZW8tc3RyZWFtLWJpbmRpbmciLCJ0eXAiOiJtZWRpYS1iaW5kaW5nIiwiaWF0IjoxNzAwMDAwMDAwLCJleHAiOjE3MDAwMDcyMDB9.Q6H909IZFLpiWKFkg5KJ1BrHXrks5C6S73UUGjhDv6Q"
    )
    master = bytes(range(32))
    assert keys.ticket == hmac.new(master, TICKET_KEY_LABEL, hashlib.sha256).digest()
    assert keys.binding_jwt == hmac.new(master, BINDING_KEY_LABEL, hashlib.sha256).digest()
    assert keys.raw_bearer == hmac.new(master, RAW_BEARER_KEY_LABEL, hashlib.sha256).digest()


def test_login_bearer_has_iat_random_jti_and_clears_then_sets_binding(ctx):
    settings = _enable(ctx)
    first = ctx.client.post("/api/auth/login", json={"username": "demo", "password": "demo123"})
    second = ctx.client.post("/api/auth/login", json={"username": "demo", "password": "demo123"})
    first_claims = jwt.decode(first.json()["access_token"], options={"verify_signature": False})
    second_claims = jwt.decode(second.json()["access_token"], options={"verify_signature": False})
    assert isinstance(first_claims["iat"], int)
    assert first_claims["jti"] != second_claims["jti"]
    cookies = first.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert cookies[0].startswith(settings.media_binding_cookie_name + "=") and "Max-Age=0" in cookies[0]
    assert cookies[1].startswith(settings.media_binding_cookie_name + "=")
    for header in cookies:
        assert "Path=/api/videos/" in header
        assert "HttpOnly" in header and "Secure" in header and "SameSite=strict" in header
        assert "Domain=" not in header


@pytest.mark.parametrize("authorization", [None, "Bearer invalid", "Bearer expired", "Bearer valid"])
def test_logout_is_unauthenticated_idempotent_clear_only(ctx, authorization):
    settings = ctx.raw_client.app.state.settings
    headers = {} if authorization is None else {"Authorization": authorization}
    response = ctx.client.post("/api/auth/logout", headers=headers)
    assert response.status_code == 204 and response.content == b""
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(settings.media_binding_cookie_name + "=")
    assert "Max-Age=0" in cookie and "Path=/api/videos/" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert "Domain=" not in cookie


def test_old_access_token_remains_compatible_for_ticket(ctx, tmp_path):
    settings = _enable(ctx)
    exp = datetime.now(timezone.utc) + timedelta(minutes=5)
    old = jwt.encode({"sub": "1", "exp": exp}, settings.secret_key, algorithm="HS256")
    headers = {"Authorization": f"Bearer {old}"}
    _, video = _video(ctx, tmp_path, headers)
    response = ctx.client.post(f"/api/videos/{video['id']}/stream-ticket", headers=headers)
    assert response.status_code == 200
    ticket = _cookie_value(response, settings.media_ticket_cookie_name)
    claims = jwt.decode(
        ticket, MediaKeys.from_settings(settings).ticket,
        algorithms=["HS256"], audience=settings.media_ticket_audience,
    )
    assert claims["exp"] == int(exp.timestamp())
    ticket_cookie = next(
        item for item in response.headers.get_list("set-cookie")
        if item.startswith(settings.media_ticket_cookie_name + "=")
    )
    max_age = int(next(part.split("=", 1)[1] for part in ticket_cookie.split("; ") if part.startswith("Max-Age=")))
    assert 0 < max_age <= 300


def test_stream_ticket_existing_binding_must_match_current_raw_bearer(ctx, tmp_path):
    settings = _enable(ctx)
    first = ctx.client.post("/api/auth/login", json={"username": "demo", "password": "demo123"})
    token_a = first.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}
    _, video = _video(ctx, tmp_path, headers_a)
    url = f"/api/videos/{video['id']}/stream-ticket"
    issued_a = ctx.client.post(url, headers=headers_a)
    assert issued_a.status_code == 200

    second = ctx.client.post("/api/auth/login", json={"username": "demo", "password": "demo123"})
    token_b = second.json()["access_token"]
    binding_b = [
        header.split(";", 1)[0].split("=", 1)[1]
        for header in _set_cookie_headers(second)
        if header.startswith(settings.media_binding_cookie_name + "=")
    ][-1]

    rejected = _raw_ticket_request(
        ctx, url, token_a, [f"{settings.media_binding_cookie_name}={binding_b}"],
    )
    assert rejected.status_code == 401
    assert _set_cookie_headers(rejected) == []

    renewed = _raw_ticket_request(
        ctx, url, token_b, [f"{settings.media_binding_cookie_name}={binding_b}"],
    )
    assert renewed.status_code == 200


def test_stream_ticket_rejects_bearer_when_existing_binding_belongs_to_another_user(ctx, tmp_path):
    settings = _enable(ctx)
    login_a = ctx.client.post("/api/auth/login", json={"username": "demo", "password": "demo123"})
    token_a = login_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}
    project, video = _video(ctx, tmp_path, headers_a)

    user_b_id = ctx.create_user("binding-user")
    ctx.add_member(project["id"], user_b_id)
    login_b = ctx.client.post(
        "/api/auth/login", json={"username": "binding-user", "password": "pw123"},
    )
    token_b = login_b.json()["access_token"]
    binding_b = [
        header.split(";", 1)[0].split("=", 1)[1]
        for header in _set_cookie_headers(login_b)
        if header.startswith(settings.media_binding_cookie_name + "=")
    ][-1]
    url = f"/api/videos/{video['id']}/stream-ticket"

    rejected = _raw_ticket_request(
        ctx, url, token_a, [f"{settings.media_binding_cookie_name}={binding_b}"],
    )
    assert rejected.status_code == 401
    assert _set_cookie_headers(rejected) == []

    accepted = _raw_ticket_request(
        ctx, url, token_b, [f"{settings.media_binding_cookie_name}={binding_b}"],
    )
    assert accepted.status_code == 200


@pytest.mark.parametrize("cookie_fields", [
    ["mouse_media_binding"],
    ["mouse_media_binding="],
    ["mouse_media_binding=not-a-jwt"],
    ["mouse_media_binding=one; mouse_media_binding=two"],
    ["mouse_media_binding=one", "mouse_media_binding=two"],
])
def test_stream_ticket_binding_cookie_is_fail_closed_and_never_overwritten(ctx, tmp_path, cookie_fields):
    settings = _enable(ctx)
    login = ctx.client.post("/api/auth/login", json={"username": "demo", "password": "demo123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    _, video = _video(ctx, tmp_path, headers)
    response = _raw_ticket_request(
        ctx, f"/api/videos/{video['id']}/stream-ticket", token, cookie_fields,
    )
    assert response.status_code == 401
    assert _set_cookie_headers(response) == []


@pytest.mark.parametrize("fault", [
    "algorithm", "aud", "typ", "future_iat", "missing_sub", "missing_iat",
    "missing_exp", "missing_binding", "expired",
])
def test_stream_ticket_rejects_invalid_existing_binding_claims(ctx, tmp_path, fault):
    settings = _enable(ctx)
    login = ctx.client.post("/api/auth/login", json={"username": "demo", "password": "demo123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    _, video = _video(ctx, tmp_path, headers)
    keys = MediaKeys.from_settings(settings)
    now = int(time.time())
    payload = {
        "sub": "1", "binding": bearer_binding(token, keys.raw_bearer),
        "aud": settings.media_binding_audience, "typ": settings.media_binding_type,
        "iat": now, "exp": now + 300,
    }
    algorithm = "HS256"
    if fault == "algorithm":
        algorithm = "HS384"
    elif fault == "aud":
        payload["aud"] = settings.media_ticket_audience
    elif fault == "typ":
        payload["typ"] = settings.media_ticket_type
    elif fault == "future_iat":
        payload["iat"] = now + 3600
    elif fault == "expired":
        payload["iat"] = now - 301
        payload["exp"] = now - 1
    elif fault.startswith("missing_"):
        del payload[fault.removeprefix("missing_")]
    if fault == "algorithm":
        # 正确 binding key 只有 32 bytes；刻意改用 HS384 会触发 PyJWT 的长度建议，
        # 该 warning 与“decoder 必须拒绝非 HS256”这一安全断言相互独立。
        with pytest.warns(jwt.warnings.InsecureKeyLengthWarning):
            forged = jwt.encode(payload, keys.binding_jwt, algorithm=algorithm)
    else:
        forged = jwt.encode(payload, keys.binding_jwt, algorithm=algorithm)
    response = _raw_ticket_request(
        ctx, f"/api/videos/{video['id']}/stream-ticket", token,
        [f"{settings.media_binding_cookie_name}={forged}"],
    )
    assert response.status_code == 401
    assert _set_cookie_headers(response) == []


@pytest.mark.parametrize("remaining", [7201, 7199, 7200])
def test_ticket_exp_and_max_age_are_capped_by_ttl_and_bearer(ctx, tmp_path, monkeypatch, remaining):
    import app.routers.videos as videos_router

    settings = _enable(ctx)
    now = int(time.time())
    token = jwt.encode(
        {"sub": "1", "iat": now, "exp": now + remaining, "jti": f"remaining-{remaining}"},
        settings.secret_key, algorithm="HS256",
    )
    headers = {"Authorization": f"Bearer {token}"}
    ctx.raw_client.cookies.clear()
    _, video = _video(ctx, tmp_path, headers)
    monkeypatch.setattr(videos_router, "time", lambda: now)
    response = ctx.client.post(f"/api/videos/{video['id']}/stream-ticket", headers=headers)
    assert response.status_code == 200
    ticket = _cookie_value(response, settings.media_ticket_cookie_name)
    claims = jwt.decode(
        ticket, MediaKeys.from_settings(settings).ticket,
        algorithms=["HS256"], audience=settings.media_ticket_audience,
    )
    expected = min(remaining, 7200)
    assert claims["exp"] == now + expected
    header = next(
        value for value in _set_cookie_headers(response)
        if value.startswith(settings.media_ticket_cookie_name + "=")
    )
    assert f"Max-Age={expected}" in header


def test_ticket_response_cookie_contract_and_range_head(ctx, tmp_path):
    settings, _token, video, issued = _ticket_pair(ctx, tmp_path)
    assert set(issued.json()) == {"url", "expires_at"}
    assert issued.json()["url"] == f"/api/videos/{video['id']}/stream"
    ticket = _cookie_value(issued, settings.media_ticket_cookie_name)
    binding = _cookie_value(issued, settings.media_binding_cookie_name)
    set_cookies = issued.headers.get_list("set-cookie")
    assert any(f"Path=/api/videos/{video['id']}/stream" in item for item in set_cookies)
    assert any("Path=/api/videos/" in item for item in set_cookies)
    assert all("HttpOnly" in item and "Secure" in item and "SameSite=strict" in item for item in set_cookies)
    assert all("Domain=" not in item for item in set_cookies)
    cookies = {"Cookie": f"{settings.media_ticket_cookie_name}={ticket}; {settings.media_binding_cookie_name}={binding}"}
    url = f"/api/videos/{video['id']}/stream"
    full = ctx.client.get(url, headers=cookies)
    assert full.status_code == 200 and full.content == b"0123456789abcdef"
    assert full.headers["cache-control"] == "private, no-store"
    assert full.headers["accept-ranges"] == "bytes"
    partial = ctx.client.get(url, headers=cookies | {"Range": "bytes=2-5"})
    assert partial.status_code == 206 and partial.content == b"2345"
    assert partial.headers["content-range"] == "bytes 2-5/16"
    malformed = ctx.client.get(url, headers=cookies | {"Range": "bytes=5-2"})
    assert malformed.status_code == 400
    unsatisfied = ctx.client.get(url, headers=cookies | {"Range": "bytes=99-100"})
    assert unsatisfied.status_code == 416
    multi = ctx.client.get(url, headers=cookies | {"Range": "bytes=0-0,2-3"})
    assert multi.status_code == 206 and "multipart/byteranges" in multi.headers["content-type"]
    head = ctx.client.head(url, headers=cookies | {"Range": "bytes=0-3"})
    assert head.status_code == 206 and head.content == b"" and head.headers["content-length"] == "4"
    validator = full.headers["etag"]
    assert ctx.client.get(url, headers=cookies | {"Range": "bytes=0-1", "If-Range": validator}).status_code == 206
    assert ctx.client.get(url, headers=cookies | {"Range": "bytes=0-1", "If-Range": '"wrong"'}).status_code == 200

    keys = MediaKeys.from_settings(settings)
    ticket_claims = jwt.decode(
        ticket, keys.ticket, algorithms=["HS256"], audience=settings.media_ticket_audience,
    )
    binding_claims = jwt.decode(
        binding, keys.binding_jwt, algorithms=["HS256"], audience=settings.media_binding_audience,
    )
    assert ticket_claims["typ"] == settings.media_ticket_type
    assert binding_claims["typ"] == settings.media_binding_type
    assert ticket_claims["exp"] <= ticket_claims["iat"] + 7200
    assert ticket_claims["binding"] == binding_claims["binding"]


def test_cookie_presence_is_fail_closed_even_with_valid_bearer(ctx, tmp_path):
    settings, token, video, issued = _ticket_pair(ctx, tmp_path)
    ticket = _cookie_value(issued, settings.media_ticket_cookie_name)
    binding = _cookie_value(issued, settings.media_binding_cookie_name)
    url = f"/api/videos/{video['id']}/stream"
    bearer = f"Bearer {token}"
    cases = [
        settings.media_ticket_cookie_name,
        settings.media_binding_cookie_name,
        f"{settings.media_ticket_cookie_name}={ticket}",
        f"{settings.media_binding_cookie_name}={binding}",
        f"{settings.media_ticket_cookie_name}=bad; {settings.media_binding_cookie_name}={binding}",
        f"{settings.media_ticket_cookie_name}={ticket}; {settings.media_ticket_cookie_name}={ticket}; {settings.media_binding_cookie_name}={binding}",
    ]
    for cookie in cases:
        response = ctx.client.get(url, headers={"Authorization": bearer, "Cookie": cookie})
        assert response.status_code == 401
    settings.media_ticket_enabled = False
    residual = ctx.client.get(
        url, headers={"Authorization": bearer, "Cookie": f"{settings.media_ticket_cookie_name}={ticket}; {settings.media_binding_cookie_name}={binding}"},
    )
    assert residual.status_code == 401


def test_ticket_is_path_bound_and_rechecks_membership(ctx, tmp_path):
    from app.models import ProjectMembership, Video

    settings, _token, video, issued = _ticket_pair(ctx, tmp_path)
    ticket = _cookie_value(issued, settings.media_ticket_cookie_name)
    binding = _cookie_value(issued, settings.media_binding_cookie_name)
    cookie = {"Cookie": f"{settings.media_ticket_cookie_name}={ticket}; {settings.media_binding_cookie_name}={binding}"}
    assert ctx.client.get(f"/api/videos/{video['id'] + 999}/stream", headers=cookie).status_code == 401
    with ctx.session_factory() as db:
        stored = db.get(Video, video["id"])
        membership = db.query(ProjectMembership).filter_by(project_id=stored.project_id, user_id=1).one()
        membership.status = "inactive"
        db.commit()
    assert ctx.client.get(f"/api/videos/{video['id']}/stream", headers=cookie).status_code == 403


def test_expired_ticket_and_binding_are_rejected_together(ctx, tmp_path, login_headers):
    settings = _enable(ctx)
    headers = login_headers()
    _, video = _video(ctx, tmp_path, headers)
    keys = MediaKeys.from_settings(settings)
    now = int(time.time())
    common = {"sub": "1", "binding": "expired-binding", "iat": now - 20, "exp": now - 10}
    ticket = encode_media_jwt(common | {
        "video_id": video["id"], "aud": settings.media_ticket_audience,
        "typ": settings.media_ticket_type,
    }, keys.ticket)
    binding = encode_media_jwt(common | {
        "aud": settings.media_binding_audience, "typ": settings.media_binding_type,
    }, keys.binding_jwt)
    response = ctx.client.get(
        f"/api/videos/{video['id']}/stream",
        headers={"Cookie": (
            f"{settings.media_ticket_cookie_name}={ticket}; "
            f"{settings.media_binding_cookie_name}={binding}"
        )},
    )
    assert response.status_code == 401


def test_old_and_new_media_cookie_halves_cannot_be_mixed(ctx, tmp_path):
    settings, _token_a, video, issued_a = _ticket_pair(ctx, tmp_path)
    ticket_a = _cookie_value(issued_a, settings.media_ticket_cookie_name)
    binding_a = _cookie_value(issued_a, settings.media_binding_cookie_name)
    login_b = ctx.client.post("/api/auth/login", json={"username": "demo", "password": "demo123"})
    token_b = login_b.json()["access_token"]
    issued_b = ctx.client.post(
        f"/api/videos/{video['id']}/stream-ticket",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert issued_b.status_code == 200
    ticket_b = _cookie_value(issued_b, settings.media_ticket_cookie_name)
    binding_b = _cookie_value(issued_b, settings.media_binding_cookie_name)
    url = f"/api/videos/{video['id']}/stream"
    for ticket, binding in ((ticket_a, binding_b), (ticket_b, binding_a)):
        response = ctx.client.get(url, headers={"Cookie": (
            f"{settings.media_ticket_cookie_name}={ticket}; "
            f"{settings.media_binding_cookie_name}={binding}"
        )})
        assert response.status_code == 401


def test_new_stream_after_completed_same_filesystem_isolation_gets_helper_404(ctx, tmp_path, login_headers):
    """仅锁定新请求边界：先完成隔离后，授权 helper 在构造 FileResponse 前返回 404。"""
    import os

    headers = login_headers()
    _, video = _video(ctx, tmp_path, headers)
    source = tmp_path / "videos" / "ticket.mp4"
    quarantine = tmp_path / "videos" / ".delete-quarantine"
    os.replace(source, quarantine)
    response = ctx.client.get(f"/api/videos/{video['id']}/stream", headers=headers)
    assert response.status_code == 404


@pytest.mark.skipif(__import__("os").name != "nt", reason="records only the local Windows os.replace contract")
def test_windows_open_handle_prevents_current_replace_isolation(tmp_path):
    """Windows 本地候选实测：已打开普通 Python handle 时 replace 被拒绝，不虚构 POSIX 行为。"""
    import os

    source = tmp_path / "open.mp4"
    target = tmp_path / "isolated.mp4"
    source.write_bytes(b"open-response-bytes")
    with source.open("rb") as opened:
        with pytest.raises(PermissionError):
            os.replace(source, target)
        assert opened.read() == b"open-response-bytes"
    assert source.exists() and not target.exists()


def test_raw_cookie_parser_preserves_cross_header_duplicates():
    scope = {"headers": [
        (b"cookie", b"mouse_media_ticket=one; other=x; malformed"),
        (b"cookie", b"mouse_media_binding=bind; mouse_media_ticket; mouse_media_ticket=two"),
    ]}
    values = raw_cookie_values(scope, {"mouse_media_ticket", "mouse_media_binding"})
    assert values == {"mouse_media_ticket": ["one", "", "two"], "mouse_media_binding": ["bind"]}


def test_legacy_only_when_both_media_cookie_names_absent(ctx, tmp_path, login_headers):
    headers = login_headers()
    _, video = _video(ctx, tmp_path, headers)
    url = f"/api/videos/{video['id']}/stream"
    assert ctx.client.get(url, headers=headers).status_code == 200
    settings = ctx.raw_client.app.state.settings
    settings.media_legacy_bearer_enabled = False
    assert ctx.client.get(url, headers=headers).status_code == 401
