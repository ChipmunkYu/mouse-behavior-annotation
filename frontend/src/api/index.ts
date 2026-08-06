/**
 * 后端接口封装（路由与 backend/app/routers/* 一一对应）。
 */
import { apiFetch, apiRaw, ApiError, handleUnauthorized, uploadFile, type UploadProgress } from "./client";
import type {
  Annotation,
  AnnotationCreateInput,
  AnnotationPatchInput,
  Category,
  ExportEvent,
  Job,
  LoginResponse,
  MediaStatus,
  Project,
  ProjectCreateInput,
  Review,
  ReviewCreateInput,
  User,
  Video,
  VideoCreateInput,
} from "./types";

// ---------- 认证 ----------
export function login(username: string, password: string): Promise<LoginResponse> {
  return apiFetch<LoginResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

// ---------- 项目 ----------
export function listProjects(): Promise<Project[]> {
  return apiFetch<Project[]>("/projects");
}

export function createProject(input: ProjectCreateInput): Promise<Project> {
  return apiFetch<Project>("/projects", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

// ---------- 类别 ----------
export function listCategories(projectId: number | string): Promise<Category[]> {
  return apiFetch<Category[]>(`/projects/${projectId}/categories`);
}

// ---------- 视频 ----------
export function listVideos(projectId: number | string): Promise<Video[]> {
  return apiFetch<Video[]>(`/projects/${projectId}/videos`);
}

export function createVideo(projectId: number | string, input: VideoCreateInput): Promise<Video> {
  return apiFetch<Video>(`/projects/${projectId}/videos`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/**
 * 上传视频文件：POST /api/projects/:projectId/videos/upload（multipart field "file"）。
 * - 201 返回 Video；不兼容格式时后端可能返回 status = "needs_transcode"
 * - 通过 signal 取消上传（xhr.abort），取消时以 AbortError 拒绝
 * - 507 磁盘不足等错误统一为 ApiError（带可读信息）
 */
export function uploadVideo(
  projectId: number | string,
  file: File,
  options: { onProgress?: (p: UploadProgress) => void; signal?: AbortSignal }
): Promise<Video> {
  return uploadFile<Video>(`/projects/${projectId}/videos/upload`, file, {
    field: "file",
    filename: file.name,
    onProgress: options.onProgress,
    signal: options.signal,
  });
}

/**
 * 视频流需要 Bearer 认证，<video> 无法直接携带请求头，
 * 因此用带 token 的请求拉取 blob 并生成 object URL。
 */
export async function fetchVideoStreamUrl(videoId: number | string): Promise<string> {
  const res = await apiRaw(`/videos/${videoId}/stream`);
  if (res.status === 401) {
    // 与 apiFetch 一致：清除登录态并广播登出，避免 blob 请求 401 后界面仍停留在标注页
    handleUnauthorized();
    throw new ApiError(401, "登录已过期，请重新登录");
  }
  if (!res.ok) {
    throw new ApiError(res.status, `视频流不可用（HTTP ${res.status}）`);
  }
  const blob = await res.blob();
  if (blob.size === 0) {
    throw new ApiError(404, "视频文件为空");
  }
  return URL.createObjectURL(blob);
}

// ---------- 标注 ----------
export function listAnnotations(
  projectId: number | string,
  videoId: number | string
): Promise<Annotation[]> {
  return apiFetch<Annotation[]>(`/projects/${projectId}/videos/${videoId}/annotations`);
}

export function createAnnotation(
  projectId: number | string,
  videoId: number | string,
  input: AnnotationCreateInput
): Promise<Annotation> {
  return apiFetch<Annotation>(`/projects/${projectId}/videos/${videoId}/annotations`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function updateAnnotation(
  projectId: number | string,
  videoId: number | string,
  annotationId: number,
  patch: AnnotationPatchInput
): Promise<Annotation> {
  return apiFetch<Annotation>(
    `/projects/${projectId}/videos/${videoId}/annotations/${annotationId}`,
    {
      method: "PATCH",
      body: JSON.stringify(patch),
    }
  );
}

export function deleteAnnotation(
  projectId: number | string,
  videoId: number | string,
  annotationId: number
): Promise<void> {
  return apiFetch<void>(
    `/projects/${projectId}/videos/${videoId}/annotations/${annotationId}`,
    { method: "DELETE" }
  );
}

export function exportAnnotations(
  projectId: number | string,
  videoId: number | string
): Promise<ExportEvent[]> {
  return apiFetch<ExportEvent[]>(`/projects/${projectId}/videos/${videoId}/annotations/export`);
}

// ---------- 审核工作流 ----------

/**
 * 提交视频审核：POST /api/projects/:pid/videos/:vid/submit。
 * 要求至少有标注；成功返回更新后的 Video（workflow_status = "submitted"）。
 */
export function submitVideoForReview(
  projectId: number | string,
  videoId: number | string
): Promise<Video> {
  return apiFetch<Video>(`/projects/${projectId}/videos/${videoId}/submit`, {
    method: "POST",
  });
}

/** 审核队列：GET /api/projects/:pid/reviews/queue -> Video[]（仅待审核视频）。 */
export function listReviewQueue(projectId: number | string): Promise<Video[]> {
  return apiFetch<Video[]>(`/projects/${projectId}/reviews/queue`);
}

/** 视频审核历史：GET /api/projects/:pid/videos/:vid/reviews -> Review[]。 */
export function listVideoReviews(
  projectId: number | string,
  videoId: number | string
): Promise<Review[]> {
  return apiFetch<Review[]>(`/projects/${projectId}/videos/${videoId}/reviews`);
}

/** 提交审核结论：POST /api/projects/:pid/videos/:vid/review -> Review。 */
export function createVideoReview(
  projectId: number | string,
  videoId: number | string,
  input: ReviewCreateInput
): Promise<Review> {
  return apiFetch<Review>(`/projects/${projectId}/videos/${videoId}/review`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

// ---------- 媒体（片段）生成（批次 4） ----------

/**
 * 媒体（片段）生成状态汇总：
 * GET /api/projects/:pid/videos/:vid/media-status -> MediaStatus。
 * 仅 approved 视频有实际统计；非 approved 仅返回工作流信息。
 */
export function getMediaStatus(
  projectId: number | string,
  videoId: number | string
): Promise<MediaStatus> {
  return apiFetch<MediaStatus>(`/projects/${projectId}/videos/${videoId}/media-status`);
}

/** 触发片段生成：POST /api/projects/:pid/videos/:vid/media/generate -> Job。 */
export function generateMedia(
  projectId: number | string,
  videoId: number | string
): Promise<Job> {
  return apiFetch<Job>(`/projects/${projectId}/videos/${videoId}/media/generate`, {
    method: "POST",
  });
}

/** 查询后台任务：GET /api/projects/:pid/jobs/:jobId -> Job。 */
export function getJob(projectId: number | string, jobId: number | string): Promise<Job> {
  return apiFetch<Job>(`/projects/${projectId}/jobs/${jobId}`);
}

export type { User };
