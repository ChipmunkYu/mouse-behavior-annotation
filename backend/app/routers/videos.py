"""视频接口：项目内视频列表 / JSON 创建视频元数据 / 真实视频流式上传 / 视频流。"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..deps import project_access
from ..models import ProjectMembership, User, Video
from ..schemas import VideoCreate, VideoOut

router = APIRouter(tags=["videos"])

# ---------- 真实视频上传：扩展名 / 媒体状态 ----------
# 常见视频扩展名；大小写不敏感（_normalize_ext 统一小写）。Content-Type 仅作辅助，不作为校验依据。
ALLOWED_VIDEO_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm", "m4v", "wmv", "mpeg", "mpg"}
# 浏览器通常可直接尝试播放 → 媒体 status=uploaded；其余 needs_transcode（本批不运行 ffprobe/ffmpeg）。
PLAYABLE_EXTENSIONS = {"mp4", "webm", "mov", "m4v"}

# 稳定错误文案（对外契约，保持不变）
ERR_FILENAME_REQUIRED = "Upload filename is required"
ERR_FILENAME_INVALID = "Upload filename is invalid"
ERR_EXTENSION_NOT_ALLOWED = "Video file extension is not allowed"
ERR_EMPTY_FILE = "Uploaded file is empty"
ERR_DISK_SPACE = "Insufficient disk space to store video"
ERR_DB_SAVE = "Failed to save video metadata"
ERR_MEMBERSHIP_INACTIVE = "Project membership is not active"


class _InsufficientDiskSpace(Exception):
    """可用空间不足（写入后可能跌破安全保留值）→ 507。"""


class _EmptyUpload(Exception):
    """上传文件为空（0 字节）→ 400。"""


def _safe_display_name(raw: str) -> str:
    """只保留安全展示名：去路径分隔（/ 与 \\）、去控制字符、去首尾空白，并限制到 DB 列长度。"""
    name = raw.replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable())
    name = name.strip()
    if not name:
        raise ValueError(ERR_FILENAME_INVALID)
    if len(name) > 255:
        stem, dot, ext = name.rpartition(".")
        if dot and ext:
            name = stem[: 255 - 1 - len(ext)] + dot + ext
        else:
            name = name[:255]
    return name


def _normalize_ext(display_name: str) -> str:
    """取最后一段扩展名并小写；无扩展名（含尾点）返回空串。"""
    _stem, dot, ext = display_name.rpartition(".")
    if not dot:
        return ""
    return ext.lower()


def _media_status_for_ext(ext: str) -> str:
    return "uploaded" if ext in PLAYABLE_EXTENSIONS else "needs_transcode"


def _check_disk_space(videos_dir: Path, reserve: int, extra: int = 0) -> None:
    """确保写入 extra 字节后 videos_dir 仍有 reserve 安全余量，不足抛 507 语义异常。

    extra=0 时等价于“当前可用空间必须大于保留值”。
    """
    free = shutil.disk_usage(videos_dir).free
    if free - reserve < extra:
        raise _InsufficientDiskSpace()


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


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


@router.post("/api/projects/{project_id}/videos/upload", response_model=VideoOut, status_code=201)
async def upload_video(
    project_id: int,
    request: Request,
    file: UploadFile = File(...),
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> Video:
    """真实视频流式上传：multipart 字段 `file`，分块写入临时文件后原子 rename。

    - 应用层不设文件大小上限；每块写入前校验 videos_dir 可用空间（保留 UPLOAD_DISK_RESERVE_BYTES）。
    - 磁盘目标为 UUID 名（相对 storage_path），临时 `.part` 成功后原子 rename；
      失败/取消/DB 提交失败均清理临时与最终孤儿文件。
    - 扩展名（大小写不敏感）是唯一校验依据；Content-Type 仅作辅助。
    """
    settings = request.app.state.settings

    # 仅 active 项目成员可上传
    _project, membership = access
    if membership.status != "active":
        raise HTTPException(status_code=403, detail=ERR_MEMBERSHIP_INACTIVE)

    raw_name = file.filename or ""
    if not raw_name:
        raise HTTPException(status_code=400, detail=ERR_FILENAME_REQUIRED)
    try:
        display_name = _safe_display_name(raw_name)
    except ValueError:
        raise HTTPException(status_code=400, detail=ERR_FILENAME_INVALID)
    ext = _normalize_ext(display_name)
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail=ERR_EXTENSION_NOT_ALLOWED)

    videos_dir = settings.videos_dir.resolve()
    videos_dir.mkdir(parents=True, exist_ok=True)
    reserve = settings.upload_disk_reserve_bytes
    chunk_size = max(1, settings.upload_chunk_size)

    # 不可碰撞磁盘目标：UUID + 小写扩展名；临时文件同目录 .part 后缀，保证同文件系统原子 rename
    uid = uuid4().hex
    final_name = f"{uid}.{ext}"
    temp_path = videos_dir / f"{uid}.part"
    final_path = videos_dir / final_name

    written = 0
    temp_file = None
    try:
        # 写入前检查一次可用空间
        _check_disk_space(videos_dir, reserve)
        temp_file = open(temp_path, "wb")
        while True:
            chunk = await file.read(chunk_size)  # 分块读取，绝不一次 read 全部
            if not chunk:
                break
            # 每块写入前检查：写入本块后仍须保留安全余量
            _check_disk_space(videos_dir, reserve, len(chunk))
            temp_file.write(chunk)
            written += len(chunk)
        temp_file.flush()
        os.fsync(temp_file.fileno())
        temp_file.close()
        temp_file = None

        if written == 0:
            raise _EmptyUpload()

        # 原子 rename：成功后 temp_path 不再存在
        os.replace(temp_path, final_path)
        temp_path = None

        video = Video(
            project_id=project_id,
            filename=display_name,
            storage_path=final_name,  # 相对路径，限制在 videos_dir 内
            status=_media_status_for_ext(ext),
            uploaded_by=membership.user_id,
            workflow_status="draft",
            annotation_revision=1,
        )
        try:
            db.add(video)
            db.commit()
        except Exception:
            # DB 提交失败：回滚并清理已落盘的最终孤儿文件
            db.rollback()
            _remove_file(final_path)
            raise HTTPException(status_code=500, detail=ERR_DB_SAVE)
        db.refresh(video)
        return video
    except _InsufficientDiskSpace:
        raise HTTPException(status_code=507, detail=ERR_DISK_SPACE)
    except _EmptyUpload:
        raise HTTPException(status_code=400, detail=ERR_EMPTY_FILE)
    finally:
        if temp_file is not None:
            try:
                temp_file.close()
            except OSError:
                pass
        if temp_path is not None:
            _remove_file(temp_path)
        await file.close()


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
