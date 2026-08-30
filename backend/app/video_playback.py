"""Authoritative selection of the entity exposed by the public video stream."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


PlaybackStatus = Literal["ready", "pending", "failed", "unavailable"]


@dataclass(frozen=True)
class PlaybackResolution:
    status: PlaybackStatus
    path: Path | None = None


def _safe_file(stored: str | None, root: Path) -> Path | None:
    if not stored:
        return None
    try:
        base = root.resolve()
        raw = Path(stored)
        candidate = raw.resolve() if raw.is_absolute() else (base / raw).resolve()
        if candidate == base or not candidate.is_relative_to(base) or not candidate.is_file():
            return None
        if candidate.stat().st_size <= 0:
            return None
        return candidate
    except (OSError, RuntimeError, ValueError):
        return None


def resolve_video_playback(video, settings) -> PlaybackResolution:
    """Resolve exactly what ticket/stream can currently serve.

    The source is served by default and whenever fallback is allowed.  Strict
    proxy mode requires a verified ready proxy and never serves the source.
    """
    source = _safe_file(video.storage_path, settings.videos_dir)
    playable_source = source if video.status != "needs_transcode" else None
    if not settings.display_proxies_enabled:
        return (PlaybackResolution("ready", playable_source) if playable_source is not None
                else PlaybackResolution("unavailable"))
    if settings.display_proxy_allow_source_fallback and playable_source is not None:
        return PlaybackResolution("ready", playable_source)

    display_status = video.display_status
    if display_status == "failed":
        return PlaybackResolution("failed")
    if display_status == "ready":
        proxy = _safe_file(video.display_path, settings.display_proxies_dir)
        if proxy is not None:
            return PlaybackResolution("ready", proxy)
    if source is None:
        return PlaybackResolution("unavailable")
    return PlaybackResolution("pending")


def public_video(video, settings, **extra) -> dict:
    """Build a VideoOut-compatible value using the authoritative resolver."""
    from .schemas import VideoOut

    data = VideoOut.model_validate(
        video, from_attributes=True,
        context={"playback_status": resolve_video_playback(video, settings).status},
    ).model_dump()
    data.update(extra)
    return data
