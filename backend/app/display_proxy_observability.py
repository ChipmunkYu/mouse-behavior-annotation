"""Small, allow-listed structured events for the display-proxy lane."""
from __future__ import annotations

import json
import logging
from typing import Any

ALLOWED_FIELDS = {
    "event", "job_id", "video_id", "project_id", "profile", "status",
    "elapsed_ms", "bytes", "error_category", "source_match",
}
EVENT_LOGGER_NAME = "app.display_proxy"


def configure_display_proxy_observability() -> None:
    """Restore the owned logger after Alembic logging configuration runs.

    Events propagate to the root logger without a dedicated handler or a local
    threshold, leaving the effective runtime level under operator control.
    """
    logger = logging.getLogger(EVENT_LOGGER_NAME)
    logger.disabled = False
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
    logger.handlers.clear()


def log_display_event(level: int, event: str, **fields: Any) -> None:
    """Emit only explicitly approved scalar fields; never attach exception details."""
    payload = {"event": event}
    payload.update({key: value for key, value in fields.items()
                    if key in ALLOWED_FIELDS and value is not None})
    logger = logging.getLogger(EVENT_LOGGER_NAME)
    logger.log(level, json.dumps(payload, sort_keys=True, separators=(",", ":")))


def error_category(exc: Exception) -> str:
    """Map failures to stable categories without exposing exception text."""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "diskspace" in name or getattr(exc, "errno", None) == 28:
        return "disk_space"
    if "ownership" in name:
        return "ownership"
    if "payload" in text:
        return "invalid_payload"
    if "sha-256" in text:
        return "source_identity"
    return "processing"
