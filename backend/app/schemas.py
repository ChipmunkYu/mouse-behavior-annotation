"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


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
    created_at: datetime


# ---------- 标注 ----------
class AnnotationCreate(BaseModel):
    category_id: int
    start_time: float = Field(ge=0)
    end_time: float = Field(ge=0)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    confidence: str = "certain"
    review_status: str = "pending"
    crop_region: Optional[dict[str, Any]] = None


class AnnotationUpdate(BaseModel):
    category_id: Optional[int] = None
    start_time: Optional[float] = Field(default=None, ge=0)
    end_time: Optional[float] = Field(default=None, ge=0)
    start_frame: Optional[int] = Field(default=None, ge=0)
    end_frame: Optional[int] = Field(default=None, ge=0)
    confidence: Optional[str] = None
    review_status: Optional[str] = None
    crop_region: Optional[dict[str, Any]] = None


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
