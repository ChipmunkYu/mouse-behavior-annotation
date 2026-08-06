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
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_category_project_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    group: Mapped[str] = mapped_column(String(64), nullable=False)
    color: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
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

    project: Mapped["Project"] = relationship(back_populates="videos")
    annotations: Mapped[list["Annotation"]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )
    reviews: Mapped[list["Review"]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
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
