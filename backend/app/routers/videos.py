"""视频接口：项目内视频列表 / JSON 创建视频元数据 / 真实视频流式上传 / 视频流。"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4

from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..assignee_triggers import ASSIGNEE_CONFLICT_DETAIL, is_assignee_write_conflict
from ..database import get_db
from ..deps import project_access
from ..models import ProjectMembership, User, Video
from ..permissions import is_manager, require_manager
from ..schemas import AssignmentBatchRequest, AssignmentStatsItem, AssignmentStatsOut, VideoCreate, VideoOut

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


def _validate_assignee(db: Session, project_id: int, membership_id: int | None) -> ProjectMembership | None:
    if membership_id is None:
        return None
    membership = db.get(ProjectMembership, membership_id)
    if membership is None or membership.project_id != project_id or membership.status != "active":
        raise HTTPException(status_code=400, detail="Assignee must be an active member of this project")
    return membership


def _raise_if_assignee_conflict(db: Session, exc: IntegrityError) -> None:
    db.rollback()
    if is_assignee_write_conflict(exc):
        raise HTTPException(status_code=409, detail=ASSIGNEE_CONFLICT_DETAIL) from None
    raise exc


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
    view: Literal["mine", "unassigned", "all"] = "all",
    workflow_status: str | None = None,
    assignee_membership_id: int | None = None,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> list[Video]:
    query = db.query(Video).filter(Video.project_id == project_id)
    if view == "mine":
        query = query.filter(Video.assignee_membership_id == access[1].id)
    elif view == "unassigned":
        query = query.filter(
            Video.assignee_membership_id.is_(None),
            Video.workflow_status == "draft",
        )
    if workflow_status is not None:
        query = query.filter(Video.workflow_status == workflow_status)
    if assignee_membership_id is not None:
        query = query.filter(Video.assignee_membership_id == assignee_membership_id)
    return query.order_by(Video.created_at.desc()).all()


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

    if body.assignee_membership_id is not None and not is_manager(access[1]):
        raise HTTPException(status_code=403, detail="Only owner/admin may specify an assignee")
    _validate_assignee(db, project_id, body.assignee_membership_id)
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
        assignee_membership_id=body.assignee_membership_id,
    )
    db.add(video)
    try:
        db.commit()
    except IntegrityError as exc:
        _raise_if_assignee_conflict(db, exc)
    db.refresh(video)
    return video


@router.post("/api/projects/{project_id}/videos/upload", response_model=VideoOut, status_code=201)
async def upload_video(
    project_id: int,
    request: Request,
    file: UploadFile = File(...),
    assignee_membership_id: int | None = Form(default=None),
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
    if assignee_membership_id is not None and not is_manager(membership):
        raise HTTPException(status_code=403, detail="Only owner/admin may specify an assignee")
    _validate_assignee(db, project_id, assignee_membership_id)

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
            assignee_membership_id=assignee_membership_id,
        )
        try:
            db.add(video)
            db.commit()
        except IntegrityError as exc:
            # DB 提交失败：回滚并清理已落盘的最终孤儿文件
            db.rollback()
            _remove_file(final_path)
            if is_assignee_write_conflict(exc):
                raise HTTPException(status_code=409, detail=ASSIGNEE_CONFLICT_DETAIL) from None
            raise HTTPException(status_code=500, detail=ERR_DB_SAVE)
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


@router.post("/api/projects/{project_id}/videos/{video_id}/claim", response_model=VideoOut)
def claim_video(project_id: int, video_id: int, access: tuple = Depends(project_access),
                db: Session = Depends(get_db)) -> Video:
    try:
        changed = db.query(Video).filter(
            Video.id == video_id,
            Video.project_id == project_id,
            Video.assignee_membership_id.is_(None),
            Video.workflow_status == "draft",
        ).update({"assignee_membership_id": access[1].id}, synchronize_session=False)
    except IntegrityError as exc:
        _raise_if_assignee_conflict(db, exc)
    if changed != 1:
        exists = db.query(Video.id).filter_by(id=video_id, project_id=project_id).first()
        if not exists:
            raise HTTPException(status_code=404, detail="Video not found in this project")
        raise HTTPException(status_code=409, detail="Video is no longer claimable")
    db.commit()
    return db.get(Video, video_id)


@router.post("/api/projects/{project_id}/videos/{video_id}/release", response_model=VideoOut)
def release_video(project_id: int, video_id: int, access: tuple = Depends(project_access),
                  db: Session = Depends(get_db)) -> Video:
    changed = db.query(Video).filter(
        Video.id == video_id, Video.project_id == project_id,
        Video.assignee_membership_id == access[1].id, Video.workflow_status == "draft",
    ).update({"assignee_membership_id": None}, synchronize_session=False)
    if changed != 1:
        video = db.query(Video).filter_by(id=video_id, project_id=project_id).one_or_none()
        if video is None:
            raise HTTPException(status_code=404, detail="Video not found in this project")
        raise HTTPException(status_code=409, detail="Only the current assignee may release a draft video")
    db.commit()
    return db.get(Video, video_id)


@router.post("/api/projects/{project_id}/videos/assignments", response_model=list[VideoOut])
def batch_assign(project_id: int, body: AssignmentBatchRequest,
                 access: tuple = Depends(project_access), db: Session = Depends(get_db)) -> list[Video]:
    require_manager(access[1])
    _validate_assignee(db, project_id, body.assignee_membership_id)
    ids = list(dict.fromkeys(body.video_ids))
    try:
        changed = db.query(Video).filter(
            Video.project_id == project_id,
            Video.id.in_(ids),
            Video.workflow_status.in_(("draft", "rejected")),
        ).update(
            {"assignee_membership_id": body.assignee_membership_id},
            synchronize_session=False,
        )
    except IntegrityError as exc:
        # The failed statement transaction is always fully rolled back.
        _raise_if_assignee_conflict(db, exc)
    if changed != len(ids):
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Assignment conflict: every video must still belong to this project and be draft/rejected",
        )
    db.commit()
    return db.query(Video).filter(Video.project_id == project_id, Video.id.in_(ids)).all()


@router.get("/api/projects/{project_id}/assignment-stats", response_model=AssignmentStatsOut)
def assignment_stats(project_id: int, access: tuple = Depends(project_access),
                     db: Session = Depends(get_db)) -> AssignmentStatsOut:
    status_counts = lambda status: func.coalesce(func.sum(case(
        (Video.workflow_status == status, 1), else_=0
    )), 0)
    rows = db.query(
        ProjectMembership.id, User.username, func.count(Video.id),
        status_counts("draft"), status_counts("submitted"),
        status_counts("approved"), status_counts("rejected"),
    ).join(User, User.id == ProjectMembership.user_id).outerjoin(
        Video,
        (Video.assignee_membership_id == ProjectMembership.id)
        & (Video.project_id == project_id),
    ).filter(ProjectMembership.project_id == project_id).group_by(
        ProjectMembership.id, User.username
    ).all()
    items = [AssignmentStatsItem(
        assignee_membership_id=mid, username=name, total=total,
        draft=draft, submitted=submitted, approved=approved, rejected=rejected,
    ) for mid, name, total, draft, submitted, approved, rejected in rows]
    totals = db.query(
        func.count(Video.id), status_counts("draft"), status_counts("submitted"),
        status_counts("approved"), status_counts("rejected"),
        func.coalesce(func.sum(case((Video.assignee_membership_id.is_(None), 1), else_=0)), 0),
        func.coalesce(func.sum(case((
            Video.assignee_membership_id.is_(None) & (Video.workflow_status == "draft"), 1
        ), else_=0)), 0),
    ).filter(Video.project_id == project_id).one()
    return AssignmentStatsOut(
        total=totals[0], draft=totals[1], submitted=totals[2],
        approved=totals[3], rejected=totals[4], unassigned=totals[5],
        claimable=totals[6], by_assignee=items,
    )


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
    # 与 upload 权限一致：成员存在但 status != active → 403
    if membership.status != "active":
        raise HTTPException(status_code=403, detail=ERR_MEMBERSHIP_INACTIVE)

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
