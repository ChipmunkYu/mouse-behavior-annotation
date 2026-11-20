"""Immutable context material, shared by prewarming and streaming exports.

Lifecycle integration: acquire CACHE_LOCK before quarantining video-{id}; entries
in PINNED_KEYS and .job-{id}-* scratch directories belong to active workers.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from bisect import bisect_left, bisect_right
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .export_contract import FILES
from .models import BackgroundJob, SubmissionAnnotation

CACHE_LOCK = threading.RLock()
PINNED_KEYS: set[str] = set()
_PIN_COUNTS: dict[str, int] = {}
JOB_TYPE_PREWARM = "context_prewarm"
# Successful hashes are reusable only for the same real file identity, including ctime.
_HASHES: dict[str, tuple] = {}


def cache_root(settings) -> Path:
    return settings.data_dir / "context_cache"


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def check_space(settings, path: Path, additional: int = 0) -> None:
    while not path.exists():
        path = path.parent
    required = settings.export_disk_reserve_bytes + additional
    free = shutil.disk_usage(path).free
    if free < required:
        resident = evict_cache(settings)["total_bytes"]
        evict_cache(settings, target_bytes=max(0, resident - (required - free)))
    if shutil.disk_usage(path).free < required:
        raise OSError(errno.ENOSPC, "export disk reserve would be exceeded")


def identity(path: Path, *, fd=None) -> tuple:
    if _is_link(path) or not path.is_file():
        raise ValueError("not a regular cache file")
    stat = os.fstat(fd) if fd is not None else path.stat()
    change_time = stat.st_ctime_ns
    if os.name == "nt":
        # Windows st_ctime is creation time, not inode change time. Restoring mtime
        # after a same-size write must still invalidate the successful hash memo.
        import ctypes
        import msvcrt
        class BasicInfo(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in
                        ("creation", "access", "write", "change")] + [("attributes", ctypes.c_ulong)]
        handle_fd = fd if fd is not None else os.open(path, os.O_RDONLY | os.O_BINARY)
        try:
            info = BasicInfo()
            if not ctypes.windll.kernel32.GetFileInformationByHandleEx(
                    ctypes.c_void_p(msvcrt.get_osfhandle(handle_fd)), 0, ctypes.byref(info), ctypes.sizeof(info)):
                raise OSError("cannot read cache file change identity")
            change_time = info.change
        finally:
            if fd is None:
                os.close(handle_fd)
    return (str(path.resolve()), stat.st_dev, stat.st_ino, stat.st_size,
            stat.st_mtime_ns, change_time)


def file_hash(path: Path) -> str:
    before = identity(path)
    with CACHE_LOCK:
        cached = _HASHES.get(before[0])
        if cached is not None and cached[0] == before:
            return cached[1]
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if identity(path, fd=stream.fileno()) != before:
            raise ValueError("cache file changed before hashing")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        if identity(path, fd=stream.fileno()) != before:
            raise ValueError("cache handle changed while hashing")
    if identity(path) != before:
        raise ValueError("cache file changed while hashing")
    # One identity per path; keep the full resident working set, not a scan-thrashing LRU.
    with CACHE_LOCK:
        _HASHES[before[0]] = (before, digest.hexdigest())
    return digest.hexdigest()


def context_range(db, annotation, submission, neighbors=None):
    snapshot = submission.detection_snapshot
    if getattr(getattr(submission, "video", None), "source", None) == "heuristic":
        return 0, snapshot.frame_count - 1
    start = max(0, annotation.start_frame - round(snapshot.fps))
    end = min(snapshot.frame_count - 1, annotation.end_frame + round(snapshot.fps))
    neighbors = neighbors if neighbors is not None else db.query(SubmissionAnnotation).filter(
        SubmissionAnnotation.submission_id == submission.id,
        SubmissionAnnotation.category_id == annotation.category_id,
        SubmissionAnnotation.id != annotation.id).all()
    for neighbor in neighbors:
        if neighbor.id == annotation.id or not set(annotation.mouse_ids).intersection(neighbor.mouse_ids):
            continue
        if neighbor.end_frame < annotation.start_frame and neighbor.end_frame >= start:
            start = max(start, neighbor.end_frame + 1)
        if neighbor.start_frame > annotation.end_frame and neighbor.start_frame <= end:
            end = min(end, neighbor.start_frame - 1)
    return start, end


def context_batch(db, rows):
    groups = {(s.id, a.category_id) for a, s, _c in rows}
    neighbors = {group: [] for group in groups}
    if groups:
        for n in db.query(SubmissionAnnotation).filter(
                SubmissionAnnotation.submission_id.in_({s for s, _ in groups})).order_by(SubmissionAnnotation.id):
            if (n.submission_id, n.category_id) in neighbors:
                neighbors[n.submission_id, n.category_id].append(n)
    result = {}
    for group, items in neighbors.items():
        tracks = {}
        for n in items:
            for mouse in n.mouse_ids:
                starts, ends = tracks.setdefault(mouse, ([], []))
                starts.append(n.start_frame)
                ends.append(n.end_frame)
        for starts, ends in tracks.values():
            starts.sort()
            ends.sort()
        digest = hashlib.sha256(json.dumps(
            [[n.id, n.start_frame, n.end_frame, n.mouse_ids] for n in items],
            separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        result[group] = tracks, digest
    return result


def frozen_context(db, annotation, submission, clip, batch=None) -> dict:
    """Only actual owner/snapshot neighbors; never read the latest attempt."""
    snapshot = submission.detection_snapshot
    if batch is None:
        batch = context_batch(db, [(annotation, submission, clip)])
    tracks, neighbor_digest = batch[submission.id, annotation.category_id]
    start = max(0, annotation.start_frame - round(snapshot.fps))
    end = min(snapshot.frame_count - 1, annotation.end_frame + round(snapshot.fps))
    for mouse in annotation.mouse_ids:
        starts, ends = tracks.get(mouse, ([], []))
        before = bisect_left(ends, annotation.start_frame)
        after = bisect_right(starts, annotation.end_frame)
        if before:
            start = max(start, ends[before - 1] + 1)
        if after < len(starts):
            end = min(end, starts[after] - 1)
    if submission.video.source == "heuristic":
        start, end = 0, snapshot.frame_count - 1
    fields = ("id", "category_id", "category_name", "category_participant_mode",
              "participant_roles_snapshot", "role_definitions_snapshot", "mouse_ids",
              "confidence", "crop_region", "start_frame", "end_frame", "start_time", "end_time")
    return {
        "version": 2, "clip_id": clip.id, "submission_id": submission.id,
        "annotation": {name: getattr(annotation, name) for name in fields},
        "range": [start, end],
        "neighbors": neighbor_digest,
        "snapshot": [snapshot.id, snapshot.raw_digest, snapshot.state_digest, snapshot.metadata_digest],
        "source": source_identity(submission),
        "source_filename": submission.source_video_filename,
        "heuristic_input": submission.video.heuristic_input if submission.video.source == "heuristic" else None,
    }


def source_identity(submission) -> list:
    return [submission.source_storage_key, submission.source_media_revision,
            submission.source_video_sha256, submission.source_file_size,
            submission.source_mtime_ns, submission.source_device, submission.source_inode]


def source_group_identity(submission) -> tuple:
    # A submission media revision can advance without changing the source file.
    # Keep revisions in immutable context keys, not in physical staging groups.
    full = source_identity(submission)
    return tuple([full[0], *full[2:]])


def distance_scale(submission):
    calibration = submission.video.distance_calibration
    if calibration is not None:
        return {"cm_per_pixel": calibration["cm_per_pixel"], "unit": "cm"}
    if submission.video.source == "heuristic":
        return (submission.video.heuristic_input or {}).get("distance_scale")
    return None


def cache_key(settings, context: dict) -> str:
    profile = [settings.media_crf, settings.media_preset, settings.media_map_audio,
               settings.ffmpeg_path, "context-render-v1"]
    return hashlib.sha256(json.dumps([context, profile], sort_keys=True,
                                    separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def entry_path(settings, video_id: int, key: str) -> Path:
    if not isinstance(video_id, int) or video_id <= 0 or not re.fullmatch(r"[a-f0-9]{64}", key):
        raise ValueError("invalid cache identity")
    root = cache_root(settings)
    path = root / f"video-{video_id}" / key
    for part in (root, path.parent, path):
        if _is_link(part):
            raise ValueError("symlink cache directory")
    return path


def lookup(settings, video_id: int, key: str, *, touch=False) -> Path | None:
    with CACHE_LOCK:
        try:
            entry = entry_path(settings, video_id, key)
            manifest_path = entry / "manifest.json"
            if _is_link(manifest_path) or not manifest_path.is_file() or manifest_path.stat().st_size > 65536:
                return None
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            item = entry / "item"
            if (not isinstance(manifest, dict) or _is_link(item) or not item.is_dir()
                    or manifest.get("key") != key or not isinstance(manifest.get("files"), dict)
                    or set(manifest["files"]) != FILES):
                return None
            if {p.name for p in item.iterdir()} != FILES:
                return None
            for name in FILES:
                path = item / name
                info = manifest["files"][name]
                if (not isinstance(info, dict) or type(info.get("size")) is not int or info["size"] < 0
                        or not isinstance(info.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", info["sha256"])):
                    return None
                if identity(path)[3] != info["size"] or file_hash(path) != info["sha256"]:
                    return None
            if touch:
                os.utime(entry, None)
            return item
        except (OSError, ValueError, KeyError, TypeError):
            return None


def evict_cache(settings, *, target_bytes=None) -> dict:
    """LRU eviction under the lifecycle lock; never follow links or remove pins/scratch."""
    with CACHE_LOCK:
        root = cache_root(settings)
        for path in list(_HASHES):
            if not Path(path).is_file():
                del _HASHES[path]
        target = settings.context_cache_max_bytes if target_bytes is None else max(0, target_bytes)
        entries = []
        if _is_link(root):
            raise ValueError("symlink cache root")
        if root.exists():
            for video in root.iterdir():
                if _is_link(video) or not video.is_dir() or not re.fullmatch(r"video-[1-9][0-9]*", video.name):
                    continue
                for entry in video.iterdir():
                    if _is_link(entry) or not entry.is_dir() or not re.fullmatch(r"[a-f0-9]{64}", entry.name):
                        continue
                    size = sum(p.stat().st_size for p in entry.rglob("*") if p.is_file() and not _is_link(p))
                    entries.append((entry.stat().st_mtime_ns, entry, size))
        total = sum(e[2] for e in entries)
        removed = []
        for _, entry, size in sorted(entries):
            if total <= target:
                break
            if entry.name in PINNED_KEYS:
                continue
            shutil.rmtree(entry)
            prefix = str(entry.resolve()) + os.sep
            for memo in list(_HASHES):
                if memo.startswith(prefix):
                    del _HASHES[memo]
            removed.append(entry.name)
            total -= size
        return {"total_bytes": total, "removed_keys": removed, "target_bytes": target}


def cleanup_job_scratch(settings, job_id: int) -> None:
    """Remove only this claimed/recovered job's private material; never follow links."""
    from .media_jobs import WorkerCleanupIncomplete
    with CACHE_LOCK:
        root = cache_root(settings)
        if _is_link(root):
            raise ValueError("symlink cache root")
        try:
            for video in root.glob("video-*"):
                if _is_link(video) or not video.is_dir():
                    continue
                for scratch in video.glob(f".job-{int(job_id)}-*"):
                    if _is_link(scratch):
                        scratch.unlink()
                    elif scratch.is_dir():
                        shutil.rmtree(scratch)
        except OSError as exc:
            raise WorkerCleanupIncomplete("context job scratch cleanup failed") from exc


@contextmanager
def material(settings, video_id, key, job_id, builder):
    """Pin only this entry. Oversized/disabled residency still yields validated scratch."""
    scratch = None
    with CACHE_LOCK:
        _PIN_COUNTS[key] = _PIN_COUNTS.get(key, 0) + 1
        PINNED_KEYS.add(key)
        hit = lookup(settings, video_id, key, touch=True) if settings.context_cache_max_bytes else None
    try:
        if hit is not None:
            yield hit
            return
        with CACHE_LOCK:
            entry = entry_path(settings, video_id, key)
            entry.parent.mkdir(parents=True, exist_ok=True)
            if entry.exists() and _PIN_COUNTS[key] == 1:
                shutil.rmtree(entry)  # Failed integrity check; do not reserve space for corrupt bytes.
            scratch = entry.parent / f".job-{job_id}-{uuid.uuid4().hex}"
            scratch.mkdir(mode=0o700)
        check_space(settings, scratch)
        builder(scratch / "item")  # builder probes media and validates tracks/four-file contract
        if {p.name for p in (scratch / "item").iterdir()} != FILES:
            raise ValueError("context material must contain exactly four files")
        files = {name: {"size": (scratch / "item" / name).stat().st_size,
                        "sha256": file_hash(scratch / "item" / name)} for name in FILES}
        (scratch / "manifest.json").write_text(json.dumps({"key": key, "files": files}), encoding="utf-8")
        size = sum(p.stat().st_size for p in scratch.rglob("*") if p.is_file())
        with CACHE_LOCK:
            limit = settings.context_cache_max_bytes
            # Exclude this key's stale version when replacing corrupted material.
            can_replace = _PIN_COUNTS[key] == 1
            if entry.exists() and can_replace:
                shutil.rmtree(entry)
            result = evict_cache(settings, target_bytes=max(0, limit - size))
            if can_replace and limit and size <= limit and result["total_bytes"] + size <= limit:
                os.replace(scratch, entry)
                scratch = None
                item = entry / "item"
            else:
                item = scratch / "item"
        yield item
    finally:
        try:
            with CACHE_LOCK:
                if scratch is not None and scratch.exists():
                    try:
                        shutil.rmtree(scratch)
                    except OSError as exc:
                        from .media_jobs import WorkerCleanupIncomplete
                        raise WorkerCleanupIncomplete("context scratch cleanup failed") from exc
        finally:
            with CACHE_LOCK:
                _PIN_COUNTS[key] -= 1
                if not _PIN_COUNTS[key]:
                    del _PIN_COUNTS[key]
                    PINNED_KEYS.discard(key)


def enqueue_prewarm(db, settings, rows) -> list[int]:
    """Caller owns publication transaction. Persist even when media is reused."""
    if not settings.context_prewarm_enabled or not settings.context_cache_max_bytes:
        return []
    ids = []
    batch = context_batch(db, rows)
    dedupes = []
    for annotation, submission, clip in rows:
        context = frozen_context(db, annotation, submission, clip, batch)
        key = cache_key(settings, context)
        dedupe = f"context:{submission.video_id}:{key}"
        db.execute(sqlite_insert(BackgroundJob).values(project_id=submission.video.project_id,
            job_type=JOB_TYPE_PREWARM, status="queued", progress=0, dedupe_key=dedupe,
            payload={"context": context, "key": key, "video_id": submission.video_id,
                     "submission_annotation_id": annotation.id}).on_conflict_do_nothing(index_elements=["dedupe_key"]))
        dedupes.append(dedupe)
    jobs = {job.dedupe_key: job for job in db.query(BackgroundJob).filter(
        BackgroundJob.dedupe_key.in_(dedupes))} if dedupes else {}
    for dedupe in dedupes:
        job = jobs[dedupe]
        # A new publication may retry failed/reclaimed material, including a
        # reused canonical owner. Capacity-deferred rows are deliberately terminal.
        if job.status in {"failed", "cancelled"} or (job.status == "succeeded" and job.progress == 100):
            job.status, job.progress, job.error, job.attempts = "queued", 0, None, 0
            job.started_at = job.finished_at = None
        ids.append(job.id)
    return ids


def preparation(db, settings, rows) -> dict:
    ready = failed = deferred = pending = running = completed = 0
    batch = context_batch(db, rows)
    keys = [(s.video_id, cache_key(settings, frozen_context(db, a, s, c, batch))) for a, s, c in rows]
    dedupes = [f"context:{video_id}:{key}" for video_id, key in keys]
    jobs = {j.dedupe_key: j for j in db.query(BackgroundJob).filter(BackgroundJob.dedupe_key.in_(dedupes))} if dedupes else {}
    for (annotation, submission, clip), (_video_id, key), dedupe in zip(rows, keys, dedupes):
        if lookup(settings, submission.video_id, key) is not None:
            ready += 1
            continue
        job = jobs.get(dedupe)
        if job is not None:
            failed += job.status == "failed"
            deferred += job.status == "succeeded" and job.progress == 99
            pending += job.status == "queued"
            running += job.status == "running"
            completed += job.status == "succeeded"
    total = len(rows)
    state = ("empty" if not total else "ready" if ready == total else "preparing" if running
             else "pending" if pending else "partial" if ready or completed else "failed" if failed else "partial")
    return dict(state=state, total_count=total, ready_count=ready, failed_count=failed, deferred_count=deferred)
