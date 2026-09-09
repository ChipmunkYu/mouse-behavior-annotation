"""Submission-authoritative submit, withdraw, review queue/history and decision API."""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import project_access
from ..media_jobs import enqueue_submission_media
from ..models import (
    Annotation, BehaviorReviewDecision, BehaviorReviewReopen, DetectionImport,
    DraftIdentityEdit, Review, Submission, SubmissionAnnotation, Video,
)
from ..permissions import can_review, require_editor, require_reviewer
from ..schemas import (BehaviorDecisionIn, BehaviorReopenIn, BehaviorReviewStateOut,
                       ReviewCreate, ReviewOut, ReviewSubmissionContextIn, VideoOut)
from ..behavior_review import current_final_approval
from ..submission_service import (create_submission, resolve_and_hash_source,
                                  validate_snapshot_integrity)
from ..video_write_gate import video_write_gate
from ..video_playback import public_video
from ..behavior_review import (decision_comparison, decision_dict, latest_decisions,
                               locked_annotation_ids, reviewer_names, serialize_snapshot)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["reviews"])


def _snapshot_annotation(copy: SubmissionAnnotation) -> dict:
    return serialize_snapshot(copy)


def _now() -> datetime:
    return datetime.utcnow()


def _get_video(db: Session, project_id: int, video_id: int) -> Video:
    video = db.get(Video, video_id)
    if video is None or video.project_id != project_id:
        raise HTTPException(status_code=404, detail="Video not found in this project")
    return video


def _to_review_out(review: Review) -> ReviewOut:
    return ReviewOut(
        id=review.id, project_id=review.project_id, video_id=review.video_id,
        reviewer_id=review.reviewer_id, result=review.result, comment=review.comment,
        annotation_revision=review.annotation_revision,
        detection_import_revision=review.detection_import_revision,
        identity_revision=review.identity_revision, created_at=review.created_at,
        reviewer=review.reviewer.username if review.reviewer else None,
        submission_id=review.submission_id,
        submission_annotations=[_snapshot_annotation(item) for item in review.submission.annotations]
        if review.submission else [],
    )


def _annotation_out(annotation: Annotation) -> dict:
    return {
        "id": annotation.id, "video_id": annotation.video_id,
        "annotator_id": annotation.annotator_id, "category_id": annotation.category_id,
        "start_time": annotation.start_time, "end_time": annotation.end_time,
        "start_frame": annotation.start_frame, "end_frame": annotation.end_frame,
        "confidence": annotation.confidence, "review_status": annotation.review_status,
        "crop_region": annotation.crop_region, "mouse_ids": annotation.mouse_ids or [],
        "participant_roles": annotation.participant_roles or {},
        "participant_status": annotation.participant_status,
        "mouse_id_status": annotation.mouse_id_status,
        "detection_import_revision": annotation.detection_import_revision,
        "identity_revision": annotation.identity_revision,
        "created_at": annotation.created_at, "updated_at": annotation.updated_at,
        "annotator": annotation.annotator.username if annotation.annotator else None,
        "category_name": annotation.category.name if annotation.category else None,
    }


def _review_state(db: Session, video: Video, membership) -> dict:
    submission = (db.query(Submission).filter_by(video_id=video.id, status="submitted").first()
                  or db.query(Submission).filter_by(video_id=video.id)
                  .order_by(Submission.attempt_no.desc()).first())
    locks = locked_annotation_ids(db, video.id)
    if submission is None:
        return {"submission_id": None, "attempt_no": None, "submission_status": None,
                "decision_revision": 0, "counts": {"pending": 0, "approved": 0, "rejected": 0},
                "can_finalize_approval": False, "annotations": [], "feedback_items": [],
                "locked_annotation_ids": locks, "can_reopen": False}
    snapshots = list(submission.annotations)
    decisions = latest_decisions(db, [row.id for row in snapshots])
    names = reviewer_names(db, list(decisions.values()))
    annotations = [{**serialize_snapshot(row), "decision": decision_dict(decisions.get(row.id), names)}
                   for row in snapshots]
    statuses = [decisions[row.id].status if row.id in decisions else "pending" for row in snapshots]
    counts = {name: statuses.count(name) for name in ("pending", "approved", "rejected")}

    rejected_submission = (submission if submission.status == "rejected" else
        db.query(Submission).filter_by(video_id=video.id, status="rejected")
        .order_by(Submission.attempt_no.desc()).first())
    feedback_items = []
    if rejected_submission is not None:
        rejected_snapshots = list(rejected_submission.annotations)
        rejected_latest = latest_decisions(db, [row.id for row in rejected_snapshots])
        rejected_names = reviewer_names(db, list(rejected_latest.values()))
        live = {row.id: row for row in db.query(Annotation).filter_by(video_id=video.id).all()}
        current_import = db.query(DetectionImport).filter_by(video_id=video.id, active=True).first()
        for row in rejected_snapshots:
            decision = rejected_latest.get(row.id)
            if decision is None or decision.status != "rejected":
                continue
            current = live.get(row.source_annotation_id)
            feedback_items.append({
                "submission_annotation_id": row.id,
                "source_annotation_id": row.source_annotation_key,
                "comparison": decision_comparison(row, current,
                    current_detection_import_id=current_import.id if current_import else None),
                "feedback": decision.feedback, "baseline": serialize_snapshot(row),
                "current": _annotation_out(current) if current else None,
                "reviewer": rejected_names.get(decision.reviewer_id),
                "decided_at": decision.decided_at,
            })
    return {
        "submission_id": submission.id, "attempt_no": submission.attempt_no,
        "submission_status": submission.status, "decision_revision": submission.decision_revision,
        "counts": counts,
        "can_finalize_approval": submission.status == "submitted" and can_review(membership)
                                 and bool(snapshots) and counts["approved"] == len(snapshots),
        "annotations": annotations, "feedback_items": feedback_items,
        "locked_annotation_ids": locks,
        "can_reopen": submission.status == "approved" and can_review(membership),
    }


@router.get("/api/projects/{project_id}/videos/{video_id}/review-state",
            response_model=BehaviorReviewStateOut)
def behavior_review_state(project_id: int, video_id: int, access: tuple = Depends(project_access),
                          db: Session = Depends(get_db)) -> dict:
    return _review_state(db, _get_video(db, project_id, video_id), access[1])


@router.put("/api/projects/{project_id}/videos/{video_id}/submissions/{submission_id}/annotations/{snapshot_id}/decision",
            response_model=BehaviorReviewStateOut)
def put_behavior_decision(project_id: int, video_id: int, submission_id: int, snapshot_id: int,
                          body: BehaviorDecisionIn, request: Request,
                          access: tuple = Depends(project_access), db: Session = Depends(get_db)) -> dict:
    membership = access[1]
    require_reviewer(membership)
    _get_video(db, project_id, video_id)
    with video_write_gate(db, project_id=project_id, video_id=video_id, allow_submitted=True,
                          operation_gate=request.app.state.video_operation_gate) as state:
        submission = db.get(Submission, submission_id)
        snapshot = db.get(SubmissionAnnotation, snapshot_id)
        if submission is None or submission.video_id != video_id or snapshot is None or snapshot.submission_id != submission_id:
            raise HTTPException(status_code=404, detail="Submission annotation snapshot not found")
        latest = db.query(Submission).filter_by(video_id=video_id).order_by(Submission.attempt_no.desc()).first()
        if latest.id != submission.id or not (submission.status == "submitted" or
                (submission.status in {"rejected", "withdrawn"} and body.status == "pending")):
            raise HTTPException(status_code=409, detail="Only the submitted attempt accepts decisions; reopen a final approval first")
        if submission.decision_revision != body.expected_decision_revision:
            raise HTTPException(status_code=409, detail={"code": "stale_decision_revision",
                "expected": submission.decision_revision, "received": body.expected_decision_revision})
        submission.decision_revision += 1
        feedback = body.feedback.strip() if body.feedback is not None else None
        decision = BehaviorReviewDecision(
            submission_annotation_id=snapshot.id, status=body.status, feedback=feedback,
            sequence=submission.decision_revision, reviewer_id=membership.user_id,
            decided_at=_now(), origin="manual",
        )
        db.add(decision)
        live = db.get(Annotation, snapshot.source_annotation_id) if snapshot.source_annotation_id is not None else None
        if live is not None and live.video_id == video_id:
            live.review_status = body.status
            live.reviewer_id = membership.user_id if body.status != "pending" else None
        db.commit()
        return _review_state(db, state.video, membership)


@router.post("/api/projects/{project_id}/videos/{video_id}/submissions/{submission_id}/reopen",
             response_model=BehaviorReviewStateOut)
def reopen_behavior_review(project_id: int, video_id: int, submission_id: int,
                           body: BehaviorReopenIn, request: Request,
                           access: tuple = Depends(project_access), db: Session = Depends(get_db)) -> dict:
    membership = access[1]
    require_reviewer(membership)
    _get_video(db, project_id, video_id)
    with video_write_gate(db, project_id=project_id, video_id=video_id, allow_submitted=True,
                          operation_gate=request.app.state.video_operation_gate) as state:
        submission = db.get(Submission, submission_id)
        latest = db.query(Submission).filter_by(video_id=video_id).order_by(Submission.attempt_no.desc()).first()
        if submission is None or latest is None or submission.id != latest.id or submission.status != "approved":
            raise HTTPException(status_code=409, detail="Only the latest approved attempt can be reopened")
        submission.status = "superseded"
        db.add(BehaviorReviewReopen(submission_id=submission.id, actor_id=membership.user_id,
                                    reason=(body.reason or "").strip() or None, created_at=_now()))
        decisions = latest_decisions(db, [row.id for row in submission.annotations])
        for snapshot in submission.annotations:
            if decisions.get(snapshot.id) and decisions[snapshot.id].status == "approved":
                submission.decision_revision += 1
                db.add(BehaviorReviewDecision(submission_annotation_id=snapshot.id, status="pending",
                    feedback=None, sequence=submission.decision_revision, reviewer_id=membership.user_id,
                    decided_at=_now(), origin="reopen"))
                live = db.get(Annotation, snapshot.source_annotation_id) if snapshot.source_annotation_id is not None else None
                if live is not None:
                    live.review_status, live.reviewer_id = "pending", None
        state.video.workflow_status = "draft"
        state.video.submitted_at = None
        state.video.approved_at = None
        state.video.approved_by = None
        db.commit()
        return _review_state(db, state.video, membership)


@router.post("/api/projects/{project_id}/videos/{video_id}/submit", response_model=VideoOut)
def submit_video(project_id: int, video_id: int, request: Request,
                 body: ReviewSubmissionContextIn | None = None,
                 access: tuple = Depends(project_access), db: Session = Depends(get_db)) -> Video:
    membership = access[1]
    require_editor(membership, "Only active project members can submit")
    observed = _get_video(db, project_id, video_id)
    observed_import = db.query(DetectionImport).filter_by(video_id=video_id, active=True).first()
    if observed_import is None:
        raise HTTPException(status_code=400, detail="No detection import is active for this video")
    source_identity = resolve_and_hash_source(request.app.state.settings, observed)
    with video_write_gate(
        db, project_id=project_id, video_id=video_id, require_active_import=True,
        operation_gate=request.app.state.video_operation_gate,
        expected_active_import_id=observed_import.id if observed_import else None,
        expected_detection_revision=observed.detection_import_revision,
        expected_edit_version=observed_import.edit_version if observed_import else None,
        expected_annotation_revision=observed.annotation_revision,
        expected_media_revision=observed.media_revision,
        expected_storage_path=source_identity[0],
    ) as state:
        video, imp = state.video, state.detection_import
        if video.identity_revision != imp.edit_version:
            raise HTTPException(status_code=400, detail=(
                f"Video identity revision projection is stale: video={video.identity_revision}, import={imp.edit_version}"
            ))
        if body is not None and (body.expected_submission_id is not None
                                 or body.expected_decision_revision is not None):
            previous = (db.query(Submission).filter_by(video_id=video.id)
                        .order_by(Submission.attempt_no.desc()).first())
            if (previous is None or (body.expected_submission_id is not None
                    and previous.id != body.expected_submission_id)):
                raise HTTPException(status_code=409, detail={"code": "stale_submission"})
            if (body.expected_decision_revision is not None
                    and previous.decision_revision != body.expected_decision_revision):
                raise HTTPException(status_code=409, detail={"code": "stale_decision_revision",
                    "expected": previous.decision_revision,
                    "received": body.expected_decision_revision})
        if current_final_approval(db, video.id) is not None:
            raise HTTPException(status_code=409, detail={"code": "approved_submission_requires_reopen",
                "message": "Reopen the final approval before submitting another attempt"})
        create_submission(db, request.app.state.settings, video, imp, membership.user_id,
                          source_identity=source_identity)
        db.commit()
        db.refresh(video)
        return public_video(video, request.app.state.settings)


@router.post("/api/projects/{project_id}/videos/{video_id}/withdraw", response_model=VideoOut)
def withdraw_video(project_id: int, video_id: int, request: Request,
                   access: tuple = Depends(project_access),
                   db: Session = Depends(get_db)) -> Video:
    membership = access[1]
    observed = _get_video(db, project_id, video_id)
    observed_was_submitted = observed.workflow_status == "submitted"
    observed_import = db.query(DetectionImport).filter_by(video_id=video_id, active=True).first()
    with video_write_gate(
        db, project_id=project_id, video_id=video_id, allow_submitted=True,
        operation_gate=request.app.state.video_operation_gate,
        expected_active_import_id=observed_import.id if observed_import else None,
        expected_detection_revision=observed.detection_import_revision,
        expected_edit_version=observed_import.edit_version if observed_import else None,
        expected_annotation_revision=observed.annotation_revision,
    ) as state:
        submission = db.query(Submission).filter_by(video_id=video_id, status="submitted").one_or_none()
        if submission is None:
            raise HTTPException(status_code=409, detail="Video has no submitted attempt to withdraw")
        require_editor(membership, "Only active project members can withdraw")
        if submission.review is not None:
            raise HTTPException(status_code=409, detail="A reviewed submission cannot be withdrawn")
        changed = db.query(Submission).filter(
            Submission.id == submission.id, Submission.status == "submitted"
        ).update({"status": "withdrawn"}, synchronize_session=False)
        if changed != 1:
            raise HTTPException(status_code=409, detail="Submission state changed concurrently")
        state.video.workflow_status = "draft"
        state.video.submitted_at = None
        db.commit(); db.refresh(state.video)
        return public_video(state.video, request.app.state.settings)


@router.get("/api/projects/{project_id}/reviews/queue", response_model=list[VideoOut])
def review_queue(project_id: int, request: Request, access: tuple = Depends(project_access),
                 db: Session = Depends(get_db)) -> list[dict]:
    require_reviewer(access[1], "Review permission is required to view the review queue")
    rows = db.query(Video, Submission).join(Submission, Submission.video_id == Video.id).filter(
        Video.project_id == project_id, Submission.status == "submitted"
    ).order_by(Submission.submitted_at.desc(), Submission.id.desc()).all()
    return [public_video(
                video, request.app.state.settings,
                submission_annotations=[_snapshot_annotation(item) for item in submission.annotations],
            )
            for video, submission in rows]


@router.get("/api/projects/{project_id}/videos/{video_id}/reviews", response_model=list[ReviewOut])
def review_history(project_id: int, video_id: int, access: tuple = Depends(project_access),
                   db: Session = Depends(get_db)) -> list[ReviewOut]:
    _get_video(db, project_id, video_id)
    rows = db.query(Review).filter(Review.video_id == video_id).order_by(Review.created_at, Review.id).all()
    return [_to_review_out(row) for row in rows]


@router.post("/api/projects/{project_id}/videos/{video_id}/review", response_model=ReviewOut)
def create_review(project_id: int, video_id: int, body: ReviewCreate, request: Request,
                  access: tuple = Depends(project_access), db: Session = Depends(get_db)) -> ReviewOut:
    membership = access[1]
    require_reviewer(membership)
    observed = _get_video(db, project_id, video_id)
    observed_was_submitted = observed.workflow_status == "submitted"
    observed_import = db.query(DetectionImport).filter_by(video_id=video_id, active=True).first()
    job_id = None
    with video_write_gate(
        db, project_id=project_id, video_id=video_id, allow_submitted=True,
        operation_gate=request.app.state.video_operation_gate,
        expected_active_import_id=observed_import.id if observed_import else None,
        expected_detection_revision=observed.detection_import_revision,
        expected_edit_version=observed_import.edit_version if observed_import else None,
        expected_annotation_revision=observed.annotation_revision,
    ) as state:
        submission = db.query(Submission).filter_by(video_id=video_id, status="submitted").one_or_none()
        if submission is None:
            if observed_was_submitted:
                raise HTTPException(status_code=409, detail="Submission state changed concurrently")
            raise HTTPException(status_code=400, detail="Only submitted videos can be reviewed")
        if submission.review is not None:
            raise HTTPException(status_code=409, detail="Submission already has a review")
        if body.expected_submission_id != submission.id:
            raise HTTPException(status_code=409, detail={"code": "stale_submission",
                "expected": submission.id, "received": body.expected_submission_id})
        if body.expected_decision_revision != submission.decision_revision:
            raise HTTPException(status_code=409, detail={"code": "stale_decision_revision",
                "expected": submission.decision_revision,
                "received": body.expected_decision_revision})
        imp = validate_snapshot_integrity(db, submission.detection_snapshot)
        if imp.video_id != video_id:
            raise HTTPException(status_code=409, detail="Submission snapshot belongs to another video")
        copies = db.query(SubmissionAnnotation).filter_by(submission_id=submission.id).all()
        if not copies:
            raise HTTPException(status_code=409, detail="Submission annotation snapshot is empty")
        copy_decisions = latest_decisions(db, [copy.id for copy in copies])
        if body.result == "approved" and any(
                copy.id not in copy_decisions or copy_decisions[copy.id].status != "approved"
                for copy in copies):
            raise HTTPException(status_code=409, detail={
                "code": "behavior_decisions_incomplete",
                "message": "Every behavior snapshot must be approved before final video approval",
            })
        now = _now()
        if body.result == "approved":
            db.query(Submission).filter(
                Submission.video_id == video_id, Submission.status == "approved",
                Submission.id != submission.id,
            ).update({"status": "superseded"}, synchronize_session=False)
        changed = db.query(Submission).filter(
            Submission.id == submission.id, Submission.status == "submitted"
        ).update({"status": body.result, "decided_at": now}, synchronize_session=False)
        if changed != 1:
            raise HTTPException(status_code=409, detail="Submission state changed concurrently")
        review = Review(
            project_id=project_id, video_id=video_id, submission_id=submission.id,
            reviewer_id=membership.user_id, result=body.result, comment=body.comment,
            annotation_revision=submission.source_annotation_version,
            detection_import_revision=imp.revision,
            identity_revision=submission.detection_snapshot.source_edit_version,
            created_at=now,
        )
        db.add(review)
        state.video.workflow_status = body.result
        state.video.approved_at = now if body.result == "approved" else None
        state.video.approved_by = membership.user_id if body.result == "approved" else None
        # Compatibility projection only; per-snapshot decisions remain authoritative. A video-level
        # rejection never invents individual rejections for pending historical rows.
        for copy in copies:
            decision = copy_decisions.get(copy.id)
            live = db.get(Annotation, copy.source_annotation_id) if copy.source_annotation_id is not None else None
            if live is not None and live.video_id == video_id and decision is not None:
                live.review_status = decision.status
                live.reviewer_id = decision.reviewer_id if decision.status != "pending" else None
        if body.result == "approved":
            db.query(DraftIdentityEdit).filter_by(detection_import_id=imp.id).delete(synchronize_session=False)
            db.flush()
            job = enqueue_submission_media(db, submission)
            job_id = job.id
        db.commit(); db.refresh(review)
        result = _to_review_out(review)
    if job_id is not None:
        try:
            request.app.state.media_worker.schedule(job_id)
        except Exception:
            logger.exception("Schedule immutable media job %s failed; queued row remains recoverable", job_id)
    return result
