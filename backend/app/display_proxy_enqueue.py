"""Shared write-path helpers for explicitly queued display proxies."""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from .display_proxy_jobs import enqueue_display_proxy
from .file_identity import hash_file_handle
from .models import Video


def hash_display_proxy_source(path: Path) -> str:
    """Hash a stable source with the repository's descriptor-bound identity check."""
    digest, _identity = hash_file_handle(path)
    return digest


def enqueue_for_video(db: Session, video: Video, settings) -> int | None:
    """Enqueue in the caller's transaction, with zero work while disabled."""
    if not settings.display_proxies_enabled:
        return None
    job = enqueue_display_proxy(db, video)
    return job.id


def submit_after_commit(request, job_id: int | None) -> bool:
    """Submit committed work; failures deliberately leave the durable job queued."""
    if job_id is None:
        return False
    worker = getattr(request.app.state, "display_proxy_worker", None)
    if worker is None:
        from .display_proxy_observability import log_display_event
        log_display_event(logging.ERROR, "display_proxy_enqueue", job_id=job_id,
                          status="queued", error_category="worker_unavailable")
        return False
    try:
        worker.submit(job_id)
    except Exception:
        from .display_proxy_observability import log_display_event
        log_display_event(logging.ERROR, "display_proxy_enqueue", job_id=job_id,
                          status="queued", error_category="submit_failed")
        return False
    return True
