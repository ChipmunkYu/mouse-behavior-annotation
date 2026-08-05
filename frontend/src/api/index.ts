/**
 * 后端接口封装（路由与 backend/app/routers/* 一一对应）。
 */
import { apiFetch, apiRaw, ApiError, handleUnauthorized } from "./client";
import type {
  Annotation,
  AnnotationCreateInput,
  AnnotationPatchInput,
  Category,
  ExportEvent,
  LoginResponse,
  Project,
  ProjectCreateInput,
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

export type { User };
