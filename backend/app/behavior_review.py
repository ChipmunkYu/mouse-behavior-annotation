"""Behavior-level review authority and material-comparison helpers."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import Annotation, BehaviorReviewDecision, Submission, SubmissionAnnotation, User, Video


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
