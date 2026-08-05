"""视频接口：项目内视频列表 / JSON 创建视频元数据 / 视频流。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..deps import project_access
from ..models import ProjectMembership, User, Video
from ..schemas import VideoCreate, VideoOut

router = APIRouter(tags=["videos"])


@router.get("/api/projects/{project_id}/videos", response_model=list[VideoOut])
def list_videos(
    project_id: int,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> list[Video]:
    return (
        db.query(Video)
        .filter(Video.project_id == project_id)
        .order_by(Video.created_at.desc())
        .all()
    )


@router.post("/api/projects/{project_id}/videos", response_model=VideoOut, status_code=201)
def create_video(
    project_id: int,
    body: VideoCreate,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> Video:
    filename = body.filename.strip()
    if not filename:
        raise HTTPException(status_code=400, detail="filename must not be empty")

    video = Video(
        project_id=project_id,
        filename=filename,
        duration=body.duration,
        fps=body.fps,
        width=body.width,
        height=body.height,
        status=body.status or "metadata",
        storage_path=body.storage_path,
        uploaded_by=access[1].user_id,
    )
    db.add(video)
    db.commit()
    db.refresh(video)
    return video


@router.get("/api/videos/{video_id}/stream")
def stream_video(
    video_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")

    # 需为视频所属项目的成员
    membership = (
        db.query(ProjectMembership)
        .filter(ProjectMembership.project_id == video.project_id, ProjectMembership.user_id == user.id)
        .first()
    )
    if membership is None:
        raise HTTPException(status_code=403, detail="You are not a member of this video's project")

    if not video.storage_path:
        raise HTTPException(status_code=404, detail="Video file is not available (no storage_path)")

    # 安全边界：只允许提供配置视频目录内的文件，防止任意读取项目外敏感文件。
    # storage_path 支持“绝对路径”或“相对 data/videos/ 的相对路径”，二者都必须解析到 videos_dir 内。
    videos_dir = request.app.state.settings.videos_dir.resolve()
    raw = Path(video.storage_path)
    path = raw.resolve() if raw.is_absolute() else (videos_dir / raw).resolve()

    if not path.is_relative_to(videos_dir):
        raise HTTPException(status_code=404, detail="Video file is outside the allowed videos directory")

    if not path.is_file():
        raise HTTPException(status_code=404, detail="Video file not found on disk")

    return FileResponse(path=path, filename=video.filename, content_disposition_type="inline")
