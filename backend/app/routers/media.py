"""媒体任务接口（批次 4）：媒体生成状态查询 / 触发生成（幂等或重试）/ 任务查询。

- `GET  .../media-status`：项目成员可读当前修订的片段生成进度与最近任务。
- `POST .../media/generate`：仅 owner/admin/reviewer，仅 approved 视频可触发；
  幂等（已有 queued/running/succeeded 返回同一任务）或重试（failed 重置回 queued）。
- `GET  .../jobs/{job_id}`：项目成员可查本项目内任务详情。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import project_access
from ..media_jobs import (clip_entities_ready, enqueue_media_job, enqueue_submission_media,
                          media_dedupe_key, submission_media_dedupe_key)
from ..models import Annotation, BackgroundJob, Clip, Submission, SubmissionAnnotation, Video
from ..permissions import can_review, is_manager
from ..schemas import JobOut, MediaStatusOut

router = APIRouter(tags=["media"])
logger = logging.getLogger(__name__)

# 触发生成角色：与审核角色一致（reviewer 只可审核与触发，不可改标注）


def _get_video_in_project(db: Session, project_id: int, video_id: int) -> Video:
    video = db.get(Video, video_id)
    if video is None or video.project_id != project_id:
        raise HTTPException(status_code=404, detail="Video not found in this project")
    return video


def _to_job_out(job: BackgroundJob) -> JobOut:
    return JobOut(
        id=job.id,
        project_id=job.project_id,
        job_type=job.job_type,
        status=job.status,
        progress=job.progress,
        payload=job.payload,
        result_path=job.result_path,
        error=job.error,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        expires_at=job.expires_at,
    )


@router.get(
    "/api/projects/{project_id}/videos/{video_id}/media-status",
    response_model=MediaStatusOut,
)
def media_status(
    project_id: int,
    video_id: int,
    request: Request,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> MediaStatusOut:
    """项目成员可读：当前修订的片段生成进度与该视频对应任务。"""
    video = _get_video_in_project(db, project_id, video_id)
    submission = db.query(Submission).filter_by(video_id=video.id, status="approved").first()
    revision = submission.source_media_revision if submission else video.media_revision
    clips = (
        db.query(Clip)
        .join(SubmissionAnnotation, SubmissionAnnotation.id == Clip.submission_annotation_id)
        .filter(SubmissionAnnotation.submission_id == submission.id)
        .all()
    ) if submission else db.query(Clip).join(Annotation, Annotation.id == Clip.annotation_id).filter(
        Annotation.video_id == video.id, Clip.source_revision == revision).all()
    counts = {"total": len(clips), "ready": 0, "processing": 0, "failed": 0, "pending": 0}
    for clip in clips:
        status = clip.status
        if status == "ready" and not clip_entities_ready(clip, request.app.state.settings):
            status = "pending"
        counts[status] = counts.get(status, 0) + 1
    job = (
        db.query(BackgroundJob)
        .filter(BackgroundJob.dedupe_key == submission_media_dedupe_key(submission.id))
        .first()
    ) if submission else db.query(BackgroundJob).filter_by(
        dedupe_key=media_dedupe_key(video.id, revision)).first()
    return MediaStatusOut(
        video_id=video.id,
        revision=revision,
        workflow_status=video.workflow_status,
        total=counts["total"],
        ready=counts["ready"],
        processing=counts["processing"],
        failed=counts["failed"],
        pending=counts["pending"],
        latest_job=_to_job_out(job) if job is not None else None,
    )


@router.post(
    "/api/projects/{project_id}/videos/{video_id}/media/generate",
    response_model=JobOut,
)
def generate_media(
    project_id: int,
    video_id: int,
    request: Request,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> JobOut:
    """仅 approved 视频可触发；幂等或重试（见模块 docstring）。"""
    membership = access[1]
    if not can_review(membership):
        raise HTTPException(
            status_code=403,
            detail="Only owner/admin/reviewer can generate media",
        )
    video = _get_video_in_project(db, project_id, video_id)
    submission = db.query(Submission).filter_by(video_id=video.id, status="approved").first()
    if submission is None and video.workflow_status != "approved":
        raise HTTPException(
            status_code=400,
            detail="Only approved videos can generate media",
        )
    if submission is not None:
        job = enqueue_submission_media(db, submission)
        db.commit(); db.refresh(job)
    else:
        job = enqueue_media_job(db, video, request.app.state.settings)
    try:
        request.app.state.media_worker.schedule(job.id)
    except Exception:
        logger.exception("Schedule media job %s failed; queued row remains recoverable", job.id)
    db.refresh(job)
    return _to_job_out(job)


@router.get("/api/projects/{project_id}/jobs/{job_id}", response_model=JobOut)
def get_job(
    project_id: int,
    job_id: int,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> JobOut:
    """项目任务隔离查询；导出任务仅 owner/admin，其他任务保持成员可读。"""
    job = db.get(BackgroundJob, job_id)
    if job is None or job.project_id != project_id:
        raise HTTPException(status_code=404, detail="Job not found in this project")
    if job.job_type == "export" and not is_manager(access[1]):
        raise HTTPException(status_code=403, detail="Only owner/admin can view export jobs")
    return _to_job_out(job)
