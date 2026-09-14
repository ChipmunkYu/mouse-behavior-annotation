"""Behavior-level review authority and material-comparison helpers."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import (
    Annotation,
    BehaviorReviewDecision,
    Clip,
    FeedbackMark,
    Submission,
    SubmissionAnnotation,
    User,
    Video,
)


def material_values(annotation: Annotation) -> dict:
    return {
        "category_id": annotation.category_id,
        "start_frame": annotation.start_frame,
        "end_frame": annotation.end_frame,
        "confidence": annotation.confidence,
        "crop_region": annotation.crop_region,
        "mouse_ids": annotation.mouse_ids or [],
        "participant_roles": annotation.participant_roles or {},
    }


def material_digest(annotation: Annotation) -> str:
    payload = annotation.material_state or material_values(annotation)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def ensure_material_evidence(annotation: Annotation) -> None:
    if not annotation.material_state:
        annotation.material_state = material_values(annotation)
    if not annotation.material_digest:
        annotation.material_digest = material_digest(annotation)


def latest_decisions(db: Session, snapshot_ids: list[int]) -> dict[int, BehaviorReviewDecision]:
    if not snapshot_ids:
        return {}
    rows = (db.query(BehaviorReviewDecision)
            .filter(BehaviorReviewDecision.submission_annotation_id.in_(snapshot_ids))
            .order_by(BehaviorReviewDecision.sequence, BehaviorReviewDecision.id).all())
    return {row.submission_annotation_id: row for row in rows}


def feedback_marks(db: Session, snapshot_ids: list[int]) -> dict[int, FeedbackMark]:
    if not snapshot_ids:
        return {}
    rows = (db.query(FeedbackMark)
            .filter(FeedbackMark.submission_annotation_id.in_(snapshot_ids)).all())
    return {row.submission_annotation_id: row for row in rows}


def record_feedback_mark(db: Session, snapshot_id: int, user_id: int) -> None:
    """Atomically record the first '标记已修改' row; later marks are no-ops.

    The unique snapshot index decides the winner, so a duplicate/concurrent mark
    never raises IntegrityError and the first ``marked_by``/``marked_at`` survive.
    """
    db.execute(
        sqlite_insert(FeedbackMark)
        .values(submission_annotation_id=snapshot_id, marked_by=user_id,
                marked_at=datetime.utcnow())
        .on_conflict_do_nothing(index_elements=["submission_annotation_id"])
    )


def approved_snapshots(db: Session, submission: Submission) -> list[SubmissionAnnotation]:
    """Publish-time approved subset: latest decision is approved (carried included)."""
    snapshots = (db.query(SubmissionAnnotation)
                 .filter_by(submission_id=submission.id)
                 .order_by(SubmissionAnnotation.id).all())
    latest = latest_decisions(db, [row.id for row in snapshots])
    return [row for row in snapshots
            if latest.get(row.id) is not None and latest[row.id].status == "approved"]


# Canonical asset equivalence is expressed only with existing immutable fields; no
# persistent content-hash column is introduced (see docs/计划/按行为发布片段设计.md 5.5).
_ASSET_SNAPSHOT_FIELDS = (
    "source_annotation_key", "source_material_revision", "material_digest",
    "start_frame", "end_frame", "crop_region", "category_name",
    "category_participant_mode", "role_definitions_snapshot",
    "participant_roles_snapshot", "mouse_ids", "confidence",
)
_ASSET_SUBMISSION_FIELDS = (
    "source_storage_key", "source_video_sha256", "source_file_size",
    "source_mtime_ns", "source_device", "source_inode",
)
# The export contract version is a code constant; snapshot schema_version is the stored proxy.
_ASSET_DETECTION_FIELDS = ("raw_digest", "state_digest", "metadata_digest", "schema_version")


def asset_equivalent(left: SubmissionAnnotation, right: SubmissionAnnotation) -> bool:
    """True when two submission snapshots would export identical content."""
    if left is None or right is None or left.submission is None or right.submission is None:
        return False
    if any(getattr(left, field) != getattr(right, field) for field in _ASSET_SNAPSHOT_FIELDS):
        return False
    if any(getattr(left.submission, field) != getattr(right.submission, field)
           for field in _ASSET_SUBMISSION_FIELDS):
        return False
    left_detection = left.submission.detection_snapshot
    right_detection = right.submission.detection_snapshot
    if left_detection is None or right_detection is None:
        return False
    return all(getattr(left_detection, field) == getattr(right_detection, field)
               for field in _ASSET_DETECTION_FIELDS)


def _carried_origin(db: Session, snapshot: SubmissionAnnotation) -> SubmissionAnnotation | None:
    """Follow the carry chain to the origin snapshot, if any."""
    decision = latest_decisions(db, [snapshot.id]).get(snapshot.id)
    if decision is None or decision.carried_from_decision_id is None:
        return None
    origin = db.get(BehaviorReviewDecision, decision.carried_from_decision_id)
    if origin is None:
        return None
    return db.get(SubmissionAnnotation, origin.submission_annotation_id)


def find_canonical_clip(db: Session, snapshot: SubmissionAnnotation) -> Clip | None:
    """Resolve an existing published Clip whose export content equals this snapshot.

    The carry chain is only a candidate hint; every candidate is validated with
    :func:`asset_equivalent` before reuse. Returns ``None`` when no asset matches.
    """
    origin = _carried_origin(db, snapshot)
    if origin is not None:
        origin_clip = db.query(Clip).filter_by(submission_annotation_id=origin.id).first()
        if origin_clip is not None and asset_equivalent(snapshot, origin):
            return origin_clip
    candidates = (
        db.query(Clip, SubmissionAnnotation)
        .join(SubmissionAnnotation, SubmissionAnnotation.id == Clip.submission_annotation_id)
        .filter(
            Clip.submission_annotation_id.is_not(None),
            SubmissionAnnotation.source_annotation_key == snapshot.source_annotation_key,
            SubmissionAnnotation.source_material_revision == snapshot.source_material_revision,
            SubmissionAnnotation.material_digest == snapshot.material_digest,
        )
        .order_by(Clip.id)
        .all()
    )
    for clip, candidate in candidates:
        if candidate.id == snapshot.id:
            return clip
    for clip, candidate in candidates:
        if asset_equivalent(snapshot, candidate):
            return clip
    return None


def current_final_approval(db: Session, video_id: int) -> Submission | None:
    current = (db.query(Submission).filter_by(video_id=video_id)
               .order_by(Submission.attempt_no.desc()).first())
    return current if current is not None and current.status == "approved" else None


def decision_comparison(snapshot: SubmissionAnnotation, live: Annotation | None,
                        *, current_detection_import_id: int | None = None) -> str:
    if snapshot.source_annotation_id is None or live is None:
        return "deleted"
    baseline_import_id = snapshot.submission.detection_snapshot.detection_import_id
    if current_detection_import_id is not None and current_detection_import_id != baseline_import_id:
        return "modified"
    if (live.material_digest or material_digest(live)) != snapshot.material_digest:
        return "modified"
    if live.material_revision > snapshot.source_material_revision:
        return "reverted"
    return "unchanged"


def locked_annotation_ids(db: Session, video_id: int) -> list[int]:
    current = (db.query(Submission).filter_by(video_id=video_id)
               .order_by(Submission.attempt_no.desc()).first())
    if current is None:
        return []
    snapshots = list(current.annotations)
    latest = latest_decisions(db, [row.id for row in snapshots])
    return sorted({row.source_annotation_key for row in snapshots
                   if row.source_annotation_id is not None
                   and latest.get(row.id) is not None and latest[row.id].status == "approved"})


def assert_annotation_unlocked(db: Session, video_id: int, annotation_id: int) -> None:
    if annotation_id in locked_annotation_ids(db, video_id):
        raise HTTPException(status_code=409, detail={
            "code": "approved_annotation_locked",
            "annotation_id": annotation_id,
            "message": "Approved annotations must be reopened before ordinary modification or deletion",
        })


def assert_video_not_final_approved(db: Session, video: Video) -> None:
    if current_final_approval(db, video.id) is not None:
        raise HTTPException(status_code=409, detail={
            "code": "approved_video_locked",
            "message": "Final approved videos must be explicitly reopened before modification",
        })


def rejected_blockers(db: Session, video_id: int, current_detection_import_id: int | None) -> list[dict]:
    rows = (db.query(BehaviorReviewDecision, SubmissionAnnotation)
            .join(SubmissionAnnotation, BehaviorReviewDecision.submission_annotation_id == SubmissionAnnotation.id)
            .join(Submission, SubmissionAnnotation.submission_id == Submission.id)
            .filter(Submission.video_id == video_id)
            .order_by(BehaviorReviewDecision.id).all())
    unresolved = {}
    for decision, snapshot in rows:
        if decision.status == "approved":
            # Approval resolves earlier feedback, even if that approval is later revoked.
            unresolved = {key: row for key, row in unresolved.items()
                          if row.source_annotation_key != snapshot.source_annotation_key}
        elif decision.status == "rejected":
            unresolved[snapshot.id] = snapshot
        else:
            unresolved.pop(snapshot.id, None)
    live_by_id = {row.id: row for row in db.query(Annotation).filter_by(video_id=video_id).all()}
    blocked = []
    for snapshot in unresolved.values():
        comparison = decision_comparison(snapshot, live_by_id.get(snapshot.source_annotation_key),
                                         current_detection_import_id=current_detection_import_id)
        if comparison in {"unchanged", "reverted"}:
            blocked.append({"submission_annotation_id": snapshot.id,
                            "source_annotation_id": snapshot.source_annotation_key,
                            "comparison": comparison})
    return blocked


def carry_approved_decisions(db: Session, submission: Submission) -> None:
    previous = (db.query(Submission).filter(Submission.video_id == submission.video_id,
                                            Submission.id != submission.id)
                .order_by(Submission.attempt_no.desc()).first())
    if previous is None or previous.detection_snapshot.detection_import_id != submission.detection_snapshot.detection_import_id:
        return
    old = {row.source_annotation_id: row for row in previous.annotations
           if row.source_annotation_id is not None}
    old_latest = latest_decisions(db, [row.id for row in old.values()])
    for snapshot in submission.annotations:
        prior = old.get(snapshot.source_annotation_key)
        decision = old_latest.get(prior.id) if prior else None
        if decision and decision.status == "approved" and prior.material_digest == snapshot.material_digest:
            submission.decision_revision += 1
            db.add(BehaviorReviewDecision(
                submission_annotation_id=snapshot.id, status="approved", feedback=decision.feedback,
                sequence=submission.decision_revision, reviewer_id=decision.reviewer_id,
                decided_at=datetime.utcnow(), origin="carried", carried_from_decision_id=decision.id,
            ))


def serialize_snapshot(snapshot: SubmissionAnnotation) -> dict:
    return {
        "id": snapshot.id, "source_annotation_id": snapshot.source_annotation_key if snapshot.source_annotation_key > 0 else None,
        "category_id": snapshot.category_id, "category_name": snapshot.category_name,
        "category_group": snapshot.category_group,
        "category_participant_mode": snapshot.category_participant_mode,
        "role_definitions": snapshot.role_definitions_snapshot or [],
        "participant_roles": snapshot.participant_roles_snapshot or {},
        "mouse_ids": snapshot.mouse_ids or [], "start_time": snapshot.start_time,
        "end_time": snapshot.end_time, "start_frame": snapshot.start_frame,
        "end_frame": snapshot.end_frame, "confidence": snapshot.confidence,
        "crop_region": snapshot.crop_region,
    }


def decision_dict(decision: BehaviorReviewDecision | None, users: dict[int, str]) -> dict:
    if decision is None:
        return {"status": "pending", "feedback": None, "decision_id": None, "sequence": None,
                "reviewer_id": None, "reviewer": None, "decided_at": None, "origin": None,
                "carried_from_decision_id": None}
    return {"status": decision.status, "feedback": decision.feedback, "decision_id": decision.id,
            "sequence": decision.sequence, "reviewer_id": decision.reviewer_id,
            "reviewer": users.get(decision.reviewer_id), "decided_at": decision.decided_at,
            "origin": decision.origin, "carried_from_decision_id": decision.carried_from_decision_id}


def reviewer_names(db: Session, decisions: list[BehaviorReviewDecision]) -> dict[int, str]:
    ids = {row.reviewer_id for row in decisions if row.reviewer_id is not None}
    return dict(db.query(User.id, User.username).filter(User.id.in_(ids)).all()) if ids else {}
