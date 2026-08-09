/**
 * 与 backend/app/schemas.py 及 backend/app/routers/* 返回结构对应的类型定义。
 * 字段命名与后端 Pydantic 输出一致（snake_case）。
 */

// ---------- 认证 ----------
export interface User {
  id: number;
  username: string;
  created_at: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  user: User;
}

// ---------- 项目 ----------
export interface Project {
  id: number;
  name: string;
  description: string | null;
  status: string;
  created_at: string;
  /** 当前用户在该项目内的角色：owner / admin / annotator / reviewer */
  role: string;
}

export interface ProjectCreateInput {
  name: string;
  description?: string | null;
}

// ---------- 行为类别 ----------
export interface Category {
  id: number;
  project_id: number;
  name: string;
  group: string;
  color: string | null;
  sort_order: number;
  is_active: boolean;
  mouse_count_min: number;
  mouse_count_max: number | null;
}

// ---------- 视频 ----------
export interface Video {
  id: number;
  project_id: number;
  filename: string;
  duration: number | null;
  fps: number | null;
  width: number | null;
  height: number | null;
  storage_path: string | null;
  status: string;
  // 审核工作流字段（批次 3）
  workflow_status: string;
  annotation_revision: number;
  detection_import_revision?: number;
  identity_revision?: number;
  submitted_at: string | null;
  approved_at: string | null;
  approved_by: number | null;
  created_at: string;
}

export interface VideoCreateInput {
  filename: string;
  duration?: number | null;
  fps?: number | null;
  width?: number | null;
  height?: number | null;
  status?: string | null;
  storage_path?: string | null;
}

// ---------- 标注 ----------
export interface Annotation {
  id: number;
  video_id: number;
  annotator_id: number;
  category_id: number;
  start_time: number;
  end_time: number;
  start_frame: number;
  end_frame: number;
  confidence: string;
  review_status: string;
  crop_region: unknown;
  mouse_ids: number[];
  mouse_id_status: "valid" | "needs_mouse_ids";
  detection_import_revision: number;
  identity_revision: number;
  created_at: string;
  updated_at: string;
  /** 便捷字段：标注者用户名 / 类别名（后端返回） */
  annotator: string | null;
  category_name: string | null;
}

export interface AnnotationCreateInput {
  category_id: number;
  start_time: number;
  end_time: number;
  start_frame: number;
  end_frame: number;
  confidence?: string;
  crop_region?: unknown;
  mouse_ids?: number[];
  detection_import_revision?: number;
  identity_revision?: number;
}

export interface AnnotationPatchInput {
  category_id?: number;
  start_time?: number;
  end_time?: number;
  start_frame?: number;
  end_frame?: number;
  confidence?: string;
  crop_region?: unknown;
  mouse_ids?: number[];
  detection_import_revision?: number;
  identity_revision?: number;
}

// ---------- 审核工作流 ----------
/** 视频审核工作流状态：draft → submitted → approved / rejected */
export type WorkflowStatus = "draft" | "submitted" | "approved" | "rejected";

export interface Review {
  id: number;
  project_id: number;
  video_id: number;
  reviewer_id: number | null;
  result: "approved" | "rejected";
  comment: string | null;
  annotation_revision: number;
  detection_import_revision: number;
  identity_revision: number;
  created_at: string;
  /** 便捷字段：审核人用户名（后端返回时显示） */
  reviewer: string | null;
}

export interface ReviewCreateInput {
  result: "approved" | "rejected";
  comment?: string | null;
}

export const WORKFLOW_LABELS: Record<string, string> = {
  draft: "草稿",
  submitted: "待审核",
  approved: "已通过",
  rejected: "已退回",
};

// ---------- 后台任务与媒体（片段）生成（批次 4） ----------
/**
 * 后台任务（与 backend/app/models.py BackgroundJob 对齐）：
 * 状态枚举 queued / running / succeeded / failed / cancelled。
 * 字段命名以当前后端基础模型为准，核对最终实现字段时在此处修正即可。
 */
export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface Job {
  id: number;
  project_id: number | null;
  job_type: string;
  status: JobStatus;
  /** 0–100 整数进度 */
  progress: number;
  payload: unknown;
  result_path: string | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  expires_at: string | null;
}

/** 单个视频的媒体（片段）生成状态汇总（GET /media-status）。 */
export interface MediaStatus {
  video_id: number;
  revision: number;
  workflow_status: string;
  /** 片段总数 */
  total: number;
  ready: number;
  processing: number;
  failed: number;
  pending: number;
  /** 最近一次生成任务（尚未生成过则为 null） */
  latest_job: Job | null;
}

export const JOB_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "处理中",
  succeeded: "已完成",
  failed: "生成失败",
  cancelled: "已取消",
};

// ---------- 导出 ----------
export interface ExportEvent {
  annotation_id: number;
  video_id: string;
  clip_file: string | null;
  start_time: number;
  end_time: number;
  start_frame: number;
  end_frame: number;
  behavior: string | null;
  mouse_ids: number[];
  detection_import_revision: number;
  identity_revision: number;
  crop_region: unknown;
  confidence: string;
  annotator: string | null;
  reviewer: string | null;
  review_status: string;
}

// ---------- 检测结果导入与 track 修正 ----------
export type ImportFileRole = "video" | "tracks" | "metadata";

export interface VideoImportBatch {
  id: number;
  project_id: number;
  status: string;
  video_upload_state: string;
  tracks_upload_state: string;
  metadata_upload_state: string;
  video_path?: string | null;
  video_filename?: string | null;
  tracks_path?: string | null;
  metadata_path?: string | null;
  created_video_id: number | null;
  validation_errors: Record<string, unknown> | null;
  created_at?: string;
}

export interface DetectionImport {
  id: number;
  video_id?: number;
  revision: number;
  schema_version: string;
  model?: string | null;
  tracker?: string | null;
  frame_range: Record<string, unknown> | null;
  detection_count: number | null;
  status: string;
  fps: number | null;
  width: number | null;
  height: number | null;
}

export interface Keypoint {
  x_px?: number;
  y_px?: number;
  x?: number;
  y?: number;
  confidence?: number;
}

export interface DetectionWithTrack {
  detection_id: number;
  frame_index: number;
  raw_track_id: number;
  display_track_id: number;
  box_xyxy_px: number[] | null;
  keypoints: Keypoint[] | null;
  confidence?: number | null;
  import_revision: number;
  identity_revision: number;
}

export interface DetectionsResponse { detections: DetectionWithTrack[]; total: number }

export interface CorrectedTrackSummary {
  display_track_id: number;
  first_frame: number | null;
  last_frame: number | null;
  detection_count: number;
  visible_in_current_frame: boolean | null;
}

export interface CorrectedTracksResponse {
  items: CorrectedTrackSummary[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

export interface CorrectedTracksParams {
  current_frame?: number;
  search?: string;
  page?: number;
  page_size?: number;
}

export interface IdentityEditCheckRequest {
  operation: "split" | "merge";
  track_ids: number[];
  frame?: number | null;
  base_identity_revision: number;
  base_detection_import_revision: number;
}

export interface IdentityEditCheckResponse {
  operation: "split" | "merge";
  old_display_track_id?: number;
  new_display_track_id?: number;
  split_frame?: number;
  detections_before?: number;
  detections_after?: number;
  retained_display_track_id?: number;
  merged_display_track_ids?: number[];
  affected_detection_count?: number;
  affected_annotation_count: number;
  conflict_frames?: number[];
}

export type IdentityEditCommitRequest = IdentityEditCheckRequest;

export interface IdentityEditResult {
  edit_id?: number;
  identity_revision: number;
  new_display_track_id?: number;
  retained_display_track_id?: number;
  affected_detection_count?: number;
  affected_annotation_count?: number;
  needs_mouse_ids_annotation_ids?: number[];
}

export interface IdentityEdit {
  id: number;
  video_id: number;
  detection_import_id: number;
  operation: string;
  base_identity_revision: number;
  result_identity_revision: number;
  params: Record<string, unknown> | null;
  affected_detections: unknown[] | null;
  affected_annotations: unknown[] | null;
  operator_id: number | null;
  created_at: string;
  reverted_edit_id: number | null;
}

export interface SuppressionCreateRequest {
  scope: "corrected_track";
  track_id: number;
  base_identity_revision: number;
  base_detection_import_revision: number;
}

export interface SuppressionResult {
  suppression_id?: number;
  identity_revision: number;
  frozen_detection_count?: number;
  freed_detection_count?: number;
  affected_track_ids?: number[];
}

export interface DetectionSuppression {
  id: number;
  scope: string;
  result_identity_revision: number;
  created_at: string;
  frozen_detection_count: number;
}

export interface CorrectedTracksExport {
  tracks_corrected: string[];
  manifest: Record<string, unknown>;
}

// ---------- 导出（批次 6） ----------
/** 发起导出请求体：category_ids 为空数组 / 缺省表示导出全部类别（POST /api/projects/:pid/export）。 */
export interface ExportRequestInput {
  category_ids?: number[];
}

/** 缺失片段条目（导出 status 返回）：标注已审核通过、但剪辑文件尚未生成的片段。 */
export interface MissingClip {
  annotation_id: number;
  category_name: string;
  video_filename: string;
}

/**
 * 导出状态汇总（GET /api/projects/:pid/export/status）。
 * 计数均为项目级：可导出 = 审核通过标注总数；就绪 = 片段已生成可打包；缺失 = 待生成。
 */
export interface ExportStatus {
  /** 最近一次导出任务（尚未发起过则为 null） */
  latest_job: Job | null;
  /** 可导出（审核通过）标注总数 */
  exportable_count: number;
  /** 片段已就绪、可打包进 ZIP 的数量 */
  ready_count: number;
  /** 片段缺失（未生成）数量 */
  missing_count: number;
  /** 缺失片段明细（仅缺失时非空） */
  missing_clips: MissingClip[];
}

/** 导出 ZIP 在服务器端的保留时长（天），下载提示文案用。 */
export const EXPORT_RETENTION_DAYS = 7;

// ---------- 片段库（批次 5） ----------
/**
 * 片段列表条目（GET /api/projects/:pid/clips 返回的 ClipItem）。
 * 字段与后端 Planned ClipOut 对齐：clip_path / thumbnail_path 为空表示片段尚未生成。
 */
export interface ClipItem {
  annotation_id: number;
  video_id: number;
  video_filename: string;
  category_id: number;
  category_name: string;
  start_time: number;
  end_time: number;
  start_frame: number;
  end_frame: number;
  confidence: string;
  clip_path: string | null;
  thumbnail_path: string | null;
  annotator_name: string | null;
  /** 标注审核状态：pending / approved / rejected */
  review_status: string;
  created_at: string;
}

/** 分页响应：{items, total, pages}。 */
export interface ClipListResponse {
  items: ClipItem[];
  total: number;
  pages: number;
}

/** 类别计数（GET /api/projects/:pid/clips/categories），用于筛选 chips。 */
export interface ClipCategoryCount {
  category_id: number;
  category_name: string;
  count: number;
}

/** 片段列表的过滤与分页参数（与 GET /clips 查询参数一一对应，全部类型化）。 */
export interface ClipListParams {
  category_id?: number | null;
  video_id?: number | null;
  /** 搜索关键词：匹配类别名或视频文件名（大小写不敏感，服务端过滤）。 */
  search?: string | null;
  page: number;
  page_size: number;
}

/** 片段库默认每页条数（后端默认值一致）。 */
export const DEFAULT_PAGE_SIZE = 20;

// ---------- 通用 ----------
export const ROLE_LABELS: Record<string, string> = {
  owner: "所有者",
  admin: "管理员",
  annotator: "标注者",
  reviewer: "审核者",
};

export const DEFAULT_FPS = 30;
