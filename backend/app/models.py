"""数据模型：User / Project / ProjectMembership / BehaviorCategory / Video / Annotation。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


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


class ProjectMembership(Base):
    """用户在某项目内的身份；user_id + project_id 唯一。"""

    __tablename__ = "project_memberships"
    __table_args__ = (UniqueConstraint("user_id", "project_id", name="uq_membership_user_project"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(32), default="annotator", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped["User"] = relationship(back_populates="memberships")
    project: Mapped["Project"] = relationship(back_populates="members")


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    project: Mapped["Project"] = relationship(back_populates="categories")
    annotations: Mapped[list["Annotation"]] = relationship(back_populates="category")


class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (
        CheckConstraint("annotation_revision >= 1", name="ck_videos_annotation_revision_min"),
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
    __table_args__ = (Index("ix_reviews_video_revision", "video_id", "annotation_revision"),)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    project: Mapped["Project"] = relationship()
    video: Mapped["Video"] = relationship(back_populates="reviews")
    reviewer: Mapped[Optional["User"]] = relationship()


class Clip(Base):
    """行为标注片段：修订隔离（annotation_id + source_revision 唯一）。

    已审核标注被修改后，新修订生成新的 Clip 记录；未来生命周期中旧 Clip 会被显式删除。
    """

    __tablename__ = "clips"
    __table_args__ = (
        UniqueConstraint("annotation_id", "source_revision", name="uq_clip_annotation_revision"),
        Index("ix_clips_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    annotation_id: Mapped[int] = mapped_column(
        ForeignKey("annotations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # v0.6：媒体修订与语义修订拆分——仅源媒体变化时才需要重编码 Clip
    media_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # pending / processing / ready / failed / stale
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    clip_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    thumbnail_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    project: Mapped["Project"] = relationship()
    annotation: Mapped["Annotation"] = relationship(back_populates="clips")


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
        Index("ix_raw_detections_import_frame", "detection_import_id", "frame_index"),
        Index("ix_raw_detections_import_track", "detection_import_id", "raw_track_id"),
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
