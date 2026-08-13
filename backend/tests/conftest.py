"""pytest 共享夹具与辅助函数。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
# 避免导入 app.main 时自动创建默认开发数据库
os.environ["ANNOTATION_BACKEND_SKIP_APP"] = "1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import database as db_mod  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.media import MediaCommandError  # noqa: E402
from app.models import ProjectMembership, User  # noqa: E402


def auth_headers(client, username: str = "demo", password: str = "demo123") -> dict:
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class AppContext:
    def __init__(self, client: TestClient, session_factory):
        self.client = client
        self.session_factory = session_factory

    def create_user(self, username: str, password: str = "pw123") -> int:
        with self.session_factory() as db:
            user = User(username=username, password_hash=hash_password(password))
            db.add(user)
            db.commit()
            return user.id

    def add_member(self, project_id: int, user_id: int, role: str = "annotator") -> None:
        with self.session_factory() as db:
            db.add(ProjectMembership(project_id=project_id, user_id=user_id, role=role))
            db.commit()

    def make_project_with_video(self, name: str = "标注测试项目") -> dict:
        headers = auth_headers(self.client)
        project = self.client.post(
            "/api/projects", json={"name": name, "description": "测试"}, headers=headers
        ).json()
        categories = self.client.get(
            f"/api/projects/{project['id']}/categories", headers=headers
        ).json()
        video = self.client.post(
            f"/api/projects/{project['id']}/videos",
            json={
                "filename": "session1.mp4",
                "duration": 120.0,
                "fps": 25.0,
                "width": 1280,
                "height": 720,
            },
            headers=headers,
        ).json()
        return {"headers": headers, "project": project, "categories": categories, "video": video}


@pytest.fixture()
def ctx(tmp_path):
    """每个测试独立的临时 SQLite 数据库 + 测试客户端。"""
    settings = Settings(
        env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        cleanup_enabled=False,
    )
    app = create_app(settings=settings)
    with TestClient(app) as client:
        # 必须在 create_app 之后取用：configure_engine 才会重新赋值 SessionLocal
        yield AppContext(client, db_mod.SessionLocal)


@pytest.fixture()
def client(ctx):
    return ctx.client


@pytest.fixture()
def login_headers(ctx):
    def _login(username: str = "demo", password: str = "demo123") -> dict:
        return auth_headers(ctx.client, username, password)

    return _login


class FakeMediaProcessor:
    """可替换媒体执行器（批次 4）：不调用真实 ffmpeg，写入伪文件并可注入失败。

    - `clip_calls` / `thumb_calls`：记录 (input_path, start, end, output_path) 调用。
    - `fail_clips` / `fail_thumbnails`：annotation_id 集合，命中即抛 MediaCommandError。
    """

    def __init__(self) -> None:
        self.clip_calls: list[tuple[str, float, float, str]] = []
        self.thumb_calls: list[tuple[str, float, str]] = []
        self.clip_crops: list[tuple | None] = []
        self.thumb_crops: list[tuple | None] = []
        self.fail_clips: set[int] = set()
        self.fail_thumbnails: set[int] = set()

    @staticmethod
    def _annotation_id(output_path: str) -> int:
        # 输出名形如 .clip_{annotation_id}_rev{n}.mp4.part
        name = Path(output_path).name
        start = name.index("clip_") + len("clip_")
        end = name.index("_rev", start)
        return int(name[start:end])

    def render_clip(self, *, input_path: str, start: float, end: float, output_path: str, crop=None) -> None:
        self.clip_calls.append((input_path, start, end, output_path))
        self.clip_crops.append(crop)
        ann_id = self._annotation_id(output_path)
        if ann_id in self.fail_clips:
            raise MediaCommandError(
                f"ffmpeg clip failed for annotation {ann_id}: fake-stderr-truncated"
            )
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"FAKE-MP4-DATA")

    def render_thumbnail(self, *, input_path: str, at: float, output_path: str, crop=None) -> None:
        self.thumb_calls.append((input_path, at, output_path))
        self.thumb_crops.append(crop)
        ann_id = self._annotation_id(output_path)
        if ann_id in self.fail_thumbnails:
            raise MediaCommandError(
                f"ffmpeg thumbnail failed for annotation {ann_id}: fake-stderr-truncated"
            )
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"FAKE-JPG-DATA")

    def probe_clip(self, path: str, *, expected: dict | None = None) -> dict:
        if expected is None:
            raise MediaCommandError("fake probe requires expected media properties")
        return {**expected, "duration": expected["frame_count"] / expected["fps"]}


class MediaAppContext(AppContext):
    def __init__(self, client, session_factory, processor: FakeMediaProcessor, app):
        super().__init__(client, session_factory)
        self.processor = processor
        self.app = app


@pytest.fixture()
def media_ctx(tmp_path):
    """批次 4 媒体测试：注入 FakeMediaProcessor + 同步单线程 worker（不要求系统 ffmpeg）。"""
    settings = Settings(
        env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'media.db').as_posix()}",
        media_synchronous=True,
        cleanup_enabled=False,
    )
    processor = FakeMediaProcessor()
    app = create_app(settings=settings, media_processor=processor)
    with TestClient(app) as client:
        yield MediaAppContext(client, db_mod.SessionLocal, processor, app)
