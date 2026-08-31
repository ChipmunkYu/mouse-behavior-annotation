"""Authoritative selection of the entity exposed by the public video stream."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import logging
from pathlib import Path
from threading import Lock
from typing import Literal

from .display_proxy_processor import DISPLAY_PROXY_PROFILE_VERSION
from .display_proxy_observability import log_display_event


DISPLAY_PROXY_PENDING = "DISPLAY_PROXY_PENDING"
DISPLAY_PROXY_FAILED = "DISPLAY_PROXY_FAILED"

PlaybackStatus = Literal["ready", "pending", "failed", "unavailable"]
_OBSERVED_LIMIT = 4096
_observed: OrderedDict[int, tuple] = OrderedDict()
_observed_lock = Lock()


@dataclass(frozen=True)
class PlaybackResolution:
    status: PlaybackStatus
    path: Path | None = None
    is_display_proxy: bool = False


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

    With proxies disabled, preserve source playback. With proxies enabled,
    resolution is strict and never inspects or falls back to the source file.
    A ready proxy must be safe, non-empty, and bound to the current source
    identity and fixed candidate profile. No per-request hash/probe is done.
    """
    if not settings.display_proxies_enabled:
        source = _safe_file(video.storage_path, settings.videos_dir)
        playable_source = source if video.status != "needs_transcode" else None
        return (PlaybackResolution("ready", playable_source) if playable_source is not None
                else PlaybackResolution("unavailable"))

    display_status = video.display_status
    if display_status == "failed":
        return PlaybackResolution("failed")
    if display_status == "ready":
        if (
            not video.source_sha256
            or video.display_source_sha256 != video.source_sha256
            or video.display_profile_version != DISPLAY_PROXY_PROFILE_VERSION
        ):
            return PlaybackResolution("failed")
        proxy = _safe_file(video.display_path, settings.display_proxies_dir)
        return (PlaybackResolution("ready", proxy, is_display_proxy=True) if proxy is not None
                else PlaybackResolution("failed"))
    if display_status in ("pending", "processing"):
        return PlaybackResolution("pending")
    return PlaybackResolution("failed")


def observe_strict_playback(video, resolution: PlaybackResolution) -> None:
    """Record state transitions once per process so normal polling stays quiet."""
    source_match = bool(video.source_sha256
                        and video.display_source_sha256 == video.source_sha256)
    if resolution.status in {"pending", "failed", "ready"}:
        profile_match = video.display_profile_version == DISPLAY_PROXY_PROFILE_VERSION
        signature = (resolution.status, profile_match, source_match)
        with _observed_lock:
            if _observed.get(video.id) == signature:
                _observed.move_to_end(video.id)
                return
            _observed[video.id] = signature
            _observed.move_to_end(video.id)
            if len(_observed) > _OBSERVED_LIMIT:
                _observed.popitem(last=False)
        level = logging.WARNING if resolution.status == "failed" else logging.INFO
        log_display_event(level, f"strict_playback_{resolution.status}",
                          video_id=video.id, project_id=video.project_id,
                          profile=DISPLAY_PROXY_PROFILE_VERSION, status=resolution.status,
                          source_match=source_match)


def public_video(video, settings, **extra) -> dict:
    """Build a VideoOut-compatible value using the authoritative resolver."""
    from .schemas import VideoOut

    data = VideoOut.model_validate(
        video, from_attributes=True,
        context={"playback_status": resolve_video_playback(video, settings).status},
    ).model_dump()
    data.update(extra)
    return data
