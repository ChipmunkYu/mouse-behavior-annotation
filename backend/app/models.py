"""数据模型：User / Project / ProjectMembership / BehaviorCategory / Video / Annotation。"""
from __future__ import annotations

from datetime import datetime
import secrets
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base
from .track_ids import TRACK_ID_UPPER_BOUND


def utcnow() -> datetime:
    return datetime.utcnow()


class User(Base):
    """独立登录账号，自身不携带业务角色；角色通过项目成员关系获得。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    memberships: Mapped[list["ProjectMembership"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    annotations: Mapped[list["Annotation"]] = relationship(
        back_populates="annotator", foreign_keys="Annotation.annotator_id"
    )


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("category_scheme_version >= 0", name="ck_projects_category_scheme_version"),
        CheckConstraint(
            "(category_scheme_locked_at IS NULL) = (category_scheme_locked_by IS NULL)",
            name="ck_projects_category_scheme_lock_pair",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    # 创建者不可随意删除：仍被项目引用时数据库拒绝删除（RESTRICT）
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    invite_code: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, default=lambda: secrets.token_urlsafe(32)
    )
    category_scheme_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    category_scheme_locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    category_scheme_locked_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )

    members: Mapped[list["ProjectMembership"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    categories: Mapped[list["BehaviorCategory"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    videos: Mapped[list["Video"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    import_batches: Mapped[list["VideoImportBatch"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    category_scheme_locker: Mapped[Optional["User"]] = relationship(
        foreign_keys=[category_scheme_locked_by]
    )
    category_scheme_audits: Mapped[list["CategorySchemeAudit"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectMembership(Base):
    """用户在某项目内的身份；user_id + project_id 唯一。"""

    __tablename__ = "project_memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "project_id", name="uq_membership_user_project"),
        UniqueConstraint("id", "project_id", name="uq_membership_id_project"),
        CheckConstraint("role IN ('owner', 'admin', 'member')", name="ck_membership_role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(32), default="member", nullable=False)
    can_review: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped["User"] = relationship(back_populates="memberships")
    project: Mapped["Project"] = relationship(back_populates="members")

    @property
    def username(self) -> str:
        return self.user.username

    @property
    def effective_can_review(self) -> bool:
        return self.role in {"owner", "admin"} or self.can_review


class BehaviorCategory(Base):
    """项目级行为类别；被标注引用的类别不可物理删除。"""

    __tablename__ = "behavior_categories"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_category_project_name"),
        CheckConstraint("mouse_count_min >= 1", name="ck_behavior_categories_mouse_count_min"),
        CheckConstraint(
            "mouse_count_max IS NULL OR mouse_count_max >= mouse_count_min",
            name="ck_behavior_categories_mouse_count_max",
        ),
        CheckConstraint(
            "participant_mode IN ('unordered', 'role_based')",
            name="ck_behavior_categories_participant_mode",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    group: Mapped[str] = mapped_column(String(64), nullable=False)
    color: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 参与小鼠数量范围：min>=1；max 为 NULL 表示无固定上限
    mouse_count_min: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    mouse_count_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    participant_mode: Mapped[str] = mapped_column(
        String(32), default="unordered", server_default="unordered", nullable=False
    )
    role_definitions: Mapped[list] = mapped_column(
        JSON, default=list, server_default=text("'[]'"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    project: Mapped["Project"] = relationship(back_populates="categories")
    annotations: Mapped[list["Annotation"]] = relationship(back_populates="category")


class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (
        CheckConstraint("annotation_revision >= 1", name="ck_videos_annotation_revision_min"),
        ForeignKeyConstraint(
            ["assignee_membership_id", "project_id"],
            ["project_memberships.id", "project_memberships.project_id"],
            ondelete="RESTRICT",
            name="fk_videos_assignee_project",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    duration: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    storage_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="metadata", nullable=False)
    # 上传者被删除后视频元数据保留，uploaded_by 置空（SET NULL）
    uploaded_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    assignee_membership_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # 工作流字段：媒体 status 之上独立的审核工作流状态
    workflow_status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    annotation_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    approved_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # 三类语义/媒体修订（v0.6）：检测导入、身份修正、媒体内容（拆分重编码判定）
    detection_import_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    identity_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    media_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    project: Mapped["Project"] = relationship(back_populates="videos")
    assignee: Mapped[Optional["ProjectMembership"]] = relationship(
        foreign_keys=[assignee_membership_id], passive_deletes=True
    )
    annotations: Mapped[list["Annotation"]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )
    reviews: Mapped[list["Review"]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )
    detection_imports: Mapped[list["DetectionImport"]] = relationship(
        back_populates="video", cascade="all, delete-orphan", passive_deletes=True
    )
    identity_edits: Mapped[list["IdentityEdit"]] = relationship(
        back_populates="video", cascade="all, delete-orphan", passive_deletes=True
    )
    detection_suppressions: Mapped[list["DetectionSuppression"]] = relationship(
        back_populates="video", cascade="all, delete-orphan", passive_deletes=True
    )


class Annotation(Base):
    """行为标注片段。"""

    __tablename__ = "annotations"
    __table_args__ = (
        CheckConstraint(
            "start_frame >= 0 AND end_frame > start_frame",
            name="ck_annotations_frame_range",
        ),
        CheckConstraint(
            "participant_status IN ('valid', 'needs_participants')",
            name="ck_annotations_participant_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 标注者不可随意删除：仍被标注引用时数据库拒绝删除（RESTRICT）
    annotator_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    category_id: Mapped[int] = mapped_column(
        ForeignKey("behavior_categories.id"), nullable=False, index=True
    )
    # 审核人被删除后标注保留，reviewer_id 置空（SET NULL）
    reviewer_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    start_frame: Mapped[int] = mapped_column(Integer, nullable=False)
    end_frame: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[str] = mapped_column(String(32), default="certain", nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    crop_region: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # v0.6：内嵌参与小鼠 ID（去重、数值升序）与身份修订；旧标注迁移后为 needs_mouse_ids
    mouse_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    mouse_id_status: Mapped[str] = mapped_column(
        String(32), default="needs_mouse_ids", nullable=False
    )
    detection_import_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    identity_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    participant_roles: Mapped[Optional[dict]] = mapped_column(
        JSON, default=dict, server_default=text("'{}'"), nullable=True
    )
    participant_status: Mapped[str] = mapped_column(
        String(32), default="valid", server_default="valid", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    video: Mapped["Video"] = relationship(back_populates="annotations")
    annotator: Mapped["User"] = relationship(
        back_populates="annotations", foreign_keys=[annotator_id]
    )
    reviewer: Mapped[Optional["User"]] = relationship(foreign_keys=[reviewer_id])
    category: Mapped["BehaviorCategory"] = relationship(back_populates="annotations")
    clips: Mapped[list["Clip"]] = relationship(
        back_populates="annotation", cascade="all, delete-orphan", passive_deletes=True
    )


class Review(Base):
    """审核记录：保留完整审核历史，用户删除后 reviewer_id 置空。"""

    __tablename__ = "reviews"
    __table_args__ = (
        Index("ix_reviews_video_revision", "video_id", "annotation_revision"),
        Index(
            "uq_reviews_submission_not_null",
            "submission_id",
            unique=True,
            sqlite_where=text("submission_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reviewer_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    result: Mapped[str] = mapped_column(String(32), nullable=False)  # approved / rejected
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    annotation_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # v0.6 审核快照补足三类语义修订；旧审核记录迁移为 0
    detection_import_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    identity_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Phase 1 兼容列：新审核将在后续阶段绑定不可变 Submission；旧记录保持 NULL。
    submission_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("submissions.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    project: Mapped["Project"] = relationship()
    video: Mapped["Video"] = relationship(back_populates="reviews")
    reviewer: Mapped[Optional["User"]] = relationship()
    submission: Mapped[Optional["Submission"]] = relationship(
        back_populates="review", foreign_keys=[submission_id]
    )


class Clip(Base):
    """行为标注片段：修订隔离（annotation_id + source_revision 唯一）。

    已审核标注被修改后，新修订生成新的 Clip 记录；未来生命周期中旧 Clip 会被显式删除。
    """

    __tablename__ = "clips"
    __table_args__ = (
        UniqueConstraint("annotation_id", "source_revision", name="uq_clip_annotation_revision"),
        Index("ix_clips_status", "status"),
        Index(
            "uq_clips_submission_annotation_not_null",
            "submission_annotation_id",
            unique=True,
            sqlite_where=text("submission_annotation_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    annotation_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("annotations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    source_revision: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # v0.6：媒体修订与语义修订拆分——仅源媒体变化时才需要重编码 Clip
    media_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Phase 1 兼容列：后续片段权威关系；旧 Clip 保留 annotation/revision 字段。
    submission_annotation_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("submission_annotations.id", ondelete="CASCADE"), nullable=True
    )
    # pending / processing / ready / failed / stale
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    clip_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    thumbnail_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    project: Mapped["Project"] = relationship()
    annotation: Mapped[Optional["Annotation"]] = relationship(back_populates="clips")
    submission_annotation: Mapped[Optional["SubmissionAnnotation"]] = relationship(
        back_populates="clip", foreign_keys=[submission_annotation_id]
    )


class BackgroundJob(Base):
    """后台任务（clip 生成 / export / cleanup 共用）。"""

    __tablename__ = "background_jobs"
    __table_args__ = (
        CheckConstraint(
            "progress >= 0 AND progress <= 100", name="ck_background_jobs_progress_range"
        ),
        Index("ix_background_jobs_status", "status"),
        Index("ix_background_jobs_type_status", "job_type", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 可空：项目级任务必填；全局清理等任务可缺省
    project_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # queued / running / succeeded / failed / cancelled
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    result_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 幂等去重键（批次 4）：同视频+修订只允许一行任务，唯一索引兜底并发重复入队；
    # 可空以兼容全局清理等非媒体任务（SQLite 唯一索引允许多个 NULL）。
    dedupe_key: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, unique=True, index=True
    )
    # 任务领取/中断重排次数（批次 4）：重启恢复时 running 视为中断，
    # attempts < media_max_attempts 则重排，否则判失败（重试上限耗尽）。
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    project: Mapped[Optional["Project"]] = relationship()


class VideoImportBatch(Base):
    """三文件（原始视频 / tracks.jsonl / metadata.json）独立上传批次。

    槽位状态 pending / uploading / uploaded / validated / failed；
    批次状态 uploading / validating / ready / failed。
    """

    __tablename__ = "video_import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="uploading", nullable=False)
    validation_errors: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    video_upload_state: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    tracks_upload_state: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    metadata_upload_state: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    # 上传文件路径（相对 videos_dir / detection_imports_dir）与原始文件名（Phase 1B）
    video_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    video_filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    tracks_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    metadata_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_video_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("videos.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    project: Mapped["Project"] = relationship(back_populates="import_batches")
    created_video: Mapped[Optional["Video"]] = relationship(foreign_keys=[created_video_id])


class DetectionImport(Base):
    """一次结构化检测导入修订：tracks/metadata 不可变，替换时新建修订。

    一个视频同一时刻只有一个 active 导入（部分唯一索引兜底）。
    """

    __tablename__ = "detection_imports"
    __table_args__ = (
        UniqueConstraint("video_id", "revision", name="uq_detection_imports_video_revision"),
        Index(
            "uq_detection_imports_active_video",
            "video_id",
            unique=True,
            sqlite_where=text("active = 1"),
        ),
        CheckConstraint("edit_version >= 0", name="ck_detection_imports_edit_version"),
        CheckConstraint(
            f"next_display_track_id >= 0 AND next_display_track_id <= {TRACK_ID_UPPER_BOUND}",
            name="ck_detection_imports_next_display_track_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    # tracks.jsonl / metadata.json 相对路径与校验和（配置数据根目录内）
    tracks_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    tracks_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    metadata_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    metadata_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    model_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    model_weights_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tracker_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tracker_params: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    fps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    frame_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # [first_frame, last_frame]
    frame_range: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    detection_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 源相对路径（metadata.source_relative），用于校验视频文件名一致性
    source_relative: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # pending / imported / failed
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    edit_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    next_display_track_id: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    created_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    video: Mapped["Video"] = relationship(back_populates="detection_imports")
    raw_detections: Mapped[list["RawDetection"]] = relationship(
        back_populates="detection_import", cascade="all, delete-orphan", passive_deletes=True
    )
    corrected_tracks: Mapped[list["CorrectedTrack"]] = relationship(
        back_populates="detection_import", cascade="all, delete-orphan", passive_deletes=True
    )
    state_overrides: Mapped[list["DetectionStateOverride"]] = relationship(
        back_populates="detection_import", cascade="all, delete-orphan", passive_deletes=True
    )
    draft_identity_edits: Mapped[list["DraftIdentityEdit"]] = relationship(
        back_populates="detection_import", cascade="all, delete-orphan", passive_deletes=True
    )
    detection_snapshots: Mapped[list["DetectionSnapshot"]] = relationship(
        back_populates="detection_import", passive_deletes=True
    )
    creator: Mapped[Optional["User"]] = relationship(foreign_keys=[created_by])


class RawDetection(Base):
    """导入后不可变的原始检测（框/关键点/置信度仅审计用途，不直接修改）。"""

    __tablename__ = "raw_detections"
    __table_args__ = (
        UniqueConstraint(
            "detection_import_id",
            "frame_index",
            "frame_detection_index",
            name="uq_raw_detections_import_frame_index",
        ),
        UniqueConstraint("id", "detection_import_id", name="uq_raw_detections_id_import"),
        CheckConstraint("frame_index >= 0", name="ck_raw_detections_frame_index"),
        CheckConstraint(
            "frame_detection_index >= 0", name="ck_raw_detections_frame_detection_index"
        ),
        CheckConstraint(
            f"raw_track_id >= 0 AND raw_track_id < {TRACK_ID_UPPER_BOUND}",
            name="ck_raw_detections_track_id",
        ),
        Index("ix_raw_detections_import_frame", "detection_import_id", "frame_index"),
        Index("ix_raw_detections_import_track", "detection_import_id", "raw_track_id"),
        Index(
            "ix_raw_detections_import_track_frame",
            "detection_import_id",
            "raw_track_id",
            "frame_index",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    detection_import_id: Mapped[int] = mapped_column(
        ForeignKey("detection_imports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    frame_index: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_detection_index: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_track_id: Mapped[int] = mapped_column(Integer, nullable=False)
    box: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    keypoints: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    detection_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    class_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    detection_import: Mapped["DetectionImport"] = relationship(back_populates="raw_detections")
    assignments: Mapped[list["CorrectedDetectionAssignment"]] = relationship(
        back_populates="raw_detection", cascade="all, delete-orphan", passive_deletes=True
    )
    state_override: Mapped[Optional["DetectionStateOverride"]] = relationship(
        back_populates="raw_detection",
        uselist=False,
        passive_deletes=True,
        overlaps="detection_import,state_overrides",
    )


class CorrectedTrack(Base):
    """修正轨迹：Split/Merge 后的显示 ID；仅表示视频内修正后的 YOLO 轨迹，非生物身份。

    当前活动轨迹满足 UNIQUE(detection_import_id, display_track_id)（部分唯一索引）。
    """

    __tablename__ = "corrected_tracks"
    __table_args__ = (
        Index(
            "uq_corrected_tracks_active_display",
            "detection_import_id",
            "display_track_id",
            unique=True,
            sqlite_where=text("active = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    detection_import_id: Mapped[int] = mapped_column(
        ForeignKey("detection_imports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    display_track_id: Mapped[int] = mapped_column(Integer, nullable=False)
    first_frame: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_frame: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    effective_detection_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_identity_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    merged_into_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("corrected_tracks.id", ondelete="SET NULL"), nullable=True
    )

    detection_import: Mapped["DetectionImport"] = relationship(back_populates="corrected_tracks")
    assignments: Mapped[list["CorrectedDetectionAssignment"]] = relationship(
        back_populates="corrected_track", cascade="all, delete-orphan", passive_deletes=True
    )
    merged_into: Mapped[Optional["CorrectedTrack"]] = relationship(
        remote_side="CorrectedTrack.id", foreign_keys="CorrectedTrack.merged_into_id"
    )


class CorrectedDetectionAssignment(Base):
    """物化视图：同一有效 RawDetection 在同一修订只归属于一个 CorrectedTrack。"""

    __tablename__ = "corrected_detection_assignments"
    __table_args__ = (
        UniqueConstraint(
            "raw_detection_id", "identity_revision", name="uq_cda_raw_detection_revision"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw_detection_id: Mapped[int] = mapped_column(
        ForeignKey("raw_detections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    corrected_track_id: Mapped[int] = mapped_column(
        ForeignKey("corrected_tracks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    identity_revision: Mapped[int] = mapped_column(Integer, nullable=False)

    raw_detection: Mapped["RawDetection"] = relationship(back_populates="assignments")
    corrected_track: Mapped["CorrectedTrack"] = relationship(back_populates="assignments")


class IdentityEdit(Base):
    """Split/Merge/撤销审计：记录操作者、基础/结果修订与影响范围。"""

    __tablename__ = "identity_edits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    detection_import_id: Mapped[int] = mapped_column(
        ForeignKey("detection_imports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    operation: Mapped[str] = mapped_column(String(32), nullable=False)  # split / merge / revert
    base_identity_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    result_identity_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    params: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    affected_detections: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    affected_annotations: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    operator_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reverted_edit_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("identity_edits.id", ondelete="SET NULL"), nullable=True
    )

    video: Mapped["Video"] = relationship(back_populates="identity_edits")
    operator: Mapped[Optional["User"]] = relationship(foreign_keys=[operator_id])
    reverted_edit: Mapped[Optional["IdentityEdit"]] = relationship(
        remote_side="IdentityEdit.id", foreign_keys="IdentityEdit.reverted_edit_id"
    )


class DetectionSuppression(Base):
    """整轨误检抑制；保留历史 detection scope 记录以支持查询与撤销。"""

    __tablename__ = "detection_suppressions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    detection_import_id: Mapped[int] = mapped_column(
        ForeignKey("detection_imports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    base_identity_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    result_identity_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)  # detection（历史）/ corrected_track
    operator_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reverted_suppression_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("detection_suppressions.id", ondelete="SET NULL"), nullable=True
    )

    video: Mapped["Video"] = relationship(back_populates="detection_suppressions")
    operator: Mapped[Optional["User"]] = relationship(foreign_keys=[operator_id])
    detections: Mapped[list["SuppressionDetection"]] = relationship(
        back_populates="suppression", cascade="all, delete-orphan", passive_deletes=True
    )
    reverted_suppression: Mapped[Optional["DetectionSuppression"]] = relationship(
        remote_side="DetectionSuppression.id",
        foreign_keys="DetectionSuppression.reverted_suppression_id",
    )


class SuppressionDetection(Base):
    """抑制操作冻结的原始检测集合（复合主键，天然唯一）。"""

    __tablename__ = "suppression_detections"

    suppression_id: Mapped[int] = mapped_column(
        ForeignKey("detection_suppressions.id", ondelete="CASCADE"), primary_key=True
    )
    raw_detection_id: Mapped[int] = mapped_column(
        ForeignKey("raw_detections.id", ondelete="CASCADE"), primary_key=True
    )

    suppression: Mapped["DetectionSuppression"] = relationship(back_populates="detections")
    raw_detection: Mapped["RawDetection"] = relationship()


# ---------------------------------------------------------------------------
# Phase 1 additive target schema.  Legacy read/write paths remain authoritative
# until the later cutover phases; these tables are intentionally not wired into
# current routers yet.
# ---------------------------------------------------------------------------


class DetectionStateOverride(Base):
    """Current draft state differing from the immutable RawDetection baseline."""

    __tablename__ = "detection_state_overrides"
    __table_args__ = (
        ForeignKeyConstraint(
            ["raw_detection_id", "detection_import_id"],
            ["raw_detections.id", "raw_detections.detection_import_id"],
            ondelete="CASCADE",
            name="fk_detection_state_overrides_raw_import",
        ),
        CheckConstraint(
            f"display_track_id >= 0 AND display_track_id < {TRACK_ID_UPPER_BOUND}",
            name="ck_detection_state_overrides_display",
        ),
        CheckConstraint(
            "updated_edit_version >= 1", name="ck_detection_state_overrides_edit_version"
        ),
        Index(
            "ix_detection_state_overrides_import_display_suppressed",
            "detection_import_id",
            "display_track_id",
            "suppressed",
        ),
        Index(
            "ix_detection_state_overrides_import_version",
            "detection_import_id",
            "updated_edit_version",
        ),
    )

    raw_detection_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    detection_import_id: Mapped[int] = mapped_column(
        ForeignKey("detection_imports.id", ondelete="CASCADE"), nullable=False
    )
    display_track_id: Mapped[int] = mapped_column(Integer, nullable=False)
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_edit_version: Mapped[int] = mapped_column(Integer, nullable=False)

    detection_import: Mapped["DetectionImport"] = relationship(
        back_populates="state_overrides",
        foreign_keys=[detection_import_id],
        overlaps="raw_detection,state_override",
    )
    raw_detection: Mapped["RawDetection"] = relationship(
        back_populates="state_override",
        foreign_keys=[raw_detection_id, detection_import_id],
        overlaps="detection_import,state_overrides",
    )


class DraftIdentityEdit(Base):
    """Current draft's compact LIFO undo stack, not permanent audit history."""

    __tablename__ = "draft_identity_edits"
    __table_args__ = (
        UniqueConstraint(
            "detection_import_id",
            "applied_edit_version",
            name="uq_draft_identity_edits_import_version",
        ),
        UniqueConstraint("id", "detection_import_id", name="uq_draft_identity_edits_id_import"),
        CheckConstraint(
            "applied_edit_version >= 1", name="ck_draft_identity_edits_applied_version"
        ),
        CheckConstraint(
            "operation IN ('split', 'merge', 'suppress_track')",
            name="ck_draft_identity_edits_operation",
        ),
        Index(
            "ix_draft_identity_edits_import_version",
            "detection_import_id",
            text("applied_edit_version DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    detection_import_id: Mapped[int] = mapped_column(
        ForeignKey("detection_imports.id", ondelete="CASCADE"), nullable=False
    )
    applied_edit_version: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    params: Mapped[dict] = mapped_column(JSON, nullable=False)
    operator_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    detection_import: Mapped["DetectionImport"] = relationship(
        back_populates="draft_identity_edits"
    )
    operator: Mapped[Optional["User"]] = relationship(foreign_keys=[operator_id])
    changes: Mapped[list["DraftDetectionChange"]] = relationship(
        back_populates="edit",
        cascade="all, delete-orphan",
        passive_deletes=True,
        overlaps="raw_detection",
    )


class DraftDetectionChange(Base):
    """Before/after sparse override state for detections touched by one draft edit."""

    __tablename__ = "draft_detection_changes"
    __table_args__ = (
        PrimaryKeyConstraint("edit_id", "raw_detection_id"),
        ForeignKeyConstraint(
            ["edit_id", "detection_import_id"],
            ["draft_identity_edits.id", "draft_identity_edits.detection_import_id"],
            ondelete="CASCADE",
            name="fk_draft_detection_changes_edit_import",
        ),
        ForeignKeyConstraint(
            ["raw_detection_id", "detection_import_id"],
            ["raw_detections.id", "raw_detections.detection_import_id"],
            ondelete="CASCADE",
            name="fk_draft_detection_changes_raw_import",
        ),
        CheckConstraint(
            "(before_override_exists = 0 AND before_display_track_id IS NULL "
            "AND before_suppressed IS NULL) OR "
            f"(before_override_exists = 1 AND before_display_track_id >= 0 "
            f"AND before_display_track_id < {TRACK_ID_UPPER_BOUND} "
            "AND before_suppressed IS NOT NULL)",
            name="ck_draft_detection_changes_before",
        ),
        CheckConstraint(
            "(after_override_exists = 0 AND after_display_track_id IS NULL "
            "AND after_suppressed IS NULL) OR "
            f"(after_override_exists = 1 AND after_display_track_id >= 0 "
            f"AND after_display_track_id < {TRACK_ID_UPPER_BOUND} "
            "AND after_suppressed IS NOT NULL)",
            name="ck_draft_detection_changes_after",
        ),
        Index(
            "ix_draft_detection_changes_import_raw",
            "detection_import_id",
            "raw_detection_id",
        ),
    )

    edit_id: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_detection_id: Mapped[int] = mapped_column(Integer, nullable=False)
    detection_import_id: Mapped[int] = mapped_column(
        ForeignKey("detection_imports.id", ondelete="CASCADE"), nullable=False
    )
    before_override_exists: Mapped[bool] = mapped_column(Boolean, nullable=False)
    before_display_track_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    before_suppressed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    after_override_exists: Mapped[bool] = mapped_column(Boolean, nullable=False)
    after_display_track_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    after_suppressed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    edit: Mapped["DraftIdentityEdit"] = relationship(
        back_populates="changes",
        foreign_keys=[edit_id, detection_import_id],
        overlaps="raw_detection",
    )
    raw_detection: Mapped["RawDetection"] = relationship(
        foreign_keys=[raw_detection_id, detection_import_id], overlaps="changes,edit"
    )


class DetectionSnapshot(Base):
    """Immutable submit-time detection and pose metadata snapshot."""

    __tablename__ = "detection_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "detection_import_id",
            "source_edit_version",
            name="uq_detection_snapshots_import_version",
        ),
        UniqueConstraint("id", "detection_import_id", name="uq_detection_snapshots_id_import"),
        CheckConstraint("source_edit_version >= 0", name="ck_detection_snapshots_edit_version"),
        CheckConstraint("raw_detection_count >= 0", name="ck_detection_snapshots_raw_count"),
        CheckConstraint(
            "override_count >= 0 AND override_count <= raw_detection_count",
            name="ck_detection_snapshots_override_count",
        ),
        CheckConstraint("schema_version >= 1", name="ck_detection_snapshots_schema_version"),
        CheckConstraint("fps > 0", name="ck_detection_snapshots_fps"),
        CheckConstraint("width > 0 AND height > 0", name="ck_detection_snapshots_dimensions"),
        CheckConstraint("frame_count >= 0", name="ck_detection_snapshots_frame_count"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    detection_import_id: Mapped[int] = mapped_column(
        ForeignKey("detection_imports.id", ondelete="RESTRICT"), nullable=False
    )
    source_edit_version: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_detection_count: Mapped[int] = mapped_column(Integer, nullable=False)
    override_count: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    fps: Mapped[float] = mapped_column(Float, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_count: Mapped[int] = mapped_column(Integer, nullable=False)
    keypoint_names: Mapped[list] = mapped_column(JSON, nullable=False)
    skeleton_edges: Mapped[list] = mapped_column(JSON, nullable=False)
    raw_digest: Mapped[str] = mapped_column(String(64), default=lambda: "0" * 64, nullable=False)
    state_digest: Mapped[str] = mapped_column(String(64), default=lambda: "0" * 64, nullable=False)
    metadata_digest: Mapped[str] = mapped_column(String(64), default=lambda: "0" * 64, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    detection_import: Mapped["DetectionImport"] = relationship(
        back_populates="detection_snapshots"
    )
    states: Mapped[list["DetectionSnapshotState"]] = relationship(
        back_populates="snapshot", passive_deletes=True, overlaps="raw_detection"
    )
    submissions: Mapped[list["Submission"]] = relationship(back_populates="detection_snapshot")


class DetectionSnapshotState(Base):
    """Sparse immutable override rows copied from a draft at submit time."""

    __tablename__ = "detection_snapshot_states"
    __table_args__ = (
        PrimaryKeyConstraint("snapshot_id", "raw_detection_id"),
        ForeignKeyConstraint(
            ["snapshot_id", "detection_import_id"],
            ["detection_snapshots.id", "detection_snapshots.detection_import_id"],
            ondelete="RESTRICT",
            name="fk_detection_snapshot_states_snapshot_import",
        ),
        ForeignKeyConstraint(
            ["raw_detection_id", "detection_import_id"],
            ["raw_detections.id", "raw_detections.detection_import_id"],
            ondelete="RESTRICT",
            name="fk_detection_snapshot_states_raw_import",
        ),
        CheckConstraint(
            f"display_track_id >= 0 AND display_track_id < {TRACK_ID_UPPER_BOUND}",
            name="ck_detection_snapshot_states_display",
        ),
        Index(
            "ix_detection_snapshot_states_snapshot_display_suppressed",
            "snapshot_id",
            "display_track_id",
            "suppressed",
        ),
    )

    snapshot_id: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_detection_id: Mapped[int] = mapped_column(Integer, nullable=False)
    detection_import_id: Mapped[int] = mapped_column(
        ForeignKey("detection_imports.id", ondelete="RESTRICT"), nullable=False
    )
    display_track_id: Mapped[int] = mapped_column(Integer, nullable=False)
    suppressed: Mapped[bool] = mapped_column(Boolean, nullable=False)

    snapshot: Mapped["DetectionSnapshot"] = relationship(
        back_populates="states",
        foreign_keys=[snapshot_id, detection_import_id],
        overlaps="raw_detection",
    )
    raw_detection: Mapped["RawDetection"] = relationship(
        foreign_keys=[raw_detection_id, detection_import_id], overlaps="snapshot,states"
    )


class Submission(Base):
    """Immutable review attempt authority; lifecycle services arrive in Phase 3."""

    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint("video_id", "attempt_no", name="uq_submissions_video_attempt"),
        CheckConstraint("attempt_no >= 1", name="ck_submissions_attempt_no"),
        CheckConstraint(
            "source_annotation_version >= 0", name="ck_submissions_annotation_version"
        ),
        CheckConstraint("source_media_revision >= 0", name="ck_submissions_media_revision"),
        CheckConstraint(
            "status IN ('submitted', 'withdrawn', 'approved', 'rejected', 'superseded')",
            name="ck_submissions_status",
        ),
        CheckConstraint(
            "length(source_video_sha256) = 64 "
            "AND source_video_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_submissions_video_sha256",
        ),
        CheckConstraint(
            "length(source_storage_key) > 0 AND substr(source_storage_key, 1, 1) <> '/' "
            "AND source_storage_key NOT LIKE '%\\%' "
            "AND instr(source_storage_key, ':') = 0 "
            "AND source_storage_key NOT LIKE '%//%' "
            "AND ('/' || source_storage_key || '/') NOT LIKE '%/./%' "
            "AND ('/' || source_storage_key || '/') NOT LIKE '%/../%'",
            name="ck_submissions_storage_key",
        ),
        Index(
            "uq_submissions_video_submitted",
            "video_id",
            unique=True,
            sqlite_where=text("status = 'submitted'"),
        ),
        Index(
            "uq_submissions_video_approved",
            "video_id",
            unique=True,
            sqlite_where=text("status = 'approved'"),
        ),
        Index("ix_submissions_detection_snapshot_id", "detection_snapshot_id"),
        Index("ix_submissions_status_submitted_at", "status", "submitted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="RESTRICT"), nullable=False
    )
    detection_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("detection_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_annotation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_media_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_video_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    source_video_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_file_size: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    source_mtime_ns: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    source_device: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    source_inode: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    submitted_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    submitted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    legacy_backfill: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )

    video: Mapped["Video"] = relationship(foreign_keys=[video_id])
    detection_snapshot: Mapped["DetectionSnapshot"] = relationship(
        back_populates="submissions"
    )
    submitter: Mapped[Optional["User"]] = relationship(foreign_keys=[submitted_by])
    annotations: Mapped[list["SubmissionAnnotation"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan", passive_deletes=True
    )
    review: Mapped[Optional["Review"]] = relationship(
        back_populates="submission", uselist=False, foreign_keys="Review.submission_id"
    )


class SubmissionAnnotation(Base):
    """Immutable annotation copy attached to one Submission attempt."""

    __tablename__ = "submission_annotations"
    __table_args__ = (
        CheckConstraint(
            "start_time >= 0 AND end_time > start_time",
            name="ck_submission_annotations_time_range",
        ),
        CheckConstraint(
            "category_participant_mode IN ('unordered', 'role_based')",
            name="ck_submission_annotations_participant_mode",
        ),
        CheckConstraint(
            "start_frame >= 0 AND end_frame > start_frame",
            name="ck_submission_annotations_frame_range",
        ),
        CheckConstraint(
            "confidence IN ('certain', 'uncertain', 'occluded')",
            name="ck_submission_annotations_confidence",
        ),
        Index(
            "ix_submission_annotations_submission_category", "submission_id", "category_id"
        ),
        Index(
            "ix_submission_annotations_category_submission", "category_id", "submission_id"
        ),
        Index(
            "uq_submission_annotations_submission_source",
            "submission_id",
            "source_annotation_id",
            unique=True,
            sqlite_where=text("source_annotation_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False
    )
    source_annotation_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("annotations.id", ondelete="SET NULL"), nullable=True
    )
    category_id: Mapped[int] = mapped_column(
        ForeignKey("behavior_categories.id", ondelete="RESTRICT"), nullable=False
    )
    category_name: Mapped[str] = mapped_column(String(64), nullable=False)
    category_group: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    category_participant_mode: Mapped[str] = mapped_column(
        String(32), default="unordered", server_default="unordered", nullable=False
    )
    role_definitions_snapshot: Mapped[list] = mapped_column(
        JSON, default=list, server_default=text("'[]'"), nullable=False
    )
    participant_roles_snapshot: Mapped[dict] = mapped_column(
        JSON, default=dict, server_default=text("'{}'"), nullable=False
    )
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    start_frame: Mapped[int] = mapped_column(Integer, nullable=False)
    end_frame: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[str] = mapped_column(String(32), nullable=False)
    crop_region: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    mouse_ids: Mapped[list] = mapped_column(JSON, nullable=False)

    submission: Mapped["Submission"] = relationship(back_populates="annotations")
    source_annotation: Mapped[Optional["Annotation"]] = relationship(
        foreign_keys=[source_annotation_id]
    )
    category: Mapped["BehaviorCategory"] = relationship(foreign_keys=[category_id])
    clip: Mapped[Optional["Clip"]] = relationship(
        back_populates="submission_annotation",
        uselist=False,
        foreign_keys="Clip.submission_annotation_id",
    )


class CategorySchemeAudit(Base):
    """Append-only project category-scheme history."""

    __tablename__ = "category_scheme_audits"
    __table_args__ = (
        CheckConstraint("scheme_version >= 0", name="ck_category_scheme_audits_version"),
        CheckConstraint("action IN ('replace', 'lock')", name="ck_category_scheme_audits_action"),
        Index("ix_category_scheme_audits_project_created", "project_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    scheme_version: Mapped[int] = mapped_column(Integer, nullable=False)
    before_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    scheme_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    project: Mapped["Project"] = relationship(back_populates="category_scheme_audits")
    actor: Mapped["User"] = relationship(foreign_keys=[actor_id])
