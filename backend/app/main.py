"""应用入口：模块化单体的最小后端。"""
from __future__ import annotations

import os
from contextlib import ExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import database as db_mod
from . import models  # noqa: F401  确保表注册到 Base.metadata
from . import seed
from .config import Settings, get_settings
from .cleanup import RetentionCleaner
from .export_jobs import ExportWorker
from .display_proxy_jobs import DisplayProxyWorker
from .display_proxy_processor import DisplayProxyProcessor
from .media import FfmpegMediaProcessor, MediaProcessor
from .media_jobs import MediaWorker
from .video_delete_service import VideoDeleteService
from .video_operation_gate import VideoOperationGateCoordinator
from .factory import register_routers


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
    display_proxy_processor=None,
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
        s.import_batches_dir,
        s.detection_imports_dir,
        s.display_proxies_dir,
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
    export_worker = ExportWorker(
        processor=processor,
        session_factory=db_mod.SessionLocal,
        settings=s,
    )
    cleanup_worker = RetentionCleaner(
        session_factory=db_mod.SessionLocal,
        settings=s,
        synchronous=s.media_synchronous,
    )
    video_operation_gate = VideoOperationGateCoordinator()
    video_delete_service = VideoDeleteService(
        session_factory=db_mod.SessionLocal, settings=s, gate=video_operation_gate,
    )
    display_worker = None
    if s.display_proxies_enabled:
        display_worker = DisplayProxyWorker(
            processor=display_proxy_processor or DisplayProxyProcessor(
                ffmpeg_path=s.ffmpeg_path, ffprobe_path=s.ffprobe_path,
                timeout_seconds=s.display_proxy_timeout_seconds),
            session_factory=db_mod.SessionLocal, settings=s,
        )

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # ExitStack also rolls back already-started workers when a later startup fails.
        with ExitStack() as startup:
            # 删除恢复必须先于 worker 恢复/调度，避免残留文件被重新消费或发布。
            delete_recovery = video_delete_service.recover()
            if any(not result.ok for result in delete_recovery):
                raise RuntimeError("Video delete startup recovery requires administrator intervention")
            # 两类恢复先全部完成再调度，避免后恢复者重置另一 worker 的新 Clip claim。
            worker._recover_interrupted()
            export_worker._recover_interrupted()
            if display_worker is not None:
                display_worker.start()
                startup.callback(display_worker.shutdown)
            worker.start(recover=False)
            startup.callback(worker.shutdown)
            export_worker.start(recover=False)
            startup.callback(export_worker.shutdown)
            cleanup_worker.start()
            startup.callback(cleanup_worker.shutdown)
            yield

    app = FastAPI(title="Behavior Annotation Backend", version="0.1.0", lifespan=_lifespan)
    # 供各路由读取当前应用配置（如 stream 的视频目录安全边界）
    app.state.settings = s
    app.state.media_worker = worker
    app.state.export_worker = export_worker
    app.state.cleanup_worker = cleanup_worker
    app.state.video_operation_gate = video_operation_gate
    app.state.video_delete_service = video_delete_service
    app.state.display_proxy_worker = display_worker
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return register_routers(app)


# uvicorn app.main:app 使用；测试通过 create_app(settings=...) 自行构建
if os.environ.get("ANNOTATION_BACKEND_SKIP_APP") != "1":
    app = create_app()
