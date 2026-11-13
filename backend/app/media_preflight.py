"""Media dependency gate before multipart parsing; fake executors remain injectable."""
from __future__ import annotations

import re

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .media import FfmpegMediaProcessor, MediaDependencyError


def check_processor_dependencies(processor) -> None:
    if isinstance(processor, FfmpegMediaProcessor):
        processor.check_dependencies()


class MediaUploadGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and (
            scope["method"] == "POST" and re.fullmatch(
                r"/api/projects/\d+/(?:videos/upload|video-import-batches)/?", scope["path"])
            or scope["method"] == "PUT" and re.fullmatch(
                r"/api/projects/\d+/video-import-batches/\d+/files/(?:video|tracks|metadata|annotation)/?",
                scope["path"])
        ):
            try:
                await run_in_threadpool(check_processor_dependencies,
                                        scope["app"].state.media_worker.processor)
            except MediaDependencyError as exc:
                await JSONResponse({"detail": exc.detail}, status_code=503)(scope, receive, send)
                return
        await self.app(scope, receive, send)
