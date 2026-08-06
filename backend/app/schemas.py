"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------- 认证 ----------
class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    created_at: datetime


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


# ---------- 项目 ----------
class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: Optional[str] = None


class ProjectOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    status: str
    created_at: datetime
    role: str


# ---------- 行为类别 ----------
class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    group: str
    color: Optional[str] = None
    sort_order: int
    is_active: bool


# ---------- 视频 ----------
class VideoCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    duration: Optional[float] = Field(default=None, ge=0)
    fps: Optional[float] = Field(default=None, ge=0)
    width: Optional[int] = Field(default=None, ge=0)
    height: Optional[int] = Field(default=None, ge=0)
    status: Optional[str] = None
    storage_path: Optional[str] = None


class VideoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    filename: str
    duration: Optional[float] = None
    fps: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    storage_path: Optional[str] = None
    status: str
    # 审核工作流字段（新增；旧数据迁移后由 DB 默认值填充）
    workflow_status: str = "draft"
    annotation_revision: int = 1
    submitted_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    approved_by: Optional[int] = None
    created_at: datetime


# ---------- 标注 ----------
class AnnotationCreate(BaseModel):
    category_id: int
    start_time: float = Field(ge=0)
    end_time: float = Field(ge=0)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    confidence: str = "certain"
    crop_region: Optional[dict[str, Any]] = None
    # review_status 不作为有效输入：创建固定 pending；显式传入非 pending 值 → 422
    review_status: Optional[str] = None

    @field_validator("review_status")
    @classmethod
    def _reject_direct_review_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v != "pending":
            raise ValueError(
                "review_status cannot be set on create; annotations are created as 'pending'"
            )
        return v


class AnnotationUpdate(BaseModel):
    category_id: Optional[int] = None
    start_time: Optional[float] = Field(default=None, ge=0)
    end_time: Optional[float] = Field(default=None, ge=0)
    start_frame: Optional[int] = Field(default=None, ge=0)
    end_frame: Optional[int] = Field(default=None, ge=0)
    confidence: Optional[str] = None
    crop_region: Optional[dict[str, Any]] = None
    # 禁止用户直接写 review_status：审核状态只能通过审核 API（review）流转
    review_status: Optional[str] = None

    @field_validator("review_status")
    @classmethod
    def _reject_direct_review_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            raise ValueError("review_status cannot be set directly; use the review API")
        return v


class AnnotationOut(BaseModel):
    id: int
    video_id: int
    annotator_id: int
    category_id: int
    start_time: float
    end_time: float
    start_frame: int
    end_frame: int
    confidence: str
    review_status: str
    crop_region: Optional[dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime
    # 便捷字段：标注者用户名 / 类别名
    annotator: Optional[str] = None
    category_name: Optional[str] = None


# ---------- 审核 ----------
class ReviewCreate(BaseModel):
    result: Literal["approved", "rejected"]
    comment: Optional[str] = None


class ReviewOut(BaseModel):
    id: int
    project_id: int
    video_id: int
    reviewer_id: Optional[int] = None
    result: str
    comment: Optional[str] = None
    annotation_revision: int
    created_at: datetime
    # 便捷字段：审核人用户名
    reviewer: Optional[str] = None
