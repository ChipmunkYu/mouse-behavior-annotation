/**
 * 演示模式独有领域类型（后端尚未提供对应接口）：
 * 片段库、项目管理、导出任务等。仅由 src/demo/** 与对应页面使用。
 */

// ---------- 行为片段（由审核通过的标注派生） ----------
export interface Clip {
  id: number;
  project_id: number;
  video_id: number;
  video_filename: string;
  category_id: number;
  category_name: string;
  category_color: string | null;
  start_time: number;
  end_time: number;
  start_frame: number;
  end_frame: number;
  /** 片段时长（秒） */
  duration: number;
  /** 审核状态：approved / pending / rejected */
  review_status: string;
  annotator: string | null;
  /** 审核通过时间（导出时展示） */
  approved_at: string | null;
  /** 缩略图占位（SVG data URI，无二进制资源） */
  thumb: string;
}

// ---------- 项目管理 ----------
export type MemberRole = "owner" | "admin" | "annotator" | "reviewer";

export interface ProjectMember {
  id: number;
  username: string;
  role: MemberRole;
  /** 是否当前登录用户 */
  is_self: boolean;
  /** 演示用加入时间 */
  joined_at: string;
}

/** 视频标注任务分配（演示）：一个视频分配给某位标注者 */
export interface TaskAssignment {
  video_id: number;
  video_filename: string;
  video_workflow: string;
  annotator_id: number | null;
  annotator_name: string | null;
  /** assigned / in_progress / done */
  status: string;
}

// ---------- 存储概览 ----------
export interface StorageOverview {
  total_videos: number;
  total_bytes: number;
  by_workflow: Record<string, number>;
  by_status: Record<string, number>;
  /** 演示用磁盘占用（GB，示意值） */
  disk_used_gb: number;
  disk_total_gb: number;
}

// ---------- 导出中心 ----------
export interface ExportScope {
  /** 仅导出该类别（null = 全部） */
  category_id: number | null;
  /** 仅导出审核通过的片段（当前实现恒为 true） */
  approved_only: boolean;
}

export interface ExportPreview {
  /** 将导出的片段数 */
  clip_count: number;
  /** 覆盖的视频数 */
  video_count: number;
  /** 片段合计时长（秒） */
  total_seconds: number;
  /** 各行为类别数量 */
  by_category: { category_id: number; name: string; color: string | null; count: number }[];
  /** 将生成的目录结构（纯文本树，含 annotations.json） */
  tree: string;
  /** annotations.json 摘要（事件数 + 样例事件） */
  summary: {
    total_events: number;
    sample: Record<string, unknown>[];
  };
}

export type ExportTaskStatus = "running" | "completed" | "failed";

export interface ExportTask {
  id: number;
  name: string;
  scope: ExportScope;
  status: ExportTaskStatus;
  /** 0–100 */
  progress: number;
  clip_count: number;
  created_at: string;
  /** 演示保留 7 天（后端接入后由服务器清理） */
  expires_at: string;
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
}
