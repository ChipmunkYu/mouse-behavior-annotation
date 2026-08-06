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
}

export interface AnnotationPatchInput {
  category_id?: number;
  start_time?: number;
  end_time?: number;
  start_frame?: number;
  end_frame?: number;
  confidence?: string;
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
  annotation_revision: number;
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
  video_id: string;
  start_time: number;
  end_time: number;
  start_frame: number;
  end_frame: number;
  behavior: string | null;
  crop_region: unknown;
  confidence: string;
  annotator: string | null;
  reviewer: null;
  review_status: string;
}

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
