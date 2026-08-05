"""验收：seed_demo 幂等性（新建/复用、无重复数据、视频文件放置、Mock 元数据）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import database as db_mod
from app.config import Settings
from app.models import (
    Annotation,
    BehaviorCategory,
    Project,
    ProjectMembership,
    User,
    Video,
)
from scripts.seed_demo import (
    ANNOTATION_BEHAVIOR,
    PROJECT_NAME,
    VIDEO_NAME,
    seed_demo,
)

DEMO_SOURCE_BYTES = b"fake-video-bytes-for-demo-2026"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
    )


def _make_source(tmp_path: Path) -> Path:
    src = tmp_path / "source_attack.mov"
    src.write_bytes(DEMO_SOURCE_BYTES)
    return src


def test_seed_demo_idempotent(tmp_path):
    """连续运行两次：复用同一批记录，项目/视频/标注各只有一条。"""
    settings = _settings(tmp_path)
    src = _make_source(tmp_path)

    first = seed_demo(settings, video_source=src)
    second = seed_demo(settings, video_source=src)

    assert first["project_id"] == second["project_id"]
    assert first["video_id"] == second["video_id"]
    assert first["annotation_id"] == second["annotation_id"]
    assert second["project_created"] is False
    assert second["video_created"] is False
    assert second["annotation_created"] is False

    with db_mod.SessionLocal() as db:
        assert db.query(Project).filter(Project.name == PROJECT_NAME).count() == 1
        assert db.query(Video).filter(Video.filename == VIDEO_NAME).count() == 1

        video = db.query(Video).filter(Video.filename == VIDEO_NAME).one()
        assert video.status == "ready"
        assert video.duration == 10.0
        assert video.fps == 25.0
        assert video.storage_path == VIDEO_NAME  # 相对名
        assert len(db.query(Annotation).filter(Annotation.video_id == video.id).all()) == 1

        # 源文件未被破坏；目标文件内容一致（硬链接或复制都满足）
        assert (settings.videos_dir / VIDEO_NAME).read_bytes() == DEMO_SOURCE_BYTES
        assert src.read_bytes() == DEMO_SOURCE_BYTES

        # demo 用户为 owner；项目初始化同样的 12 类
        project = db.query(Project).filter(Project.name == PROJECT_NAME).one()
        demo = db.query(User).filter(User.username == settings.demo_username).one()
        membership = (
            db.query(ProjectMembership)
            .filter(ProjectMembership.project_id == project.id, ProjectMembership.user_id == demo.id)
            .one()
        )
        assert membership.role == "owner"
        assert db.query(BehaviorCategory).filter(BehaviorCategory.project_id == project.id).count() == 12
        assert (
            db.query(BehaviorCategory)
            .filter(
                BehaviorCategory.project_id == project.id,
                BehaviorCategory.name == ANNOTATION_BEHAVIOR,
            )
            .count()
            == 1
        )


def test_seed_demo_mock_metadata_without_source(tmp_path):
    """不提供视频源：仅创建 Mock 元数据（storage_path 为空）。"""
    settings = _settings(tmp_path)
    result = seed_demo(settings)

    with db_mod.SessionLocal() as db:
        video = db.get(Video, result["video_id"])
        assert video.storage_path is None
        assert video.status == "ready"
        assert video.duration == 10.0
        assert video.fps == 25.0
        # 不应在 videos_dir 下生成文件
        assert not (settings.videos_dir / VIDEO_NAME).exists()


def test_seed_demo_duration_fps_override_on_reuse(tmp_path):
    """CLI 覆盖 duration/fps：复用同一视频记录并应用新值。"""
    settings = _settings(tmp_path)
    seed_demo(settings, duration=5.0, fps=30.0)
    result = seed_demo(settings, duration=8.0, fps=24.0)

    with db_mod.SessionLocal() as db:
        video = db.get(Video, result["video_id"])
        assert video.duration == 8.0
        assert video.fps == 24.0
        assert db.query(Video).count() == 1  # 仍只有一条视频记录


def test_seed_demo_missing_source_rejected(tmp_path):
    """视频源不存在时明确报错。"""
    settings = _settings(tmp_path)
    with pytest.raises(FileNotFoundError):
        seed_demo(settings, video_source=tmp_path / "not_exist.mov")


def test_seed_demo_invalid_duration_rejected(tmp_path):
    """非正 duration/fps 明确报错。"""
    settings = _settings(tmp_path)
    with pytest.raises(ValueError):
        seed_demo(settings, duration=0)
    with pytest.raises(ValueError):
        seed_demo(settings, fps=-1)
