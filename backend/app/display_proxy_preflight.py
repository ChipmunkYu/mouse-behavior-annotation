"""Read-only strict display-proxy cutover preflight."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from .display_proxy_jobs import JOB_TYPE_DISPLAY_PROXY
from .display_proxy_processor import DISPLAY_PROXY_PROFILE_VERSION
from .models import BackgroundJob, Video
from .video_playback import _safe_file


@dataclass(frozen=True)
class DisplayProxyPreflight:
    file_backed_total: int = 0
    ready_safe: int = 0
    pending: int = 0
    processing: int = 0
    failed: int = 0
    invalid: int = 0
    active_display_jobs: int = 0
    metadata_only_excluded: int = 0

    @property
    def passed(self) -> bool:
        return self.ready_safe == self.file_backed_total and self.active_display_jobs == 0

    def lines(self) -> list[str]:
        values = asdict(self)
        return ["Display proxy strict-mode preflight: " + ("PASS" if self.passed else "FAIL"),
                *(f"  {key}: {value}" for key, value in values.items())]


def ready_entity_is_safe(video, settings) -> bool:
    """Check only persisted identity/profile fields and the bounded proxy entity."""
    identity_ok = bool(video.source_sha256
                       and video.display_source_sha256 == video.source_sha256)
    profile_ok = video.display_profile_version == DISPLAY_PROXY_PROFILE_VERSION
    entity_ok = _safe_file(video.display_path, settings.display_proxies_dir) is not None
    return video.display_status == "ready" and identity_ok and profile_ok and entity_ok


def inspect_display_proxy_readiness(db: Session, settings) -> DisplayProxyPreflight:
    """Inspect database and proxy entities without hashing, probing, enqueueing, or writes."""
    counts = {name: 0 for name in DisplayProxyPreflight.__dataclass_fields__}
    for video in db.query(Video).all():
        if not video.storage_path:
            counts["metadata_only_excluded"] += 1
            continue
        counts["file_backed_total"] += 1
        if video.display_status in {"pending", "processing", "failed"}:
            counts[video.display_status] += 1
            continue
        if ready_entity_is_safe(video, settings):
            counts["ready_safe"] += 1
        else:
            counts["invalid"] += 1
    counts["active_display_jobs"] = db.query(BackgroundJob).filter(
        BackgroundJob.job_type == JOB_TYPE_DISPLAY_PROXY,
        BackgroundJob.status.in_(("queued", "running")),
    ).count()
    return DisplayProxyPreflight(**counts)
