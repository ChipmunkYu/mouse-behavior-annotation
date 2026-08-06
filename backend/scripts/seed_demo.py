"""第一阶段本地演示数据脚本（幂等）。

从 backend 目录运行：
    .venv/Scripts/python scripts/seed_demo.py
    .venv/Scripts/python scripts/seed_demo.py --video-source C:/path/to/some.mov
    .venv/Scripts/python scripts/seed_demo.py --duration 5 --fps 30

仅面向本地开发演示，直接复用 app 配置 / Session / 模型 / seed 逻辑，
不修改任何生产接口；可安全重复运行，不会产生重复数据。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# 使脚本可从任意目录直接运行（脚本位于 backend/scripts/ 下）
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 仅开发演示输出：Windows 控制台按 UTF-8 输出，避免中文路径/文本乱码
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

from app import database as db_mod  # noqa: E402
from app import models  # noqa: E402, F401  确保表注册到 Base.metadata
from app.config import Settings, get_settings  # noqa: E402
from app.models import (  # noqa: E402
    Annotation,
    BehaviorCategory,
    Project,
    ProjectMembership,
    Video,
)
from app.seed import ensure_demo_user, init_project_categories  # noqa: E402

PROJECT_NAME = "北医行为标注演示"
VIDEO_NAME = "demo_attack.mov"
VIDEO_STATUS = "ready"
DEFAULT_DURATION = 10.0
DEFAULT_FPS = 25.0
# “攻击行为”演示标注：1.0-3.0 秒，25-75 帧
ANNOTATION_INTERVAL = {
    "start_time": 1.0,
    "end_time": 3.0,
    "start_frame": 25,
    "end_frame": 75,
}
ANNOTATION_BEHAVIOR = "攻击行为"


def _place_video_file(source: Path, videos_dir: Path) -> Path:
    """把源视频放入配置 videos_dir 下（硬链接优先、复制回退），返回目标路径。"""
    videos_dir.mkdir(parents=True, exist_ok=True)
    target = videos_dir / VIDEO_NAME
    if target.exists():
        return target
    try:
        os.link(source, target)  # 同一文件系统上优先硬链接，不复制数据
    except OSError:
        shutil.copy2(source, target)  # 跨卷 / 权限受限时回退为复制
    return target


def seed_demo(
    settings: Settings,
    video_source: Path | None = None,
    duration: float = DEFAULT_DURATION,
    fps: float = DEFAULT_FPS,
) -> dict:
    """幂等创建演示数据；返回项目/视频/标注 id、是否新建及 demo 登录信息。"""
    if duration <= 0 or fps <= 0:
        raise ValueError("duration 与 fps 必须为正数")

    # 与 create_app 相同的初始化路径：目录、引擎、幂等迁移建表
    for directory in (settings.data_dir, settings.videos_dir, settings.exports_dir):
        directory.mkdir(parents=True, exist_ok=True)
    db_mod.configure_engine(settings.resolved_database_url)
    db_mod.ensure_schema(settings.resolved_database_url)

    with db_mod.SessionLocal() as db:
        demo_user = ensure_demo_user(db, settings)

        # ---------- 幂等：项目（demo 用户为 owner） ----------
        project = db.query(Project).filter(Project.name == PROJECT_NAME).first()
        project_created = project is None
        if project is None:
            project = Project(
                name=PROJECT_NAME,
                description="第一阶段本地演示项目（seed_demo.py 自动创建）",
                status="active",
                created_by=demo_user.id,
            )
            db.add(project)
            db.flush()  # 获取 project.id
            db.add(ProjectMembership(project_id=project.id, user_id=demo_user.id, role="owner"))
            init_project_categories(db, project.id)  # 同样的项目级 12 类
            db.commit()
            db.refresh(project)
        else:
            # 复用已有项目时，仍保证 demo 用户是 owner 成员
            membership = (
                db.query(ProjectMembership)
                .filter(
                    ProjectMembership.project_id == project.id,
                    ProjectMembership.user_id == demo_user.id,
                )
                .first()
            )
            if membership is None:
                db.add(
                    ProjectMembership(project_id=project.id, user_id=demo_user.id, role="owner")
                )
                db.commit()

        # ---------- “攻击行为”类别（新建项目时已由 12 类种子初始化） ----------
        attack_category = (
            db.query(BehaviorCategory)
            .filter(
                BehaviorCategory.project_id == project.id,
                BehaviorCategory.name == ANNOTATION_BEHAVIOR,
            )
            .first()
        )
        if attack_category is None:
            raise RuntimeError(f"项目缺少类别「{ANNOTATION_BEHAVIOR}」，请检查种子数据")

        # ---------- 可选：把源视频放到配置 videos_dir ----------
        storage_path: str | None = None
        if video_source is not None:
            src = Path(video_source).expanduser()
            if not src.is_file():
                raise FileNotFoundError(f"视频源不存在: {src}")
            _place_video_file(src, settings.videos_dir)
            storage_path = VIDEO_NAME  # 数据库存相对名，供 stream 端点在 videos_dir 内解析

        # ---------- 幂等：视频 ----------
        video = (
            db.query(Video)
            .filter(Video.project_id == project.id, Video.filename == VIDEO_NAME)
            .first()
        )
        video_created = video is None
        if video is None:
            video = Video(
                project_id=project.id,
                filename=VIDEO_NAME,
                duration=duration,
                fps=fps,
                status=VIDEO_STATUS,
                storage_path=storage_path,
                uploaded_by=demo_user.id,
            )
            db.add(video)
            db.commit()
            db.refresh(video)
        else:
            # 复用：应用 CLI 解析后的 duration/fps/status，并补充 storage_path
            video.duration = duration
            video.fps = fps
            video.status = VIDEO_STATUS
            if storage_path is not None:
                video.storage_path = storage_path
            db.commit()
            db.refresh(video)

        # ---------- 幂等：一条“攻击行为”标注 ----------
        annotation = (
            db.query(Annotation)
            .join(BehaviorCategory, Annotation.category_id == BehaviorCategory.id)
            .filter(
                Annotation.video_id == video.id,
                BehaviorCategory.name == ANNOTATION_BEHAVIOR,
                Annotation.start_time == ANNOTATION_INTERVAL["start_time"],
                Annotation.end_time == ANNOTATION_INTERVAL["end_time"],
                Annotation.start_frame == ANNOTATION_INTERVAL["start_frame"],
                Annotation.end_frame == ANNOTATION_INTERVAL["end_frame"],
            )
            .first()
        )
        annotation_created = annotation is None
        if annotation is None:
            annotation = Annotation(
                video_id=video.id,
                annotator_id=demo_user.id,
                category_id=attack_category.id,
                start_time=ANNOTATION_INTERVAL["start_time"],
                end_time=ANNOTATION_INTERVAL["end_time"],
                start_frame=ANNOTATION_INTERVAL["start_frame"],
                end_frame=ANNOTATION_INTERVAL["end_frame"],
                confidence="certain",
                review_status="pending",
            )
            db.add(annotation)
            db.commit()
            db.refresh(annotation)

        return {
            "project_id": project.id,
            "project_created": project_created,
            "video_id": video.id,
            "video_created": video_created,
            "annotation_id": annotation.id,
            "annotation_created": annotation_created,
            "demo_username": settings.demo_username,
            "demo_password": settings.demo_password,  # 明文 demo 密码，便于登录演示
            "video_path": str(settings.videos_dir / VIDEO_NAME) if video.storage_path else None,
        }


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="幂等创建第一阶段本地演示数据（项目/视频/标注/demo 账号）"
    )
    parser.add_argument(
        "--video-source",
        type=Path,
        default=None,
        metavar="PATH",
        help="本地视频文件路径（可选）；硬链接/复制到 data/videos/demo_attack.mov，"
        "不提供则仅创建 Mock 元数据",
    )
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION, help=f"视频时长（秒），默认 {DEFAULT_DURATION}")
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS, help=f"视频帧率，默认 {DEFAULT_FPS}")
    args = parser.parse_args()

    try:
        result = seed_demo(get_settings(), args.video_source, args.duration, args.fps)
    except FileNotFoundError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        sys.exit(1)

    print("演示数据就绪（可重复运行，幂等）：")
    print(
        f"  项目 id={result['project_id']}  名称={PROJECT_NAME}  "
        f"({'新建' if result['project_created'] else '复用'})"
    )
    print(
        f"  视频 id={result['video_id']}  文件名={VIDEO_NAME}  "
        f"({'新建' if result['video_created'] else '复用'})"
    )
    print(
        f"  标注 id={result['annotation_id']}  行为=攻击行为  1.0-3.0s / 25-75 帧  "
        f"({'新建' if result['annotation_created'] else '复用'})"
    )
    if result["video_path"]:
        print(f"  视频文件 {result['video_path']}")
    # 只输出登录信息，绝不输出密码哈希
    print(f"  demo 登录：{result['demo_username']} / {result['demo_password']}")


if __name__ == "__main__":
    _main()
