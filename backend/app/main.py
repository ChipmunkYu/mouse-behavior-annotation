"""应用入口：模块化单体的最小后端。"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import database as db_mod
from . import models  # noqa: F401  确保表注册到 Base.metadata
from . import seed
from .config import Settings, get_settings
from .routers import annotations, auth, categories, health, projects, reviews, videos


def create_app(settings: Settings | None = None) -> FastAPI:
    """应用工厂：初始化数据库、建表、种子数据，并注册路由。"""
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

    app = FastAPI(title="Behavior Annotation Backend", version="0.1.0")
    # 供各路由读取当前应用配置（如 stream 的视频目录安全边界）
    app.state.settings = s
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
    return app


# uvicorn app.main:app 使用；测试通过 create_app(settings=...) 自行构建
if os.environ.get("ANNOTATION_BACKEND_SKIP_APP") != "1":
    app = create_app()
