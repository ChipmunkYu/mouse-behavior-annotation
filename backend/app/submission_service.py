"""Authoritative immutable submission and detection-snapshot services."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path, PurePosixPath

from fastapi import HTTPException
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from .effective_detections import effective_detection_query
from .models import (
    Annotation, BehaviorCategory, DetectionImport, DetectionSnapshot,
    DetectionSnapshotState, DetectionStateOverride, RawDetection, Submission,
    SubmissionAnnotation, Video,
)
from .submission_media_plan import build_submission_media_plan
from .file_identity import FileIdentity, file_identity, hash_file_handle
from .integrity_canonical import (canonical_digest as _canonical_digest, canonical_rows_digest,
                                  validate_pose_metadata)

SNAPSHOT_SCHEMA_VERSION = 1


def _fault(_stage: str) -> None:
    """Test-only fault-injection seam; production implementation is intentionally empty."""


def validate_storage_key(value: str | None) -> str:
    if value is None or value != value.strip() or not value or "\\" in value or value.endswith("/"):
        raise HTTPException(status_code=409, detail="Video storage key is not a canonical relative key")
    path = PurePosixPath(value)
    parts = value.split("/")
    if path.is_absolute() or ":" in value or any(not p or p in {".", ".."} or p.strip() != p for p in parts):
        raise HTTPException(status_code=409, detail="Video storage key is not a canonical relative key")
    return value


def resolve_and_hash_source(settings, video: Video) -> tuple[str, str, FileIdentity]:
    key = validate_storage_key(video.storage_path)
    root = settings.videos_dir.resolve()
    source = (root / key).resolve()
    if source == root or not source.is_relative_to(root) or not source.is_file():
        raise HTTPException(status_code=409, detail="Submission source video is missing or outside storage")
    try:
        digest, identity = hash_file_handle(source)
    except OSError as exc:
        raise HTTPException(status_code=409, detail="Submission source changed while hashing")
    return key, digest, identity


def verify_source_identity(settings, key: str, expected: FileIdentity) -> None:
    root = settings.videos_dir.resolve()
    source = (root / validate_storage_key(key)).resolve()
    if source == root or not source.is_relative_to(root) or not source.is_file():
        raise HTTPException(status_code=409, detail="Submission source changed concurrently")
    if file_identity(source) != expected:
        raise HTTPException(status_code=409, detail="Submission source changed concurrently")


def _raw_digest(db: Session, import_id: int) -> str:
    rows = db.query(RawDetection).filter_by(detection_import_id=import_id).order_by(RawDetection.id).yield_per(500)
    return canonical_rows_digest(([raw.id, raw.frame_index, raw.frame_detection_index, raw.raw_track_id,
                                   raw.box, raw.keypoints, raw.detection_confidence, raw.class_id] for raw in rows))


def _state_digest(db: Session, import_id: int, snapshot_id: int | None = None) -> str:
    model = DetectionSnapshotState if snapshot_id is not None else DetectionStateOverride
    query = db.query(model.raw_detection_id, model.display_track_id, model.suppressed)
    query = query.filter(model.snapshot_id == snapshot_id) if snapshot_id is not None else query.filter(
        model.detection_import_id == import_id)
    return _canonical_digest([[r[0], r[1], bool(r[2])] for r in query.order_by(model.raw_detection_id)])


def _pose_metadata(settings, imp: DetectionImport) -> tuple[list, list]:
    if not imp.metadata_path:
        raise HTTPException(status_code=409, detail="Detection metadata path is missing")
    root = settings.detection_imports_dir.resolve()
    path = (root / validate_storage_key(imp.metadata_path)).resolve()
    if path == root or not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=409, detail="Detection metadata file is missing or outside storage")
    try:
        raw = path.read_bytes()
        if not imp.metadata_sha256 or hashlib.sha256(raw).hexdigest() != imp.metadata_sha256:
            raise HTTPException(status_code=409, detail="Detection metadata SHA-256 mismatch")
        content = json.loads(raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=409, detail="Detection metadata cannot be verified") from exc
    names = content.get("keypoint_names")
    edges = content.get("skeleton_edges")
    if edges is None:
        edges = content.get("skeleton_edges_0based")
    try:
        names, edges = validate_pose_metadata(names, edges)
    except ValueError:
        raise HTTPException(status_code=409, detail="Detection pose metadata is incomplete")
    return names, edges


def _snapshot_header(db: Session, settings, imp: DetectionImport) -> dict:
    raw_count = db.query(func.count(RawDetection.id)).filter(RawDetection.detection_import_id == imp.id).scalar()
    if imp.detection_count is None or raw_count != imp.detection_count:
        raise HTTPException(status_code=409, detail="Raw detection count does not match immutable import metadata")
    override_count = db.query(func.count(DetectionStateOverride.raw_detection_id)).filter(
        DetectionStateOverride.detection_import_id == imp.id
    ).scalar()
    if imp.fps is None or imp.fps <= 0 or not imp.width or not imp.height or imp.frame_count is None:
        raise HTTPException(status_code=409, detail="Detection import media metadata is incomplete")
    names, edges = _pose_metadata(settings, imp)
    metadata = dict(schema_version=SNAPSHOT_SCHEMA_VERSION, fps=imp.fps, width=imp.width,
                    height=imp.height, frame_count=imp.frame_count,
                    keypoint_names=names, skeleton_edges=edges)
    return dict(
        detection_import_id=imp.id, source_edit_version=imp.edit_version,
        raw_detection_count=raw_count, override_count=override_count,
        **metadata, raw_digest=_raw_digest(db, imp.id), state_digest=_state_digest(db, imp.id),
        metadata_digest=_canonical_digest(metadata),
    )


def _assert_snapshot_matches(db: Session, snapshot: DetectionSnapshot, expected: dict) -> None:
    fields = tuple(expected)
    if any(getattr(snapshot, field) != expected[field] for field in fields):
        raise HTTPException(status_code=409, detail="Existing detection snapshot header is inconsistent")
    states = db.query(func.count(DetectionSnapshotState.raw_detection_id)).filter(
        DetectionSnapshotState.snapshot_id == snapshot.id
    ).scalar()
    if states != snapshot.override_count:
        raise HTTPException(status_code=409, detail="Existing detection snapshot state count is inconsistent")
    if _state_digest(db, snapshot.detection_import_id, snapshot.id) != snapshot.state_digest:
        raise HTTPException(status_code=409, detail="Existing detection snapshot state digest is inconsistent")


def get_or_create_snapshot(db: Session, settings, imp: DetectionImport) -> DetectionSnapshot:
    expected = _snapshot_header(db, settings, imp)
    existing = db.query(DetectionSnapshot).filter_by(
        detection_import_id=imp.id, source_edit_version=imp.edit_version
    ).first()
    if existing is not None:
        _assert_snapshot_matches(db, existing, expected)
        return existing
    snapshot = DetectionSnapshot(**expected)
    db.add(snapshot)
    db.flush()
    _fault("snapshot_header")
    db.execute(
            insert(DetectionSnapshotState).from_select(
                ["snapshot_id", "raw_detection_id", "detection_import_id", "display_track_id", "suppressed"],
                select(
                    func.cast(snapshot.id, DetectionSnapshotState.snapshot_id.type),
                    DetectionStateOverride.raw_detection_id,
                    DetectionStateOverride.detection_import_id,
                    DetectionStateOverride.display_track_id,
                    DetectionStateOverride.suppressed,
                ).where(DetectionStateOverride.detection_import_id == imp.id),
            )
        )
    db.flush()
    _assert_snapshot_matches(db, snapshot, expected)
    return snapshot


def validate_snapshot_integrity(db: Session, snapshot: DetectionSnapshot) -> DetectionImport:
    imp = db.get(DetectionImport, snapshot.detection_import_id)
    if imp is None:
        raise HTTPException(status_code=409, detail="Detection snapshot import is missing")
    raw_count = db.query(func.count(RawDetection.id)).filter(RawDetection.detection_import_id == imp.id).scalar()
    state_count = db.query(func.count(DetectionSnapshotState.raw_detection_id)).filter(
        DetectionSnapshotState.snapshot_id == snapshot.id,
        DetectionSnapshotState.detection_import_id == imp.id,
    ).scalar()
    cross_count = db.query(func.count(DetectionSnapshotState.raw_detection_id)).filter(
        DetectionSnapshotState.snapshot_id == snapshot.id,
        DetectionSnapshotState.detection_import_id != imp.id,
    ).scalar()
    metadata = dict(schema_version=snapshot.schema_version, fps=snapshot.fps, width=snapshot.width,
                    height=snapshot.height, frame_count=snapshot.frame_count,
                    keypoint_names=snapshot.keypoint_names, skeleton_edges=snapshot.skeleton_edges)
    if (raw_count != snapshot.raw_detection_count or state_count != snapshot.override_count or cross_count
            or _raw_digest(db, imp.id) != snapshot.raw_digest
            or _state_digest(db, imp.id, snapshot.id) != snapshot.state_digest
            or _canonical_digest(metadata) != snapshot.metadata_digest):
        raise HTTPException(status_code=409, detail="Detection snapshot baseline integrity check failed")
    return imp


def snapshot_detection_query(db: Session, snapshot: DetectionSnapshot, **filters):
    validate_snapshot_integrity(db, snapshot)
    return effective_detection_query(
        db, snapshot.detection_import_id, snapshot_id=snapshot.id, **filters
    )


def _validate_crop(crop, width: int, height: int) -> bool:
    if crop is None:
        return True
    if not isinstance(crop, dict) or set(crop) != {"x", "y", "w", "h"}:
        return False
    values = [crop[k] for k in ("x", "y", "w", "h")]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        return False
    x, y, w, h = values
    return x >= 0 and y >= 0 and w > 0 and h > 0 and x + w <= width and y + h <= height


def validate_annotations(db: Session, video: Video, imp: DetectionImport) -> list[tuple[Annotation, BehaviorCategory]]:
    rows = db.query(Annotation, BehaviorCategory).outerjoin(
        BehaviorCategory, BehaviorCategory.id == Annotation.category_id
    ).filter(Annotation.video_id == video.id).order_by(Annotation.id).all()
    if not rows:
        raise HTTPException(status_code=400, detail="At least one annotation is required before submitting")
    needs_ids = sum(1 for ann, _category in rows if not ann.mouse_ids)
    if needs_ids:
        raise HTTPException(
            status_code=400,
            detail=f"{needs_ids} annotation(s) still need valid mouse_ids before submission",
        )
    invalid = []
    effective = effective_detection_query(db, imp.id, include_suppressed=True).all()
    all_tracks = {row.display_track_id for row in effective}
    active_frames: dict[int, set[int]] = {}
    for row in effective:
        if not row.suppressed:
            active_frames.setdefault(row.display_track_id, set()).add(row.RawDetection.frame_index)
    for ann, category in rows:
        reason = None
        if category is None or category.project_id != video.project_id:
            reason = "Category does not belong to the video's project"
        elif ann.video_id != video.id:
            reason = "Annotation does not belong to the video"
        elif ann.confidence not in {"certain", "uncertain", "occluded"}:
            reason = "Invalid confidence"
        elif ann.start_time < 0 or ann.end_time <= ann.start_time or ann.start_frame < 0 or ann.end_frame < ann.start_frame:
            reason = "Invalid time/frame range"
        elif imp.frame_count is not None and ann.end_frame >= imp.frame_count:
            reason = "Frame range exceeds detection metadata"
        elif video.duration is not None and ann.end_time > video.duration + 1e-6:
            reason = "Time range exceeds source media"
        elif not _validate_crop(ann.crop_region, imp.width, imp.height):
            reason = "Invalid crop region"
        if reason is None:
            try:
                build_submission_media_plan(
                    start_time=ann.start_time, end_time=ann.end_time,
                    start_frame=ann.start_frame, end_frame=ann.end_frame, fps=imp.fps,
                    frame_count=imp.frame_count, width=imp.width, height=imp.height,
                    crop_region=ann.crop_region,
                )
            except ValueError as exc:
                reason = str(exc)
        ids = ann.mouse_ids if isinstance(ann.mouse_ids, list) else []
        if reason is None and (not ids or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in ids)
                               or ids != sorted(set(ids))):
            reason = "mouse_ids must be a non-empty sorted unique integer list"
        if reason is None and not (category.mouse_count_min <= len(ids)
                                   and (category.mouse_count_max is None or len(ids) <= category.mouse_count_max)):
            reason = f"Category '{category.name}' participant count is invalid"
        if reason is None:
            for track_id in ids:
                if not any(ann.start_frame <= frame <= ann.end_frame
                           for frame in active_frames.get(track_id, ())):
                    exists = track_id in all_tracks
                    reason = (f"Track ID {track_id} has no unsuppressed detections in frame range "
                              f"[{ann.start_frame}, {ann.end_frame}]" if exists else
                              f"Track ID {track_id} is not an active corrected track")
                    break
        if reason:
            invalid.append({"annotation_id": ann.id, "reason": reason})
    if invalid:
        raise HTTPException(status_code=400, detail={
            "message": "Some annotations are invalid and need revalidation",
            "invalid_annotations": invalid,
        })
    return rows


def create_submission(db: Session, settings, video: Video, imp: DetectionImport, submitter_id: int,
                      *, source_identity: tuple[str, str, FileIdentity]) -> Submission:
    if imp.video_id != video.id or not imp.active:
        raise HTTPException(status_code=409, detail="Active detection import does not belong to video")
    if db.query(Submission.id).filter_by(video_id=video.id, status="submitted").first():
        raise HTTPException(status_code=409, detail="Video already has a submitted attempt")
    rows = validate_annotations(db, video, imp)
    key, digest, identity = source_identity
    if video.storage_path != key:
        raise HTTPException(status_code=409, detail="Video storage key changed concurrently")
    verify_source_identity(settings, key, identity)
    snapshot = get_or_create_snapshot(db, settings, imp)
    if snapshot.detection_import.video_id != video.id:
        raise HTTPException(status_code=409, detail="Detection snapshot does not belong to video")
    attempt = (db.query(func.max(Submission.attempt_no)).filter(Submission.video_id == video.id).scalar() or 0) + 1
    submission = Submission(
        video_id=video.id, detection_snapshot_id=snapshot.id, attempt_no=attempt,
        source_annotation_version=video.annotation_revision,
        source_media_revision=video.media_revision, source_video_filename=video.filename,
        source_storage_key=key, source_video_sha256=digest,
        source_file_size=identity.size, source_mtime_ns=identity.mtime_ns,
        source_device=identity.device, source_inode=identity.inode, status="submitted",
        submitted_by=submitter_id, submitted_at=datetime.utcnow(), legacy_backfill=False,
    )
    db.add(submission)
    db.flush()
    _fault("submission")
    db.execute(insert(SubmissionAnnotation), [dict(
        submission_id=submission.id, source_annotation_id=ann.id,
        category_id=category.id, category_name=category.name,
        start_time=ann.start_time, end_time=ann.end_time,
        start_frame=ann.start_frame, end_frame=ann.end_frame,
        confidence=ann.confidence, crop_region=ann.crop_region,
        mouse_ids=list(ann.mouse_ids),
    ) for ann, category in rows])
    _fault("submission_annotations")
    video.workflow_status = "submitted"
    video.submitted_at = submission.submitted_at
    video.approved_at = None
    video.approved_by = None
    for ann, _category in rows:
        ann.review_status = "pending"
        ann.reviewer_id = None
        ann.mouse_id_status = "valid"
        ann.detection_import_revision = imp.revision
        ann.identity_revision = imp.edit_version
    db.flush()
    return submission


def cleanup_orphan_snapshot(db: Session, snapshot_id: int) -> bool:
    snapshot = db.get(DetectionSnapshot, snapshot_id)
    if snapshot is None:
        return False
    if db.query(Submission.id).filter(Submission.detection_snapshot_id == snapshot.id).first():
        raise HTTPException(status_code=409, detail="Referenced detection snapshots cannot be cleaned")
    db.query(DetectionSnapshotState).filter_by(snapshot_id=snapshot.id).delete(synchronize_session=False)
    db.delete(snapshot)
    return True


def guard_raw_baseline_deletion(db: Session, *, detection_import_id: int) -> None:
    """Reject deletion/replacement cleanup of a raw baseline referenced by a snapshot."""
    if db.query(DetectionSnapshot.id).filter_by(detection_import_id=detection_import_id).first():
        raise HTTPException(status_code=409, detail="Snapshot-referenced raw detections cannot be deleted")


def delete_raw_baseline(db: Session, *, detection_import_id: int) -> int:
    """Shared guarded primitive for any current/future RawDetection cleanup path."""
    guard_raw_baseline_deletion(db, detection_import_id=detection_import_id)
    return db.query(RawDetection).filter_by(detection_import_id=detection_import_id).delete(
        synchronize_session=False
    )
