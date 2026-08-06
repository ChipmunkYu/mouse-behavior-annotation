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

// ---------- 通用 ----------
export const ROLE_LABELS: Record<string, string> = {
  owner: "所有者",
  admin: "管理员",
  annotator: "标注者",
  reviewer: "审核者",
};

export const DEFAULT_FPS = 30;
