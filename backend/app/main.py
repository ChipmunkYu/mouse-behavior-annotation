"""应用入口：模块化单体的最小后端。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import database as db_mod
from . import models  # noqa: F401  确保表注册到 Base.metadata
from . import seed
from .config import Settings, get_settings
from .media import FfmpegMediaProcessor, MediaProcessor
from .media_jobs import MediaWorker
from .routers import annotations, auth, categories, health, media, projects, reviews, videos


def _default_media_processor(settings: Settings) -> MediaProcessor:
    """默认媒体执行器：真实 ffmpeg/ffprobe（可经 FFMPEG_PATH / FFPROBE_PATH 覆盖）。"""
    return FfmpegMediaProcessor(
        ffmpeg_path=settings.ffmpeg_path,
        ffprobe_path=settings.ffprobe_path,
        crf=settings.media_crf,
        preset=settings.media_preset,
        timeout_seconds=settings.media_timeout_seconds,
        map_audio=settings.media_map_audio,
    )


def create_app(
    settings: Settings | None = None,
    media_processor: MediaProcessor | None = None,
) -> FastAPI:
    """应用工厂：初始化数据库、建表、种子数据、媒体 worker，并注册路由。

    - `settings`：注入测试用配置（临时 data_dir / media_synchronous 等）。
    - `media_processor`：注入可替换媒体执行器（测试用 FakeMediaProcessor），
      不要求本机安装 ffmpeg。
    """
    s = settings or get_settings()

    for directory in (
        s.data_dir,
        s.videos_dir,
        s.exports_dir,
        s.clips_dir,
        s.thumbnails_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    db_mod.configure_engine(s.resolved_database_url)
    db_mod.ensure_schema(s.resolved_database_url)

    with db_mod.SessionLocal() as db:
        seed.ensure_demo_user(db, s)

    processor = media_processor if media_processor is not None else _default_media_processor(s)
    worker = MediaWorker(
        processor=processor,
        session_factory=db_mod.SessionLocal,
        settings=s,
    )

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # 启动恢复：running 任务视为中断（重排/判失败）；调度全部 queued 任务
        worker.start()
        try:
            yield
        finally:
            worker.shutdown()

    app = FastAPI(title="Behavior Annotation Backend", version="0.1.0", lifespan=_lifespan)
    # 供各路由读取当前应用配置（如 stream 的视频目录安全边界）
    app.state.settings = s
    app.state.media_worker = worker
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(projects.router)
    app.include_router(categories.router)
    app.include_router(videos.router)
    app.include_router(annotations.router)
    app.include_router(reviews.router)
    app.include_router(media.router)
    return app


# uvicorn app.main:app 使用；测试通过 create_app(settings=...) 自行构建
if os.environ.get("ANNOTATION_BACKEND_SKIP_APP") != "1":
    app = create_app()
