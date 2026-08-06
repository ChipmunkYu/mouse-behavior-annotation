"""项目级分类导出接口：状态、入队和安全下载。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import project_access
from ..export_jobs import (
    JOB_TYPE_EXPORT,
    _resolve_within,
    approved_rows,
    enqueue_export_job,
    latest_export_job,
)
from ..models import BackgroundJob, BehaviorCategory
from ..schemas import ExportRequest, ExportStatusOut, JobOut, MissingClipOut

router = APIRouter(tags=["exports"])
_EXPORT_ROLES = {"owner", "admin"}


def _require_export_role(access: tuple) -> None:
    if access[1].role not in _EXPORT_ROLES:
        raise HTTPException(status_code=403, detail="Only owner/admin can manage exports")


def _job_out(job: BackgroundJob) -> JobOut:
    return JobOut.model_validate(job)


@router.post("/api/projects/{project_id}/export", response_model=JobOut, status_code=201)
def create_export(
    project_id: int,
    body: ExportRequest,
    request: Request,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> JobOut:
    """owner/admin 发起导出；同项目 active 导出排他。"""
    _require_export_role(access)
    category_ids = list(dict.fromkeys(body.category_ids or []))
    if category_ids:
        found = {
            row[0]
            for row in db.query(BehaviorCategory.id)
            .filter(
                BehaviorCategory.project_id == project_id,
                BehaviorCategory.id.in_(category_ids),
            )
            .all()
        }
        if found != set(category_ids):
            raise HTTPException(
                status_code=400, detail="Category does not belong to this project"
            )
    job = enqueue_export_job(db, access[0], category_ids)
    if job is None:
        raise HTTPException(status_code=409, detail="An export is already queued or running")
    request.app.state.export_worker.schedule(job.id)
    db.refresh(job)
    return _job_out(job)


@router.get("/api/projects/{project_id}/export/status", response_model=ExportStatusOut)
def export_status(
    project_id: int,
    request: Request,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> ExportStatusOut:
    """返回最近任务范围内可导出、实体就绪和缺失片段统计。"""
    _require_export_role(access)
    latest = latest_export_job(db, project_id)
    category_ids = None
    if latest is not None:
        category_ids = (latest.payload or {}).get("category_ids") or None
    rows = approved_rows(db, project_id, category_ids)
    missing: list[MissingClipOut] = []
    ready = 0
    for annotation, video, clip in rows:
        path = None
        if clip is not None and clip.status == "ready":
            path, _ = _resolve_within(clip.clip_path, request.app.state.settings.clips_dir)
        if path is not None and path.is_file():
            ready += 1
        else:
            missing.append(
                MissingClipOut(
                    annotation_id=annotation.id,
                    category_name=annotation.category.name,
                    video_filename=video.filename,
                )
            )
    return ExportStatusOut(
        latest_job=_job_out(latest) if latest is not None else None,
        exportable_count=len(rows),
        ready_count=ready,
        missing_count=len(missing),
        missing_clips=missing,
    )


@router.get("/api/projects/{project_id}/export/download")
def download_export(
    project_id: int,
    request: Request,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
):
    """下载项目最近成功且未过期的安全导出文件。"""
    _require_export_role(access)
    job = (
        db.query(BackgroundJob)
        .filter(
            BackgroundJob.project_id == project_id,
            BackgroundJob.job_type == JOB_TYPE_EXPORT,
            BackgroundJob.status == "succeeded",
        )
        .order_by(BackgroundJob.id.desc())
        .first()
    )
    if job is None or job.expires_at is None or job.expires_at <= datetime.utcnow():
        raise HTTPException(status_code=404, detail="Export not found")
    path, _ = _resolve_within(job.result_path, request.app.state.settings.exports_dir)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Export not found")
    return FileResponse(path, media_type="application/zip", filename=path.name)
