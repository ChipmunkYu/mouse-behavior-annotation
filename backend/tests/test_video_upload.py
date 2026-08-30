"""批次 2 验收：真实视频流式上传 `POST /api/projects/{project_id}/videos/upload`。

覆盖：权限/跨项目、扩展名校验（大小写不敏感）、空文件、同名不覆盖、分块流式写入、
磁盘不足（首检与写入中）507、写入异常清理、DB 提交失败清理、上传后可流式读取且路径安全、
Content-Type 仅辅助、无固定大小限制配置、JSON Mock 接口并存。
"""
from __future__ import annotations

import collections
import sqlite3
from pathlib import Path

import pytest
import starlette.datastructures as sd
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.models import ProjectMembership
from app.routers import videos as videos_module

DU = collections.namedtuple("DiskUsage", "total used free")


def _upload(client, pid, headers, name="clip.mp4", data=b"x", content_type="video/mp4"):
    return client.post(
        f"/api/projects/{pid}/videos/upload",
        files={"file": (name, data, content_type)},
        headers=headers,
    )


def _make_project(ctx, login_headers, name: str = "上传项目") -> int:
    resp = ctx.client.post("/api/projects", json={"name": name}, headers=login_headers())
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _videos_dir(ctx) -> Path:
    return ctx.client.app.state.settings.videos_dir.resolve()


def _files_in(dirpath: Path) -> list[str]:
    return sorted(p.name for p in dirpath.iterdir() if p.is_file())


def _video_rows(ctx) -> list:
    with ctx.session_factory() as db:
        return db.query(models.Video).all()


def _storage_path(ctx, video_id: int) -> str | None:
    with ctx.session_factory() as db:
        return db.get(models.Video, video_id).storage_path


# ---------- 基本上传 / 元数据 / 流式读取 ----------
def test_upload_mp4_sets_fields_and_streams(ctx, login_headers):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    data = b"FAKE-MP4-BYTES"
    resp = _upload(ctx.client, pid, headers, name="session1.mp4", data=data)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["project_id"] == pid
    assert body["filename"] == "session1.mp4"
    assert body["status"] == "uploaded"
    assert body["workflow_status"] == "draft"
    assert body["annotation_revision"] == 1
    assert "storage_path" not in body
    assert body["duration"] is None and body["fps"] is None  # 本批不运行 ffprobe

    with ctx.session_factory() as db:
        row = db.get(models.Video, body["id"])
        assert row is not None
        assert row.uploaded_by is not None  # uploaded_by = 当前用户
        storage_path = row.storage_path
        assert storage_path == Path(storage_path).name

    videos_dir = _videos_dir(ctx)
    final = videos_dir / storage_path
    assert final.is_file()
    assert final.read_bytes() == data
    assert final.resolve().is_relative_to(videos_dir)  # 路径限制在 videos_dir 内

    # stream 能读取上传文件且内容一致
    resp2 = ctx.client.get(f"/api/videos/{body['id']}/stream", headers=headers)
    assert resp2.status_code == 200
    assert resp2.content == data
    assert not list(videos_dir.glob("*.part"))  # 无 .part 残留


@pytest.mark.parametrize(
    ("name", "expected_status"),
    [
        ("clip.mp4", "uploaded"),
        ("clip.webm", "uploaded"),
        ("clip.mov", "uploaded"),
        ("clip.m4v", "uploaded"),
        ("clip.avi", "needs_transcode"),
        ("clip.mkv", "needs_transcode"),
        ("clip.wmv", "needs_transcode"),
        ("clip.mpeg", "needs_transcode"),
        ("clip.mpg", "needs_transcode"),
    ],
)
def test_upload_extension_status_mapping(ctx, login_headers, name, expected_status):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    resp = _upload(ctx.client, pid, headers, name=name, data=b"data")
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == expected_status
    assert resp.json()["playback_status"] == (
        "ready" if expected_status == "uploaded" else "unavailable"
    )


def test_upload_extension_case_insensitive(ctx, login_headers):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    resp = _upload(ctx.client, pid, headers, name="CLIP.MP4", data=b"data")
    assert resp.status_code == 201
    assert resp.json()["status"] == "uploaded"
    resp = _upload(ctx.client, pid, headers, name="cage.AVI", data=b"data")
    assert resp.status_code == 201
    assert resp.json()["status"] == "needs_transcode"
    assert Path(_storage_path(ctx, resp.json()["id"])).suffix == ".avi"  # 磁盘目标扩展名小写


def test_upload_chinese_filename_preserved(ctx, login_headers):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    name = "小鼠 攻击行为 第1段.MP4"
    resp = _upload(ctx.client, pid, headers, name=name, data=b"data")
    assert resp.status_code == 201, resp.text
    assert resp.json()["filename"] == name


# ---------- 扩展名校验 ----------
def test_upload_rejects_blank_filename(ctx, login_headers):
    """空/纯空白文件名被拒绝（空白名经清理后无效）。"""
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    # 空字符串文件名在框架 multipart 解析层即被丢弃 → 422（file 字段缺失）
    resp = ctx.client.post(
        f"/api/projects/{pid}/videos/upload",
        files={"file": ("", b"data", "video/mp4")},
        headers=headers,
    )
    assert resp.status_code == 422
    # 纯空白文件名到达应用层后按“文件名无效”拒绝 → 400
    resp = _upload(ctx.client, pid, headers, name="   ", data=b"data")
    assert resp.status_code == 400
    assert resp.json()["detail"] == videos_module.ERR_FILENAME_INVALID
    assert _files_in(_videos_dir(ctx)) == []
    assert _video_rows(ctx) == []


def test_upload_rejects_no_extension(ctx, login_headers):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    resp = _upload(ctx.client, pid, headers, name="movie", data=b"xx")
    assert resp.status_code == 400
    assert resp.json()["detail"] == videos_module.ERR_EXTENSION_NOT_ALLOWED


def test_upload_rejects_disallowed_extension(ctx, login_headers):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    for bad in ("evil.exe", "notes.txt", "archive.tar.gz"):
        resp = _upload(ctx.client, pid, headers, name=bad, data=b"x")
        assert resp.status_code == 400, bad
        assert resp.json()["detail"] == videos_module.ERR_EXTENSION_NOT_ALLOWED
    assert _files_in(_videos_dir(ctx)) == []
    assert _video_rows(ctx) == []


# ---------- 空文件 ----------
def test_upload_empty_file_rejected(ctx, login_headers):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    resp = _upload(ctx.client, pid, headers, name="empty.mp4", data=b"")
    assert resp.status_code == 400
    assert resp.json()["detail"] == videos_module.ERR_EMPTY_FILE
    assert _files_in(_videos_dir(ctx)) == []
    assert _video_rows(ctx) == []


# ---------- 路径穿越文件名安全化 ----------
@pytest.mark.parametrize("evil", ["../../evil.mp4", "..\\..\\evil.mp4", "dir/../evil.mp4"])
def test_upload_sanitizes_traversal_filename(ctx, login_headers, evil):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    resp = _upload(ctx.client, pid, headers, name=evil, data=b"data")
    assert resp.status_code == 201, evil
    body = resp.json()
    assert body["filename"] == "evil.mp4"
    storage_path = _storage_path(ctx, body["id"])
    assert storage_path == Path(storage_path).name
    target = _videos_dir(ctx) / storage_path
    assert target.is_file()
    assert target.resolve().is_relative_to(_videos_dir(ctx))


# ---------- 同名不覆盖 ----------
def test_upload_same_name_does_not_overwrite(ctx, login_headers):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    r1 = _upload(ctx.client, pid, headers, name="same.mp4", data=b"ONE")
    r2 = _upload(ctx.client, pid, headers, name="same.mp4", data=b"TWO")
    assert r1.status_code == 201 and r2.status_code == 201
    p1, p2 = _storage_path(ctx, r1.json()["id"]), _storage_path(ctx, r2.json()["id"])
    assert p1 != p2
    videos_dir = _videos_dir(ctx)
    assert (videos_dir / p1).read_bytes() == b"ONE"
    assert (videos_dir / p2).read_bytes() == b"TWO"
    assert len(_video_rows(ctx)) == 2


# ---------- 权限 / 跨项目 ----------
def test_upload_requires_auth(ctx, login_headers):
    pid = _make_project(ctx, login_headers)
    resp = ctx.client.post(
        f"/api/projects/{pid}/videos/upload",
        files={"file": ("a.mp4", b"data", "video/mp4")},
    )
    assert resp.status_code == 401


def test_upload_non_member_rejected(ctx, login_headers):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    ctx.create_user("alice")
    alice_headers = login_headers(username="alice", password="pw123")
    resp = _upload(ctx.client, pid, alice_headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "You are not a member of this project"
    assert _files_in(_videos_dir(ctx)) == []
    assert _video_rows(ctx) == []


def test_upload_unknown_project_404(ctx, login_headers):
    headers = login_headers()
    resp = _upload(ctx.client, 999999, headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Project not found"


def test_upload_cross_project_rejected(ctx, login_headers):
    headers = login_headers()
    pid1 = _make_project(ctx, login_headers, "项目A")
    pid2 = _make_project(ctx, login_headers, "项目B")
    alice_id = ctx.create_user("alice")
    alice_headers = login_headers(username="alice", password="pw123")
    ctx.add_member(pid1, alice_id)  # alice 仅属于项目 A

    resp = _upload(ctx.client, pid2, alice_headers)  # 跨项目 → 403
    assert resp.status_code == 403

    resp = _upload(ctx.client, pid1, alice_headers, name="alice.mp4", data=b"from-alice")
    assert resp.status_code == 201
    with ctx.session_factory() as db:
        row = db.get(models.Video, resp.json()["id"])
        assert row.uploaded_by == alice_id


def test_upload_inactive_membership_rejected(ctx, login_headers):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    alice_id = ctx.create_user("alice")
    alice_headers = login_headers(username="alice", password="pw123")
    with ctx.session_factory() as db:
        db.add(
            ProjectMembership(project_id=pid, user_id=alice_id, role="member", status="inactive")
        )
        db.commit()
    resp = _upload(ctx.client, pid, alice_headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == videos_module.ERR_MEMBERSHIP_INACTIVE
    assert _files_in(_videos_dir(ctx)) == []
    assert _video_rows(ctx) == []


# ---------- 分块流式写入（可观测） ----------
def test_upload_streams_in_chunks_and_checks_disk_per_chunk(ctx, login_headers, monkeypatch):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    ctx.client.app.state.settings.upload_chunk_size = 4  # 每块 4 字节 → 强制多块
    data = b"chunked-upload-" * 30
    calls = []
    real = videos_module.shutil.disk_usage

    def fake(path):
        calls.append(Path(path))
        return real(path)

    monkeypatch.setattr(videos_module.shutil, "disk_usage", fake)
    resp = _upload(ctx.client, pid, headers, name="big.mp4", data=data)
    assert resp.status_code == 201, resp.text
    # 分块写入 → 磁盘检查多次（开写前首检 + 每块写入前）
    assert len(calls) >= 2
    assert all(str(c).endswith("videos") for c in calls)
    target = _videos_dir(ctx) / _storage_path(ctx, resp.json()["id"])
    assert target.read_bytes() == data
    assert not list(_videos_dir(ctx).glob("*.part"))


# ---------- 磁盘不足 → 507 ----------
def test_upload_disk_full_returns_507(ctx, login_headers, monkeypatch):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    monkeypatch.setattr(
        videos_module.shutil, "disk_usage", lambda p: DU(total=100, used=100, free=0)
    )
    resp = _upload(ctx.client, pid, headers, name="full.mp4", data=b"data")
    assert resp.status_code == 507
    assert resp.json()["detail"] == videos_module.ERR_DISK_SPACE
    assert _files_in(_videos_dir(ctx)) == []
    assert _video_rows(ctx) == []


def test_upload_disk_full_mid_stream_cleans_temp(ctx, login_headers, monkeypatch):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    ctx.client.app.state.settings.upload_chunk_size = 3
    state = {"n": 0}

    def fake(p):
        state["n"] += 1
        if state["n"] == 1:  # 开写前有空间
            return DU(total=10**12, used=0, free=10**12)
        return DU(total=100, used=100, free=0)  # 写入过程中耗尽

    monkeypatch.setattr(videos_module.shutil, "disk_usage", fake)
    resp = _upload(ctx.client, pid, headers, name="mid.mp4", data=b"123456789")
    assert resp.status_code == 507
    assert state["n"] >= 2  # 确实发生了“写入中”的磁盘检查
    assert not list(_videos_dir(ctx).glob("*.part"))
    assert _files_in(_videos_dir(ctx)) == []
    assert _video_rows(ctx) == []


# ---------- 写入异常清理 ----------
def test_upload_write_error_cleans_temp(ctx, login_headers, monkeypatch):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)

    async def broken_read(self, size=-1):
        raise RuntimeError("client aborted mid-upload")

    monkeypatch.setattr(sd.UploadFile, "read", broken_read)
    with pytest.raises(RuntimeError):
        _upload(ctx.client, pid, headers, name="abort.mp4", data=b"some-bytes")
    assert not list(_videos_dir(ctx).glob("*.part"))
    assert _files_in(_videos_dir(ctx)) == []
    assert _video_rows(ctx) == []


# ---------- DB 提交失败清理 ----------
def test_upload_db_failure_cleans_orphan(ctx, login_headers, monkeypatch):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)

    def boom(self):
        raise RuntimeError("db down")

    monkeypatch.setattr(Session, "commit", boom)
    resp = _upload(ctx.client, pid, headers, name="db.mp4", data=b"bytes")
    assert resp.status_code == 500
    assert resp.json()["detail"] == videos_module.ERR_DB_SAVE
    assert not list(_videos_dir(ctx).glob("*.part"))
    assert _files_in(_videos_dir(ctx)) == []  # 最终孤儿已清理
    assert _video_rows(ctx) == []


def test_upload_assignee_race_returns_409_cleans_file_and_retries(ctx, login_headers, monkeypatch):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    alice_id = ctx.create_user("upload-race-alice")
    bob_id = ctx.create_user("upload-race-bob")
    ctx.add_member(pid, alice_id)
    ctx.add_member(pid, bob_id)
    with ctx.session_factory() as db:
        alice_mid = db.query(ProjectMembership.id).filter_by(project_id=pid, user_id=alice_id).scalar()
        bob_mid = db.query(ProjectMembership.id).filter_by(project_id=pid, user_id=bob_id).scalar()

    original_commit = Session.commit
    fail_once = True

    def race_commit(session):
        nonlocal fail_once
        if fail_once and any(
            isinstance(item, models.Video) and item.assignee_membership_id == alice_mid
            for item in session.new
        ):
            fail_once = False
            raise IntegrityError(
                "video assignee write", {},
                sqlite3.IntegrityError("assignee must be an active membership in the video project"),
            )
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", race_commit)
    failed = ctx.client.post(
        f"/api/projects/{pid}/videos/upload",
        files={"file": ("race.mp4", b"race", "video/mp4")},
        data={"assignee_membership_id": str(alice_mid)}, headers=headers,
    )
    assert failed.status_code == 409
    assert failed.json()["detail"] == videos_module.ASSIGNEE_CONFLICT_DETAIL
    assert _files_in(_videos_dir(ctx)) == []
    retried = ctx.client.post(
        f"/api/projects/{pid}/videos/upload",
        files={"file": ("retry.mp4", b"retry", "video/mp4")},
        data={"assignee_membership_id": str(bob_mid)}, headers=headers,
    )
    assert retried.status_code == 201
    assert retried.json()["assignee_membership_id"] == bob_mid


# ---------- Content-Type 仅辅助 ----------
def test_upload_content_type_does_not_override_extension(ctx, login_headers):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    # 文件名合法、Content-Type 误导 → 仍按扩展名接受
    resp = _upload(ctx.client, pid, headers, name="real.mp4", data=b"data", content_type="text/plain")
    assert resp.status_code == 201
    assert resp.json()["status"] == "uploaded"
    # 文件名不合法、Content-Type 是 video/mp4 → 仍拒绝
    resp = _upload(ctx.client, pid, headers, name="fake.exe", data=b"data", content_type="video/mp4")
    assert resp.status_code == 400
    assert resp.json()["detail"] == videos_module.ERR_EXTENSION_NOT_ALLOWED


# ---------- 无固定大小限制配置 ----------
def test_upload_has_no_fixed_size_limit_config(ctx):
    app = ctx.client.app
    op = app.openapi()["paths"]["/api/projects/{project_id}/videos/upload"]["post"]
    body_schema = op["requestBody"]["content"]["multipart/form-data"]["schema"]
    ref = body_schema.get("$ref", "")
    if ref:
        schema_name = ref.rsplit("/", 1)[-1]
        props = app.openapi()["components"]["schemas"][schema_name]["properties"]
    else:
        props = body_schema.get("properties", {})
    assert "maxUploadSize" not in props["file"]
    assert "maxLength" not in props["file"]
    settings = app.state.settings
    assert getattr(settings, "max_upload_size", None) is None
    assert getattr(settings, "max_upload_bytes", None) is None


# ---------- 缺 file 字段 / Mock 接口并存 ----------
def test_upload_missing_file_field_422(ctx, login_headers):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    resp = ctx.client.post(f"/api/projects/{pid}/videos/upload", headers=headers)
    assert resp.status_code == 422


def test_mock_json_endpoint_still_works(ctx, login_headers):
    headers = login_headers()
    pid = _make_project(ctx, login_headers)
    resp = ctx.client.post(
        f"/api/projects/{pid}/videos",
        json={"filename": "mock.mp4", "status": "metadata"},
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "metadata"
    assert "storage_path" not in resp.json()
    assert _storage_path(ctx, resp.json()["id"]) is None
