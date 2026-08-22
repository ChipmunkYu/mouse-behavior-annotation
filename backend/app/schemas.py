"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator, model_validator


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
class ProjectOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    status: str
    created_at: datetime
    role: str
    membership_id: int
    can_review: bool
    category_scheme_version: int = 0
    category_scheme_locked_at: Optional[datetime] = None
    category_scheme_locked_by: Optional[int] = None


class MembershipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    user_id: int
    username: str
    role: Literal["owner", "admin", "member"]
    can_review: bool
    status: str
    created_at: datetime


class MembershipUpdate(BaseModel):
    role: Optional[Literal["admin", "member"]] = None
    can_review: Optional[bool] = None


class InviteOut(BaseModel):
    invite_code: str


class JoinProjectRequest(BaseModel):
    invite_code: str = Field(min_length=20, max_length=64)


class AssigneeDirectoryItem(BaseModel):
    membership_id: int
    username: str


class AssigneeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    project_id: int
    user_id: int
    username: str
    role: Literal["owner", "admin", "member"]
    can_review: bool = Field(validation_alias="effective_can_review")
    status: str
    created_at: datetime


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
    # v0.6：参与小鼠数量范围（max 为 None 表示无固定上限）
    mouse_count_min: int = 1
    mouse_count_max: Optional[int] = None
    participant_mode: Literal["unordered", "role_based"] = "unordered"
    role_definitions: list[dict[str, Any]] = []


class CategorySchemeCategoryIn(BaseModel):
    id: Optional[int] = None
    name: str = Field(min_length=1, max_length=64)
    group: str = Field(min_length=1, max_length=64)
    color: Optional[str] = Field(default=None, max_length=32)
    sort_order: int = Field(ge=0, strict=True)
    is_active: bool = True
    participant_mode: Literal["unordered", "role_based"] = "unordered"
    role_definitions: list[dict[str, Any]] = []
    mouse_count_min: Optional[int] = Field(default=None, ge=1)
    mouse_count_max: Optional[int] = Field(default=None, ge=1)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: Optional[str] = None
    categories: list[CategorySchemeCategoryIn] = Field(min_length=1)

    @model_validator(mode="after")
    def require_category_colors(self) -> "ProjectCreate":
        if any(category.color is None or not category.color.strip() for category in self.categories):
            raise ValueError("Every category requires a non-blank color when creating a project")
        return self


class CategorySchemePut(BaseModel):
    expected_version: int = Field(ge=0)
    categories: list[CategorySchemeCategoryIn]


class CategorySchemeLock(BaseModel):
    expected_version: int = Field(ge=0)


class CategorySchemeOut(BaseModel):
    project_id: int
    category_scheme_version: int
    category_scheme_locked_at: Optional[datetime] = None
    category_scheme_locked_by: Optional[int] = None
    categories: list[CategoryOut]


class CategorySchemeAuditOut(BaseModel):
    id: int
    project_id: int
    actor_id: int
    action: Literal["replace", "lock"]
    scheme_version: int
    before_json: Optional[dict[str, Any]] = None
    after_json: dict[str, Any]
    scheme_hash: str
    created_at: datetime


# ---------- 视频 ----------
class VideoCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    duration: Optional[float] = Field(default=None, ge=0)
    fps: Optional[float] = Field(default=None, ge=0)
    width: Optional[int] = Field(default=None, ge=0)
    height: Optional[int] = Field(default=None, ge=0)
    status: Optional[str] = None
    storage_path: Optional[str] = None
    assignee_membership_id: Optional[int] = None


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
    detection_import_revision: int = 0
    identity_revision: int = 0
    submitted_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    approved_by: Optional[int] = None
    assignee_membership_id: Optional[int] = None
    assignee: Optional[AssigneeOut] = None
    created_at: datetime
    submission_annotations: list[dict[str, Any]] = []


class AssignmentBatchRequest(BaseModel):
    video_ids: list[int] = Field(min_length=1)
    assignee_membership_id: Optional[int] = None


class VideoClaimsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_ids: list[PositiveInt] = Field(min_length=1, max_length=200)

    @field_validator("video_ids")
    @classmethod
    def video_ids_must_be_unique(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("video_ids must be unique")
        return value


class VideoClaimsResponse(BaseModel):
    claimed_count: int
    videos: list[VideoOut]


class AssignmentStatsItem(BaseModel):
    assignee_membership_id: int
    username: str
    total: int
    draft: int
    submitted: int
    approved: int
    rejected: int


class AssignmentStatsOut(BaseModel):
    total: int
    draft: int
    submitted: int
    approved: int
    rejected: int
    unassigned: int
    claimable: int
    by_assignee: list[AssignmentStatsItem]


# ---------- 标注 ----------
class AnnotationCreate(BaseModel):
    category_id: int
    start_time: float = Field(ge=0)
    end_time: float = Field(ge=0)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    confidence: str = "certain"
    crop_region: Optional[dict[str, Any]] = None
    # v0.6：参与小鼠 ID + 修订号
    mouse_ids: Optional[list[int]] = None
    participant_roles: Optional[dict[str, Any]] = None
    detection_import_revision: Optional[int] = None
    identity_revision: Optional[int] = None
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
    # v0.6：参与小鼠 ID + 修订号
    mouse_ids: Optional[list[int]] = None
    participant_roles: Optional[dict[str, Any]] = None
    detection_import_revision: Optional[int] = None
    identity_revision: Optional[int] = None
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
    # v0.6：参与小鼠 ID 与状态
    mouse_ids: list[int] = []
    participant_roles: dict[str, list[int]] = {}
    participant_status: Literal["valid", "needs_participants"] = "valid"
    mouse_id_status: str = "needs_mouse_ids"
    detection_import_revision: int = 0
    identity_revision: int = 0
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
    detection_import_revision: int = 0
    identity_revision: int = 0
    created_at: datetime
    # 便捷字段：审核人用户名
    reviewer: Optional[str] = None
    submission_id: Optional[int] = None
    submission_annotations: list[dict[str, Any]] = []


# ---------- 后台任务 / 媒体状态（批次 4） ----------
class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: Optional[int] = None
    job_type: str
    status: str
    progress: int
    payload: Optional[dict[str, Any]] = None
    result_path: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class MediaStatusOut(BaseModel):
    video_id: int
    revision: int
    workflow_status: str
    total: int
    ready: int
    processing: int
    failed: int
    pending: int
    latest_job: Optional[JobOut] = None


# ---------- 跨视频片段库（批次 5） ----------
class ClipItem(BaseModel):
    """库中的一条审核通过标注，含对应 ready Clip 的相对路径（非 ready 为 null）。"""

    annotation_id: int
    video_id: int
    video_filename: str
    category_id: int
    category_name: str
    start_time: float
    end_time: float
    start_frame: int
    end_frame: int
    confidence: str
    clip_path: Optional[str] = None
    thumbnail_path: Optional[str] = None
    annotator_name: Optional[str] = None
    review_status: str
    created_at: datetime
    category_group: Optional[str] = None
    category_participant_mode: Literal["unordered", "role_based"] = "unordered"
    role_definitions: list[dict[str, Any]] = []
    participant_roles: dict[str, list[int]] = {}
    mouse_ids: list[int] = []


class ClipPageOut(BaseModel):
    items: list[ClipItem]
    total: int
    pages: int


class ClipCategoryCount(BaseModel):
    """分类筛选 chip：类别 + 审核通过片段计数。"""

    category_id: int
    category_name: str
    count: int


# ---------- 全项目分类导出（批次 6） ----------
class ExportRequest(BaseModel):
    """导出请求：可选 `category_ids` 限定类别（缺省导出全部审核通过片段）。"""

    category_ids: Optional[list[int]] = None


class MissingClipOut(BaseModel):
    """状态接口中缺失（未 ready）片段的标注信息。"""

    annotation_id: int
    category_name: str
    video_filename: str


class ExportStatusOut(BaseModel):
    """导出状态：最近任务 + 可导出/已就绪/缺失计数与缺失明细。"""

    latest_job: Optional[JobOut] = None
    exportable_count: int
    ready_count: int
    missing_count: int
    missing_clips: list[MissingClipOut]


# ---------- 身份编辑（Phase 2） ----------

class IdentityEditCheckRequest(BaseModel):
    operation: Literal["split", "merge"]
    track_ids: list[int] = Field(min_length=1)
    frame: Optional[int] = None
    base_identity_revision: int = Field(ge=0)
    base_detection_import_revision: int = Field(ge=1)


class IdentityEditCommitRequest(BaseModel):
    operation: Literal["split", "merge"]
    track_ids: list[int] = Field(min_length=1)
    frame: Optional[int] = None
    base_identity_revision: int = Field(ge=0)
    base_detection_import_revision: int = Field(ge=1)


class IdentityEditRevertRequest(BaseModel):
    base_identity_revision: int = Field(ge=0)
    base_detection_import_revision: int = Field(ge=1)


# ---------- 检测抑制（Phase 2） ----------

class SuppressionCreateRequest(BaseModel):
    scope: Literal["corrected_track"]
    track_id: int
    base_identity_revision: int = Field(ge=0)
    base_detection_import_revision: int = Field(ge=1)


class SuppressionRevertRequest(BaseModel):
    base_identity_revision: int = Field(ge=0)
    base_detection_import_revision: int = Field(ge=1)


# ---------- 检测导入与身份修正（Phase 1A 占位，未接入任何路由） ----------
class VideoImportBatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    status: str
    validation_errors: Optional[Any] = None
    video_upload_state: str
    tracks_upload_state: str
    metadata_upload_state: str
    video_path: Optional[str] = None
    video_filename: Optional[str] = None
    tracks_path: Optional[str] = None
    metadata_path: Optional[str] = None
    created_video_id: Optional[int] = None
    created_at: datetime


class DetectionImportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    video_id: int
    revision: int
    schema_version: str
    tracks_path: Optional[str] = None
    tracks_sha256: Optional[str] = None
    metadata_path: Optional[str] = None
    metadata_sha256: Optional[str] = None
    model: Optional[str] = Field(default=None, validation_alias="model_name")
    model_weights_sha256: Optional[str] = None
    tracker: Optional[str] = Field(default=None, validation_alias="tracker_name")
    tracker_params: Optional[dict[str, Any]] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    frame_count: Optional[int] = None
    frame_range: Optional[dict[str, Any]] = None
    detection_count: Optional[int] = None
    source_relative: Optional[str] = None
    status: str
    error: Optional[str] = None
    active: bool
    created_by: Optional[int] = None
    created_at: datetime


class RawDetectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    detection_import_id: int
    frame_index: int
    frame_detection_index: int
    raw_track_id: int
    box: Optional[dict[str, Any]] = None
    keypoints: Optional[list[Any]] = None
    confidence: Optional[float] = Field(default=None, validation_alias="detection_confidence")
    class_id: Optional[int] = None


class CorrectedTrackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    detection_import_id: int
    display_track_id: int
    first_frame: Optional[int] = None
    last_frame: Optional[int] = None
    effective_detection_count: int
    created_identity_revision: int
    active: bool
    merged_into_id: Optional[int] = None


class CorrectedDetectionAssignmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    raw_detection_id: int
    corrected_track_id: int
    identity_revision: int


class IdentityEditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    video_id: int
    detection_import_id: int
    operation: str
    base_identity_revision: int
    result_identity_revision: int
    params: Optional[dict[str, Any]] = None
    affected_detections: Optional[list[Any]] = None
    affected_annotations: Optional[list[Any]] = None
    operator_id: Optional[int] = None
    created_at: datetime
    reverted_edit_id: Optional[int] = None


class DetectionSuppressionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    video_id: int
    detection_import_id: int
    base_identity_revision: int
    result_identity_revision: int
    scope: str
    operator_id: Optional[int] = None
    created_at: datetime
    reverted_suppression_id: Optional[int] = None


# ---------- 检测导入 Phase 1B 响应模型 ----------

class BatchStatusOut(BaseModel):
    """批次状态（含文件上传状态与已创建视频 ID）。"""
    id: int
    project_id: int
    status: str
    validation_errors: Optional[Any] = None
    video_upload_state: str
    tracks_upload_state: str
    metadata_upload_state: str
    video_path: Optional[str] = None
    video_filename: Optional[str] = None
    tracks_path: Optional[str] = None
    metadata_path: Optional[str] = None
    created_video_id: Optional[int] = None
    created_at: datetime


class DetectionImportCurrentOut(BaseModel):
    """当前活动 DetectionImport 摘要。"""
    id: int
    revision: int
    schema_version: str
    model: Optional[str] = Field(default=None, validation_alias="model_name")
    tracker: Optional[str] = Field(default=None, validation_alias="tracker_name")
    frame_range: Optional[dict[str, Any]] = None
    detection_count: Optional[int] = None
    status: str
    fps: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None


class DetectionWithTrackOut(BaseModel):
    """detections 端点：带 display_track_id 与修订号的检测条目。"""
    detection_id: int
    frame_index: int
    raw_track_id: int
    display_track_id: int
    box_xyxy_px: Optional[list[float]] = None
    keypoints: Optional[list[Any]] = None
    confidence: Optional[float] = None
    import_revision: int
    identity_revision: int


class CorrectedTrackSummaryOut(BaseModel):
    """corrected-tracks 端点：轨迹摘要与当前帧可见性。"""
    display_track_id: int
    first_frame: Optional[int] = None
    last_frame: Optional[int] = None
    detection_count: int
    visible_in_current_frame: Optional[bool] = None


class DetectionImportReplacementPreviewOut(BaseModel):
    """检测替换确认前的结构化影响预览，不预测 role-based 最终状态。"""
    preview: Literal[True]
    message: str
    current_revision: int
    new_revision: int
    affected_annotations_count: int
    unordered_force_reselection_count: int
    role_based_revalidation_count: int
    detection_count: int
    unique_track_count: int


class DetectionImportReplacementConfirmedOut(BaseModel):
    """检测替换完成摘要；标注状态必须通过标注列表重新获取。"""
    preview: Literal[False] = False
    id: int
    video_id: int
    revision: int
    detection_count: int
    track_count: int
    status: str
    affected_annotations_count: int
    annotations_must_be_refetched: Literal[True]
    message: str


class PageOut(BaseModel):
    """通用分页包装。"""
    items: list[Any]
    total: int
    page: int
    page_size: int
    pages: int
