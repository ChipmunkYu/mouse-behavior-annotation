"""FastAPI dependency for the app-scoped, non-blocking per-video gate."""
from __future__ import annotations

from collections.abc import Iterator

from fastapi import HTTPException, Request

from .video_operation_gate import VideoOperationBusyError

VIDEO_OPERATION_BUSY_DETAIL = "Video is busy with another persistent operation; retry later"


def require_video_operation_gate(request: Request, video_id: int) -> Iterator[None]:
    try:
        with request.app.state.video_operation_gate.acquire(video_id):
            yield
    except VideoOperationBusyError as exc:
        raise HTTPException(status_code=409, detail=VIDEO_OPERATION_BUSY_DETAIL) from exc
