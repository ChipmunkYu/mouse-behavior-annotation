/**
 * 后端接口封装（路由与 backend/app/routers/* 一一对应）。
 * 演示模式（VITE_DEMO_MODE=true，npm run demo）下全部转发到 src/demo/api.ts，
 * 函数签名与返回类型完全一致；普通 dev / build 走下方真实 fetch 实现。
 */
import { apiFetch, apiRaw, ApiError, handleUnauthorized, uploadFile, type UploadProgress } from "./client";
import { DEMO_MODE } from "../demo/mode";
import * as demoApi from "../demo/api";
import type {
  Annotation,
  AnnotationCreateInput,
  AnnotationPatchInput,
  Category,
  ExportEvent,
  LoginResponse,
  Project,
  ProjectCreateInput,
  Review,
  ReviewCreateInput,
  User,
  Video,
  VideoCreateInput,
} from "./types";
import type {
  Clip,
  ExportPreview,
  ExportScope,
  ExportTask,
  MemberRole,
  ProjectMember,
  StorageOverview,
  TaskAssignment,
} from "../demo/types";

// ---------- 认证 ----------
export function login(username: string, password: string): Promise<LoginResponse> {
  if (DEMO_MODE) return demoApi.login(username, password);
  return apiFetch<LoginResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

// ---------- 项目 ----------
export function listProjects(): Promise<Project[]> {
  if (DEMO_MODE) return demoApi.listProjects();
  return apiFetch<Project[]>("/projects");
}

export function createProject(input: ProjectCreateInput): Promise<Project> {
  if (DEMO_MODE) return demoApi.createProject(input);
  return apiFetch<Project>("/projects", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

// ---------- 类别 ----------
export function listCategories(projectId: number | string): Promise<Category[]> {
  if (DEMO_MODE) return demoApi.listCategories(projectId);
  return apiFetch<Category[]>(`/projects/${projectId}/categories`);
}

// ---------- 视频 ----------
export function listVideos(projectId: number | string): Promise<Video[]> {
  if (DEMO_MODE) return demoApi.listVideos(projectId);
  return apiFetch<Video[]>(`/projects/${projectId}/videos`);
}

export function createVideo(projectId: number | string, input: VideoCreateInput): Promise<Video> {
  if (DEMO_MODE) return demoApi.createVideo(projectId, input);
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
 * 演示模式：本地模拟进度 / 取消 / 成功，不产生任何网络请求。
 */
export function uploadVideo(
  projectId: number | string,
  file: File,
  options: { onProgress?: (p: UploadProgress) => void; signal?: AbortSignal }
): Promise<Video> {
  if (DEMO_MODE) return demoApi.uploadVideo(projectId, file, options);
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
 * 演示模式：页面不会调用本函数（改用内置演示画面）。
 */
export async function fetchVideoStreamUrl(videoId: number | string): Promise<string> {
  if (DEMO_MODE) return demoApi.fetchVideoStreamUrl();
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
  if (DEMO_MODE) return demoApi.listAnnotations(projectId, videoId);
  return apiFetch<Annotation[]>(`/projects/${projectId}/videos/${videoId}/annotations`);
}

export function createAnnotation(
  projectId: number | string,
  videoId: number | string,
  input: AnnotationCreateInput
): Promise<Annotation> {
  if (DEMO_MODE) return demoApi.createAnnotation(projectId, videoId, input);
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
  if (DEMO_MODE) return demoApi.updateAnnotation(projectId, videoId, annotationId, patch);
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
  if (DEMO_MODE) return demoApi.deleteAnnotation(projectId, videoId, annotationId);
  return apiFetch<void>(
    `/projects/${projectId}/videos/${videoId}/annotations/${annotationId}`,
    { method: "DELETE" }
  );
}

export function exportAnnotations(
  projectId: number | string,
  videoId: number | string
): Promise<ExportEvent[]> {
  if (DEMO_MODE) return demoApi.exportAnnotations(projectId, videoId);
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
  if (DEMO_MODE) return demoApi.submitVideoForReview(projectId, videoId);
  return apiFetch<Video>(`/projects/${projectId}/videos/${videoId}/submit`, {
    method: "POST",
  });
}

/** 审核队列：GET /api/projects/:pid/reviews/queue -> Video[]（仅待审核视频）。 */
export function listReviewQueue(projectId: number | string): Promise<Video[]> {
  if (DEMO_MODE) return demoApi.listReviewQueue(projectId);
  return apiFetch<Video[]>(`/projects/${projectId}/reviews/queue`);
}

/** 视频审核历史：GET /api/projects/:pid/videos/:vid/reviews -> Review[]。 */
export function listVideoReviews(
  projectId: number | string,
  videoId: number | string
): Promise<Review[]> {
  if (DEMO_MODE) return demoApi.listVideoReviews(projectId, videoId);
  return apiFetch<Review[]>(`/projects/${projectId}/videos/${videoId}/reviews`);
}

/** 提交审核结论：POST /api/projects/:pid/videos/:vid/review -> Review。 */
export function createVideoReview(
  projectId: number | string,
  videoId: number | string,
  input: ReviewCreateInput
): Promise<Review> {
  if (DEMO_MODE) return demoApi.createVideoReview(projectId, videoId, input);
  return apiFetch<Review>(`/projects/${projectId}/videos/${videoId}/review`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

// ================= 演示模式扩展接口（后端尚未接入，真实模式返回明确提示） =================

/** 未接入后端时统一抛出，避免页面误以为真实接口可用。 */
function notImplemented(name: string): ApiError {
  return new ApiError(501, `「${name}」为演示功能，后端尚未接入；请以 npm run demo 查看演示效果`);
}

/** 片段库：审核通过的跨视频行为片段。 */
export function listClips(projectId: number | string): Promise<Clip[]> {
  if (DEMO_MODE) return demoApi.listClips(projectId);
  return Promise.reject(notImplemented("片段库"));
}

/** 导出预览：目录结构 + annotations.json 摘要。 */
export function getExportPreview(projectId: number | string, scope: ExportScope): Promise<ExportPreview> {
  if (DEMO_MODE) return demoApi.getExportPreview(projectId, scope);
  return Promise.reject(notImplemented("导出预览"));
}

/** 创建导出任务（演示模式返回后由本地模拟后台推进）。 */
export function createExportTask(projectId: number | string, scope: ExportScope): Promise<ExportTask> {
  if (DEMO_MODE) return demoApi.createExportTask(projectId, scope);
  return Promise.reject(notImplemented("导出任务"));
}

export function listExportTasks(): Promise<ExportTask[]> {
  if (DEMO_MODE) return demoApi.listExportTasks();
  return Promise.reject(notImplemented("导出任务列表"));
}

/** 项目管理：成员与项目内角色。 */
export function listProjectMembers(projectId: number | string): Promise<ProjectMember[]> {
  if (DEMO_MODE) return demoApi.listProjectMembers();
  void projectId;
  return Promise.reject(notImplemented("项目成员"));
}

export function setMemberRole(
  projectId: number | string,
  memberId: number,
  role: MemberRole
): Promise<ProjectMember[]> {
  if (DEMO_MODE) return demoApi.setMemberRole(projectId, memberId, role);
  return Promise.reject(notImplemented("修改成员角色"));
}

export function addMember(projectId: number | string, username: string, role: MemberRole): Promise<ProjectMember[]> {
  if (DEMO_MODE) return demoApi.addMember(projectId, username, role);
  return Promise.reject(notImplemented("添加成员"));
}

export function removeMember(projectId: number | string, memberId: number): Promise<ProjectMember[]> {
  if (DEMO_MODE) return demoApi.removeMember(projectId, memberId);
  return Promise.reject(notImplemented("移除成员"));
}

/** 项目管理：视频标注任务分配。 */
export function listAssignments(projectId: number | string): Promise<TaskAssignment[]> {
  if (DEMO_MODE) return demoApi.listAssignments(projectId);
  return Promise.reject(notImplemented("任务分配"));
}

export function setAssignment(projectId: number | string, videoId: number, annotatorId: number | null): Promise<TaskAssignment[]> {
  if (DEMO_MODE) return demoApi.setAssignment(projectId, videoId, annotatorId);
  return Promise.reject(notImplemented("任务分配"));
}

/** 项目管理：行为类别启停。 */
export function setCategoryActive(projectId: number | string, categoryId: number, isActive: boolean): Promise<Category[]> {
  if (DEMO_MODE) return demoApi.setCategoryActive(projectId, categoryId, isActive);
  return Promise.reject(notImplemented("类别启停"));
}

/** 项目管理：存储概览。 */
export function getStorageOverview(projectId: number | string): Promise<StorageOverview> {
  if (DEMO_MODE) return demoApi.getStorageOverview(projectId);
  return Promise.reject(notImplemented("存储概览"));
}

/** 重置演示数据（仅演示模式生效，清空 localStorage 并重新种子）。 */
export function resetDemoData(): void {
  if (DEMO_MODE) demoApi.resetDemo();
}

export type { User };
