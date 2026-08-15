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
    Annotation, DetectionImport, DraftIdentityEdit, Review, Submission,
    SubmissionAnnotation, Video,
)
from ..permissions import require_editor, require_reviewer
from ..schemas import ReviewCreate, ReviewOut, VideoOut
from ..submission_service import (create_submission, resolve_and_hash_source,
                                  validate_snapshot_integrity)
from ..video_write_gate import video_write_gate

logger = logging.getLogger(__name__)
router = APIRouter(tags=["reviews"])


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
    )


@router.post("/api/projects/{project_id}/videos/{video_id}/submit", response_model=VideoOut)
def submit_video(project_id: int, video_id: int, request: Request,
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
        current_approved = db.query(Submission).filter_by(video_id=video.id, status="approved").first()
        if current_approved and (
            current_approved.source_annotation_version == video.annotation_revision
            and current_approved.source_media_revision == video.media_revision
            and current_approved.detection_snapshot.detection_import_id == imp.id
            and current_approved.detection_snapshot.detection_import.revision == imp.revision
            and current_approved.detection_snapshot.source_edit_version == imp.edit_version
            and current_approved.source_storage_key == source_identity[0]
            and current_approved.source_video_sha256 == source_identity[1]
            and (current_approved.source_file_size, current_approved.source_mtime_ns,
                 current_approved.source_device, current_approved.source_inode)
                == (source_identity[2].size, source_identity[2].mtime_ns,
                    source_identity[2].device, source_identity[2].inode)
        ):
            raise HTTPException(status_code=400, detail="Video is already approved with no draft changes")
        create_submission(db, request.app.state.settings, video, imp, membership.user_id,
                          source_identity=source_identity)
        db.commit()
        db.refresh(video)
        return video


@router.post("/api/projects/{project_id}/videos/{video_id}/withdraw", response_model=VideoOut)
def withdraw_video(project_id: int, video_id: int, access: tuple = Depends(project_access),
                   db: Session = Depends(get_db)) -> Video:
    membership = access[1]
    observed = _get_video(db, project_id, video_id)
    observed_was_submitted = observed.workflow_status == "submitted"
    observed_import = db.query(DetectionImport).filter_by(video_id=video_id, active=True).first()
    with video_write_gate(
        db, project_id=project_id, video_id=video_id, allow_submitted=True,
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
        return state.video


@router.get("/api/projects/{project_id}/reviews/queue", response_model=list[VideoOut])
def review_queue(project_id: int, access: tuple = Depends(project_access),
                 db: Session = Depends(get_db)) -> list[Video]:
    require_reviewer(access[1], "Review permission is required to view the review queue")
    return db.query(Video).join(Submission, Submission.video_id == Video.id).filter(
        Video.project_id == project_id, Submission.status == "submitted"
    ).order_by(Submission.submitted_at.desc(), Submission.id.desc()).all()


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
        imp = validate_snapshot_integrity(db, submission.detection_snapshot)
        if imp.video_id != video_id:
            raise HTTPException(status_code=409, detail="Submission snapshot belongs to another video")
        copies = db.query(SubmissionAnnotation).filter_by(submission_id=submission.id).all()
        if not copies:
            raise HTTPException(status_code=409, detail="Submission annotation snapshot is empty")
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
        # Compatibility projection only; decision authority remains immutable copies.
        source_ids = [copy.source_annotation_id for copy in copies if copy.source_annotation_id is not None]
        db.query(Annotation).filter(Annotation.id.in_(source_ids), Annotation.video_id == video_id).update(
            {"review_status": body.result, "reviewer_id": membership.user_id}, synchronize_session=False
        )
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
