"""验收：视频元数据创建 / 列表 / 流。"""
from __future__ import annotations

from app.models import ProjectMembership, Video
from app.routers import videos as videos_module


def test_create_and_list_video(ctx, login_headers):
    headers = login_headers()
    project = ctx.client.post(
        "/api/projects", json={"name": "视频项目"}, headers=headers
    ).json()
    pid = project["id"]

    resp = ctx.client.post(
        f"/api/projects/{pid}/videos",
        json={
            "filename": "cage1.mp4",
            "duration": 300.5,
            "fps": 30.0,
            "width": 1920,
            "height": 1080,
            "status": "metadata",
        },
        headers=headers,
    )
    assert resp.status_code == 201
    video = resp.json()
    assert video["filename"] == "cage1.mp4"
    assert video["duration"] == 300.5
    assert video["fps"] == 30.0
    assert video["width"] == 1920
    assert video["height"] == 1080

    items = ctx.client.get(f"/api/projects/{pid}/videos", headers=headers).json()
    assert len(items) == 1
    assert items[0]["id"] == video["id"]

    # 非成员不可见
    alice_id = ctx.create_user("alice")
    alice_headers = login_headers(username="alice", password="pw123")
    assert (
        ctx.client.get(f"/api/projects/{pid}/videos", headers=alice_headers).status_code == 403
    )


def test_unassigned_view_only_lists_drafts_and_combines_workflow_filter(ctx, login_headers):
    headers = login_headers()
    project = ctx.client.post(
        "/api/projects", json={"name": "待领取筛选"}, headers=headers
    ).json()
    videos = [
        ctx.client.post(
            f"/api/projects/{project['id']}/videos",
            json={"filename": f"{status}.mp4"},
            headers=headers,
        ).json()
        for status in ("draft", "submitted", "approved", "rejected")
    ]
    with ctx.session_factory() as db:
        for video, status in zip(videos, ("draft", "submitted", "approved", "rejected")):
            db.get(Video, video["id"]).workflow_status = status
        db.commit()

    unassigned = ctx.client.get(
        f"/api/projects/{project['id']}/videos?view=unassigned", headers=headers
    ).json()
    assert [item["workflow_status"] for item in unassigned] == ["draft"]

    draft = ctx.client.get(
        f"/api/projects/{project['id']}/videos?view=unassigned&workflow_status=draft",
        headers=headers,
    ).json()
    submitted = ctx.client.get(
        f"/api/projects/{project['id']}/videos?view=unassigned&workflow_status=submitted",
        headers=headers,
    ).json()
    assert [item["id"] for item in draft] == [videos[0]["id"]]
    assert submitted == []


def test_stream_404_without_storage_path(ctx, login_headers):
    headers = login_headers()
    project = ctx.client.post(
        "/api/projects", json={"name": "流项目"}, headers=headers
    ).json()
    video = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "nofile.mp4"},
        headers=headers,
    ).json()
    resp = ctx.client.get(f"/api/videos/{video['id']}/stream", headers=headers)
    assert resp.status_code == 404


def test_stream_serves_file(ctx, tmp_path, login_headers):
    headers = login_headers()
    project = ctx.client.post(
        "/api/projects", json={"name": "流项目2"}, headers=headers
    ).json()

    # 文件必须位于配置的视频目录（data_dir/videos）内
    video_file = tmp_path / "videos" / "clip.mp4"
    video_file.parent.mkdir(parents=True, exist_ok=True)
    video_file.write_bytes(b"FAKE-VIDEO-BYTES")
    video = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "clip.mp4", "storage_path": str(video_file)},
        headers=headers,
    ).json()

    resp = ctx.client.get(f"/api/videos/{video['id']}/stream", headers=headers)
    assert resp.status_code == 200
    assert resp.content == b"FAKE-VIDEO-BYTES"


def test_stream_relative_path_within_videos_dir(ctx, tmp_path, login_headers):
    """相对路径按 data/videos/ 解析，且不允许逃逸到视频目录之外。"""
    headers = login_headers()
    project = ctx.client.post(
        "/api/projects", json={"name": "流项目4"}, headers=headers
    ).json()

    video_file = tmp_path / "videos" / "relclip.mp4"
    video_file.parent.mkdir(parents=True, exist_ok=True)
    video_file.write_bytes(b"REL-BYTES")
    video = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "relclip.mp4", "storage_path": "relclip.mp4"},
        headers=headers,
    ).json()

    resp = ctx.client.get(f"/api/videos/{video['id']}/stream", headers=headers)
    assert resp.status_code == 200
    assert resp.content == b"REL-BYTES"


def test_stream_outside_videos_dir_rejected(ctx, tmp_path, login_headers):
    """安全边界：绝对路径指向视频目录外的文件 → 404，禁止任意文件读取。"""
    headers = login_headers()
    project = ctx.client.post(
        "/api/projects", json={"name": "流项目5"}, headers=headers
    ).json()

    sensitive = tmp_path / "sensitive.txt"  # 在 videos_dir 之外
    sensitive.write_text("secret")
    video = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "evil.mp4", "storage_path": str(sensitive)},
        headers=headers,
    ).json()

    resp = ctx.client.get(f"/api/videos/{video['id']}/stream", headers=headers)
    assert resp.status_code == 404


def test_stream_traversal_rejected(ctx, tmp_path, login_headers):
    """安全边界：../ 路径穿越不允许逃出视频目录。"""
    headers = login_headers()
    project = ctx.client.post(
        "/api/projects", json={"name": "流项目6"}, headers=headers
    ).json()

    sensitive = tmp_path / "sensitive2.txt"  # data_dir 根下，视频目录之外
    sensitive.write_text("secret")
    video = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "evil.mp4", "storage_path": "../sensitive2.txt"},
        headers=headers,
    ).json()

    resp = ctx.client.get(f"/api/videos/{video['id']}/stream", headers=headers)
    assert resp.status_code == 404


def test_stream_storage_path_missing_on_disk(ctx, login_headers):
    headers = login_headers()
    project = ctx.client.post(
        "/api/projects", json={"name": "流项目3"}, headers=headers
    ).json()
    video = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "ghost.mp4", "storage_path": r"C:\does\not\exist\ghost.mp4"},
        headers=headers,
    ).json()
    resp = ctx.client.get(f"/api/videos/{video['id']}/stream", headers=headers)
    assert resp.status_code == 404


def test_stream_non_member_rejected(ctx, login_headers):
    headers = login_headers()
    project = ctx.client.post(
        "/api/projects", json={"name": "私有视频"}, headers=headers
    ).json()
    video = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "private.mp4"},
        headers=headers,
    ).json()

    alice_id = ctx.create_user("alice")
    alice_headers = login_headers(username="alice", password="pw123")
    resp = ctx.client.get(f"/api/videos/{video['id']}/stream", headers=alice_headers)
    assert resp.status_code == 403


def test_stream_active_member_allowed(ctx, tmp_path, login_headers):
    """active 成员（非 owner）可流式读取视频。"""
    headers = login_headers()
    project = ctx.client.post(
        "/api/projects", json={"name": "流项目-活动成员"}, headers=headers
    ).json()
    video_file = tmp_path / "videos" / "member.mp4"
    video_file.parent.mkdir(parents=True, exist_ok=True)
    video_file.write_bytes(b"MEMBER-BYTES")
    video = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "member.mp4", "storage_path": str(video_file)},
        headers=headers,
    ).json()

    alice_id = ctx.create_user("alice")
    alice_headers = login_headers(username="alice", password="pw123")
    ctx.add_member(project["id"], alice_id)  # 默认 status=active

    resp = ctx.client.get(f"/api/videos/{video['id']}/stream", headers=alice_headers)
    assert resp.status_code == 200
    assert resp.content == b"MEMBER-BYTES"


def test_stream_inactive_membership_rejected(ctx, tmp_path, login_headers):
    """成员存在但 status != active → 403，复用与 upload 一致的稳定文案。"""
    headers = login_headers()
    project = ctx.client.post(
        "/api/projects", json={"name": "流项目-停用成员"}, headers=headers
    ).json()
    video_file = tmp_path / "videos" / "private2.mp4"
    video_file.parent.mkdir(parents=True, exist_ok=True)
    video_file.write_bytes(b"SECRET-BYTES")
    video = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "private2.mp4", "storage_path": str(video_file)},
        headers=headers,
    ).json()

    alice_id = ctx.create_user("alice")
    alice_headers = login_headers(username="alice", password="pw123")
    with ctx.session_factory() as db:
        db.add(
            ProjectMembership(
                project_id=project["id"], user_id=alice_id, role="member", status="inactive"
            )
        )
        db.commit()

    resp = ctx.client.get(f"/api/videos/{video['id']}/stream", headers=alice_headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == videos_module.ERR_MEMBERSHIP_INACTIVE
