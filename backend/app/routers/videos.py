"""视频接口：项目内视频列表 / JSON 创建视频元数据 / 真实视频流式上传 / 视频流。"""
from __future__ import annotations

import hmac
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from time import time
from uuid import uuid4

from collections.abc import Iterator
from typing import Literal

import jwt
from anyio import to_thread
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import AuthContext, authenticate_token, get_auth_context, get_current_user
from ..assignee_triggers import ASSIGNEE_CONFLICT_DETAIL, is_assignee_write_conflict
from ..database import get_db
from ..display_proxy_enqueue import enqueue_for_video, hash_display_proxy_source, submit_after_commit
from ..deps import project_access
from ..models import BackgroundJob, ProjectMembership, User, Video
from ..media_auth import (
    MediaKeys, bearer_binding, decode_media_jwt, encode_media_jwt, raw_cookie_values,
)
from ..permissions import is_manager, require_manager
from ..video_delete_db import (
    VideoDeleteConflictError, VideoDeleteForbiddenError, VideoDeleteIntegrityError,
    VideoDeleteNotFoundError,
)
from ..video_delete_service import VideoDeleteServiceError
from ..video_operation_gate import VideoOperationBusyError
from ..video_operation_dependency import VIDEO_OPERATION_BUSY_DETAIL
from ..video_operation_dependency import require_video_operation_gate
from ..video_playback import (
    DISPLAY_PROXY_FAILED,
    DISPLAY_PROXY_PENDING,
    public_video,
    resolve_video_playback,
)
from ..schemas import (
    AssignmentBatchRequest,
    AssignmentStatsItem,
    AssignmentStatsOut,
    VideoClaimsRequest,
    VideoClaimsResponse,
    VideoCreate,
    VideoOut,
    StreamTicketResponse,
)

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
ERR_BATCH_CLAIM_CONFLICT = "One or more videos are no longer claimable"


def _acquire_video_ids(request: Request, video_ids: list[int]) -> Iterator[None]:
    try:
        with request.app.state.video_operation_gate.acquire_many(video_ids):
            yield
    except VideoOperationBusyError as exc:
        raise HTTPException(status_code=409, detail=VIDEO_OPERATION_BUSY_DETAIL) from exc


def _claim_operation_gate(request: Request, body: VideoClaimsRequest) -> Iterator[None]:
    yield from _acquire_video_ids(request, list(body.video_ids))


def _assignment_operation_gate(request: Request, body: AssignmentBatchRequest) -> Iterator[None]:
    yield from _acquire_video_ids(request, list(body.video_ids))


@router.delete("/api/projects/{project_id}/videos/{video_id}", status_code=204)
def hard_delete_video(
    project_id: int,
    video_id: int,
    request: Request,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> None:
    """Synchronously hard-delete one draft/rejected video under the app gate."""
    actor_user_id = access[1].user_id
    # Release project_access's SQLite read transaction before the service opens
    # its independent BEGIN IMMEDIATE final-delete transaction.
    db.rollback()
    try:
        request.app.state.video_delete_service.delete(
            project_id=project_id, video_id=video_id, actor_user_id=actor_user_id,
        )
    except VideoDeleteForbiddenError as exc:
        raise HTTPException(status_code=403, detail=exc.safe_message) from exc
    except VideoDeleteNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.safe_message) from exc
    except (VideoDeleteConflictError, VideoOperationBusyError) as exc:
        detail = exc.safe_message if isinstance(exc, VideoDeleteConflictError) else VIDEO_OPERATION_BUSY_DETAIL
        raise HTTPException(status_code=409, detail=detail) from exc
    except (VideoDeleteIntegrityError, VideoDeleteServiceError) as exc:
        detail = exc.safe_message
        raise HTTPException(status_code=500, detail=detail) from exc


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


def _claim_videos_cas(
    db: Session,
    project_id: int,
    video_ids: list[int],
    membership_id: int,
) -> int:
    """将仍属于项目、未分配且为 draft 的指定视频原子领取给当前成员。"""
    return db.query(Video).filter(
        Video.id.in_(video_ids),
        Video.project_id == project_id,
        Video.assignee_membership_id.is_(None),
        Video.workflow_status == "draft",
    ).update({"assignee_membership_id": membership_id}, synchronize_session=False)


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


def _committed_upload_is_visible(
    db: Session,
    *,
    video_id: int,
    project_id: int,
    storage_path: str,
    source_sha256: str | None,
    display_proxies_enabled: bool,
    job_id: int | None,
) -> bool:
    """Resolve an uncertain commit using a fresh session on an independent connection."""
    bind = db.get_bind()
    try:
        with bind.connect() as connection, Session(bind=connection) as verifier:
            video = verifier.get(Video, video_id)
            if not (
                video is not None
                and video.project_id == project_id
                and video.storage_path == storage_path
                and video.source_sha256 == source_sha256
            ):
                return False
            if not display_proxies_enabled:
                return True
            if job_id is None:
                return False
            job = verifier.get(BackgroundJob, job_id)
            payload = job.payload if job is not None else None
            return bool(
                job is not None
                and job.project_id == project_id
                and job.job_type == "display_proxy"
                and isinstance(payload, dict)
                and payload.get("video_id") == video_id
                and payload.get("project_id") == project_id
                and payload.get("source_sha256") == source_sha256
            )
    except Exception:
        return False


@router.get("/api/projects/{project_id}/videos", response_model=list[VideoOut])
def list_videos(
    project_id: int,
    request: Request,
    view: Literal["mine", "unassigned", "all"] = "all",
    workflow_status: str | None = None,
    assignee_membership_id: int | None = None,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> list[dict]:
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
    return [public_video(video, request.app.state.settings)
            for video in query.order_by(Video.created_at.desc()).all()]


@router.post("/api/projects/{project_id}/videos", response_model=VideoOut, status_code=201)
def create_video(
    project_id: int,
    body: VideoCreate,
    request: Request,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> dict:
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
        uploaded_by=access[1].user_id,
        assignee_membership_id=body.assignee_membership_id,
    )
    db.add(video)
    try:
        db.commit()
    except IntegrityError as exc:
        _raise_if_assignee_conflict(db, exc)
    db.refresh(video)
    return public_video(video, request.app.state.settings)


@router.post("/api/projects/{project_id}/videos/upload", response_model=VideoOut, status_code=201)
async def upload_video(
    project_id: int,
    request: Request,
    file: UploadFile = File(...),
    assignee_membership_id: int | None = Form(default=None),
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> dict:
    """真实视频流式上传：multipart 字段 `file`，分块写入临时文件后原子 rename。

    - 应用层不设文件大小上限；每块写入前校验 videos_dir 可用空间（保留 UPLOAD_DISK_RESERVE_BYTES）。
    - 磁盘目标使用内部 UUID 名，临时 `.part` 成功后原子 rename；
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
    final_owned_by_database = False
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

        source_sha256 = None
        if settings.display_proxies_enabled:
            try:
                source_sha256 = await to_thread.run_sync(hash_display_proxy_source, final_path)
            except Exception:
                db.rollback()
                raise HTTPException(status_code=500, detail=ERR_DB_SAVE) from None

        video = Video(
            project_id=project_id,
            filename=display_name,
            storage_path=final_name,  # 相对路径，限制在 videos_dir 内
            status=_media_status_for_ext(ext),
            uploaded_by=membership.user_id,
            workflow_status="draft",
            annotation_revision=1,
            assignee_membership_id=assignee_membership_id,
            source_sha256=source_sha256,
        )
        job_id = None
        try:
            db.add(video)
            # Establish the target identity before commit so an uncertain commit can
            # be resolved without consulting the failed request session.
            db.flush()
            if settings.display_proxies_enabled:
                job_id = enqueue_for_video(db, video, settings)
        except IntegrityError as exc:
            db.rollback()
            if is_assignee_write_conflict(exc):
                raise HTTPException(status_code=409, detail=ASSIGNEE_CONFLICT_DETAIL) from None
            raise HTTPException(status_code=500, detail=ERR_DB_SAVE)
        except Exception:
            db.rollback()
            raise HTTPException(status_code=500, detail=ERR_DB_SAVE)

        video_id = video.id
        try:
            db.commit()
        except BaseException as exc:
            # A driver may raise after the database has actually committed. Release
            # the failed request transaction, then decide file ownership exclusively
            # from a fresh session/connection. Never submit work from this path.
            try:
                db.rollback()
            except Exception:
                pass
            final_owned_by_database = _committed_upload_is_visible(
                db,
                video_id=video_id,
                project_id=project_id,
                storage_path=final_name,
                source_sha256=source_sha256,
                display_proxies_enabled=settings.display_proxies_enabled,
                job_id=job_id,
            )
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, IntegrityError) and is_assignee_write_conflict(exc):
                raise HTTPException(status_code=409, detail=ASSIGNEE_CONFLICT_DETAIL) from None
            raise HTTPException(status_code=500, detail=ERR_DB_SAVE) from None

        final_owned_by_database = True
        submit_after_commit(request, job_id)
        db.refresh(video)
        return public_video(video, settings)
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
        if not final_owned_by_database:
            _remove_file(final_path)
        await file.close()


@router.post(
    "/api/projects/{project_id}/videos/claims",
    response_model=VideoClaimsResponse,
    dependencies=[Depends(_claim_operation_gate)],
)
def claim_videos(
    project_id: int,
    body: VideoClaimsRequest,
    request: Request,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> VideoClaimsResponse:
    video_ids = list(body.video_ids)
    try:
        changed = _claim_videos_cas(db, project_id, video_ids, access[1].id)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=ERR_BATCH_CLAIM_CONFLICT) from None
    if changed != len(video_ids):
        db.rollback()
        raise HTTPException(status_code=409, detail=ERR_BATCH_CLAIM_CONFLICT)
    db.commit()
    rows = db.query(Video).filter(
        Video.project_id == project_id,
        Video.id.in_(video_ids),
    ).all()
    by_id = {video.id: video for video in rows}
    return VideoClaimsResponse(
        claimed_count=len(video_ids),
        videos=[public_video(by_id[video_id], request.app.state.settings)
                for video_id in video_ids],
    )


@router.post("/api/projects/{project_id}/videos/{video_id}/claim", response_model=VideoOut,
             dependencies=[Depends(require_video_operation_gate)])
def claim_video(project_id: int, video_id: int, request: Request,
                access: tuple = Depends(project_access), db: Session = Depends(get_db)) -> dict:
    try:
        changed = _claim_videos_cas(db, project_id, [video_id], access[1].id)
    except IntegrityError as exc:
        _raise_if_assignee_conflict(db, exc)
    if changed != 1:
        exists = db.query(Video.id).filter_by(id=video_id, project_id=project_id).first()
        if not exists:
            raise HTTPException(status_code=404, detail="Video not found in this project")
        raise HTTPException(status_code=409, detail="Video is no longer claimable")
    db.commit()
    return public_video(db.get(Video, video_id), request.app.state.settings)


@router.post("/api/projects/{project_id}/videos/{video_id}/release", response_model=VideoOut,
             dependencies=[Depends(require_video_operation_gate)])
def release_video(project_id: int, video_id: int, request: Request,
                  access: tuple = Depends(project_access), db: Session = Depends(get_db)) -> dict:
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
    return public_video(db.get(Video, video_id), request.app.state.settings)


@router.post("/api/projects/{project_id}/videos/assignments", response_model=list[VideoOut],
             dependencies=[Depends(_assignment_operation_gate)])
def batch_assign(project_id: int, body: AssignmentBatchRequest, request: Request,
                 access: tuple = Depends(project_access), db: Session = Depends(get_db)) -> list[dict]:
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
    return [public_video(video, request.app.state.settings) for video in
            db.query(Video).filter(Video.project_id == project_id, Video.id.in_(ids)).all()]


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


def _authorized_video_path(
    *, video_id: int, user_id: int, request: Request, db: Session
) -> tuple[Video, Path, bool]:
    """签票与每次取流共用的实时用户、成员、路径和普通非空文件检查。"""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User no longer exists")
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")

    # 需为视频所属项目的成员
    membership = (
        db.query(ProjectMembership)
        .filter(ProjectMembership.project_id == video.project_id, ProjectMembership.user_id == user_id)
        .first()
    )
    if membership is None:
        raise HTTPException(status_code=403, detail="You are not a member of this video's project")
    # 与 upload 权限一致：成员存在但 status != active → 403
    if membership.status != "active":
        raise HTTPException(status_code=403, detail=ERR_MEMBERSHIP_INACTIVE)

    playback = resolve_video_playback(video, request.app.state.settings)
    if request.app.state.settings.display_proxies_enabled:
        from ..video_playback import observe_strict_playback
        observe_strict_playback(video, playback)
    if playback.status != "ready" or playback.path is None:
        if request.app.state.settings.display_proxies_enabled:
            detail = DISPLAY_PROXY_PENDING if playback.status == "pending" else DISPLAY_PROXY_FAILED
            raise HTTPException(status_code=409, detail=detail)
        raise HTTPException(status_code=404, detail="Video playback is not ready")
    return video, playback.path, playback.is_display_proxy


def _set_media_cookie(response: Response, name: str, value: str, *, path: str, max_age: int) -> None:
    response.set_cookie(
        name, value, max_age=max_age, path=path,
        secure=True, httponly=True, samesite="strict",
    )


@router.post("/api/videos/{video_id}/stream-ticket", response_model=StreamTicketResponse)
def create_stream_ticket(
    video_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
) -> StreamTicketResponse:
    settings = request.app.state.settings
    if not settings.media_ticket_enabled:
        raise HTTPException(status_code=404, detail="Media tickets are disabled")
    keys = MediaKeys.from_settings(settings)
    binding_values = raw_cookie_values(request.scope, {settings.media_binding_cookie_name})[
        settings.media_binding_cookie_name
    ]
    # 零个 binding 仅用于滚动升级旧会话；一旦出现，必须唯一且属于当前原始 Bearer。
    # 失败发生在任何 Set-Cookie 之前，不能借续票覆盖现有 binding。
    if len(binding_values) > 1:
        raise HTTPException(status_code=401, detail="Invalid media credentials")
    expected_binding = bearer_binding(auth.raw_token, keys.raw_bearer)
    if binding_values:
        try:
            existing = decode_media_jwt(
                binding_values[0], key=keys.binding_jwt,
                audience=settings.media_binding_audience,
                expected_type=settings.media_binding_type,
                required=("sub", "binding", "aud", "typ", "iat", "exp"),
            )
            if (
                not isinstance(existing["sub"], str)
                or existing["sub"] != str(auth.user.id)
                or not isinstance(existing["binding"], str)
                or not hmac.compare_digest(existing["binding"], expected_binding)
            ):
                raise jwt.InvalidTokenError("binding does not match bearer")
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            raise HTTPException(status_code=401, detail="Invalid media credentials") from None
    _authorized_video_path(video_id=video_id, user_id=auth.user.id, request=request, db=db)
    now = int(time())
    bearer_exp = int(auth.claims["exp"])
    exp = min(now + settings.media_ticket_ttl_seconds, bearer_exp)
    if exp <= now:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    binding = expected_binding
    sub = str(auth.user.id)
    ticket = encode_media_jwt({
        "sub": sub, "video_id": video_id, "binding": binding,
        "aud": settings.media_ticket_audience, "typ": settings.media_ticket_type,
        "iat": now, "exp": exp,
    }, keys.ticket)
    binding_token = encode_media_jwt({
        "sub": sub, "binding": binding, "aud": settings.media_binding_audience,
        "typ": settings.media_binding_type, "iat": now, "exp": bearer_exp,
    }, keys.binding_jwt)
    stream_path = f"/api/videos/{video_id}/stream"
    _set_media_cookie(
        response, settings.media_ticket_cookie_name, ticket,
        path=stream_path, max_age=max(0, exp - now),
    )
    _set_media_cookie(
        response, settings.media_binding_cookie_name, binding_token,
        path=settings.media_binding_cookie_path, max_age=max(0, bearer_exp - now),
    )
    return StreamTicketResponse(
        url=stream_path,
        expires_at=datetime.fromtimestamp(exp, tz=timezone.utc),
    )


def _legacy_auth_context(request: Request, db: Session) -> AuthContext:
    value = request.headers.get("authorization")
    if not value or not value.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = value[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return authenticate_token(token, db, request.app.state.settings)


def _stream_user_id(video_id: int, request: Request, db: Session) -> int:
    settings = request.app.state.settings
    names = {settings.media_ticket_cookie_name, settings.media_binding_cookie_name}
    values = raw_cookie_values(request.scope, names)
    cookie_present = any(values[name] for name in names)
    if not cookie_present:
        if not settings.media_legacy_bearer_enabled:
            raise HTTPException(status_code=401, detail="Media authentication required")
        return _legacy_auth_context(request, db).user.id

    # 任一媒体名出现即锁定 Cookie 分支；功能关闭或重复/缺一均不得回退 Bearer。
    if not settings.media_ticket_enabled or any(len(values[name]) != 1 for name in names):
        raise HTTPException(status_code=401, detail="Invalid media credentials")
    keys = MediaKeys.from_settings(settings)
    try:
        ticket = decode_media_jwt(
            values[settings.media_ticket_cookie_name][0], key=keys.ticket,
            audience=settings.media_ticket_audience, expected_type=settings.media_ticket_type,
            required=("sub", "video_id", "binding", "aud", "typ", "iat", "exp"),
        )
        binding = decode_media_jwt(
            values[settings.media_binding_cookie_name][0], key=keys.binding_jwt,
            audience=settings.media_binding_audience, expected_type=settings.media_binding_type,
            required=("sub", "binding", "aud", "typ", "iat", "exp"),
        )
        if (
            type(ticket["video_id"]) is not int
            or ticket["video_id"] != video_id
            or not isinstance(ticket["sub"], str)
            or not isinstance(binding["sub"], str)
            or not isinstance(ticket["binding"], str)
            or not isinstance(binding["binding"], str)
            or ticket["sub"] != binding["sub"]
            or ticket["binding"] != binding["binding"]
        ):
            raise jwt.InvalidTokenError("media claims do not match")
        return int(ticket["sub"])
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid media credentials") from None


@router.get("/api/videos/{video_id}/stream")
@router.head("/api/videos/{video_id}/stream")
def stream_video(
    video_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> FileResponse:
    user_id = _stream_user_id(video_id, request, db)
    video, path, is_display_proxy = _authorized_video_path(
        video_id=video_id, user_id=user_id, request=request, db=db,
    )

    return FileResponse(
        path=path,
        filename=f"display-video-{video.id}.mp4" if is_display_proxy else video.filename,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store"},
    )
