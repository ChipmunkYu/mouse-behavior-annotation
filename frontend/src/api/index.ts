/**
 * 后端接口封装（路由与 backend/app/routers/* 一一对应）。
 */
import { apiFetch, apiRaw, ApiError, handleUnauthorized, uploadFile, type UploadProgress } from "./client";
import type {
  Annotation,
  AnnotationCreateInput,
  AnnotationPatchInput,
  Category,
  ClipCategoryCount,
  ClipListParams,
  ClipListResponse,
  ExportEvent,
  ExportRequestInput,
  ExportStatus,
  IdentityEdit as IdentityEditRecord,
  Job,
  LoginResponse,
  MediaStatus,
  Project,
  ProjectCreateInput,
  Review,
  ReviewCreateInput,
  User,
  Video,
  VideoClaimsInput,
  VideoClaimsResponse,
  VideoCreateInput,
  CorrectedTracksExport,
  CorrectedTracksParams,
  CorrectedTracksResponse,
  DetectionImport,
  DetectionSuppression,
  DetectionsResponse,
  IdentityEditCheckRequest,
  IdentityEditCheckResponse,
  IdentityEditCommitRequest,
  IdentityEditResult,
  ImportFileRole,
  SuppressionCreateRequest,
  SuppressionResult,
  VideoImportBatch,
  AssignmentStats,
  Membership,
  MembershipUpdateInput,
  Invite,
  VideoListParams,
  AssigneeDirectoryItem,
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

export function joinProject(invite_code: string): Promise<Membership> {
  return apiFetch<Membership>("/projects/join", { method: "POST", body: JSON.stringify({ invite_code }) });
}
export function listMembers(projectId: number | string): Promise<Membership[]> {
  return apiFetch<Membership[]>(`/projects/${projectId}/members`);
}
export function listAssignees(projectId: number | string): Promise<AssigneeDirectoryItem[]> {
  return apiFetch<AssigneeDirectoryItem[]>(`/projects/${projectId}/assignees`);
}
export function updateMember(projectId: number | string, membershipId: number, input: MembershipUpdateInput): Promise<Membership> {
  return apiFetch<Membership>(`/projects/${projectId}/members/${membershipId}`, { method: "PATCH", body: JSON.stringify(input) });
}
export function removeMember(projectId: number | string, membershipId: number): Promise<void> {
  return apiFetch<void>(`/projects/${projectId}/members/${membershipId}`, { method: "DELETE" });
}
export function getProjectInvite(projectId: number | string): Promise<Invite> {
  return apiFetch<Invite>(`/projects/${projectId}/invite`);
}
export function resetProjectInvite(projectId: number | string): Promise<Invite> {
  return apiFetch<Invite>(`/projects/${projectId}/invite/reset`, { method: "POST" });
}

// ---------- 类别 ----------
export function listCategories(projectId: number | string): Promise<Category[]> {
  return apiFetch<Category[]>(`/projects/${projectId}/categories`);
}

// ---------- 视频 ----------
export function listVideos(projectId: number | string, params: VideoListParams = {}): Promise<Video[]> {
  const qs = new URLSearchParams();
  if (params.view) qs.set("view", params.view);
  if (params.workflow_status) qs.set("workflow_status", params.workflow_status);
  if (params.assignee_membership_id != null) qs.set("assignee_membership_id", String(params.assignee_membership_id));
  return apiFetch<Video[]>(`/projects/${projectId}/videos${qs.size ? `?${qs}` : ""}`);
}

export function claimVideo(projectId: number | string, videoId: number): Promise<Video> {
  return apiFetch<Video>(`/projects/${projectId}/videos/${videoId}/claim`, { method: "POST" });
}
export function claimVideos(projectId: number | string, input: VideoClaimsInput): Promise<VideoClaimsResponse> {
  return apiFetch<VideoClaimsResponse>(`/projects/${projectId}/videos/claims`, { method: "POST", body: JSON.stringify(input) });
}
export function releaseVideo(projectId: number | string, videoId: number): Promise<Video> {
  return apiFetch<Video>(`/projects/${projectId}/videos/${videoId}/release`, { method: "POST" });
}
export function batchAssignVideos(projectId: number | string, video_ids: number[], assignee_membership_id: number | null): Promise<Video[]> {
  return apiFetch<Video[]>(`/projects/${projectId}/videos/assignments`, { method: "POST", body: JSON.stringify({ video_ids, assignee_membership_id }) });
}
export function getAssignmentStats(projectId: number | string): Promise<AssignmentStats> {
  // 响应同时包含 unassigned（全部未分配）与 claimable（未分配草稿）。
  return apiFetch<AssignmentStats>(`/projects/${projectId}/assignment-stats`);
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
  options: { onProgress?: (p: UploadProgress) => void; signal?: AbortSignal; assigneeMembershipId?: number | null }
): Promise<Video> {
  return uploadFile<Video>(`/projects/${projectId}/videos/upload`, file, {
    field: "file",
    filename: file.name,
    onProgress: options.onProgress,
    signal: options.signal,
    fields: options.assigneeMembershipId != null ? { assignee_membership_id: String(options.assigneeMembershipId) } : undefined,
  });
}

// ---------- YOLO 三文件导入 ----------
export function createImportBatch(projectId: number | string): Promise<VideoImportBatch> {
  return apiFetch<VideoImportBatch>(`/projects/${projectId}/video-import-batches`, { method: "POST" });
}

export function uploadBatchFile(
  projectId: number | string,
  batchId: number,
  role: ImportFileRole,
  file: File,
  options: { onProgress?: (p: UploadProgress) => void; signal?: AbortSignal } = {}
): Promise<VideoImportBatch> {
  return uploadFile<VideoImportBatch>(`/projects/${projectId}/video-import-batches/${batchId}/files/${role}`, file, {
    ...options, method: "PUT", field: "file", filename: file.name,
  });
}

export function completeImportBatch(projectId: number | string, batchId: number, assigneeMembershipId?: number | null): Promise<VideoImportBatch> {
  const qs = assigneeMembershipId != null ? `?assignee_membership_id=${assigneeMembershipId}` : "";
  return apiFetch<VideoImportBatch>(`/projects/${projectId}/video-import-batches/${batchId}/complete${qs}`, { method: "POST" });
}

export function getImportBatch(projectId: number | string, batchId: number): Promise<VideoImportBatch> {
  return apiFetch<VideoImportBatch>(`/projects/${projectId}/video-import-batches/${batchId}`);
}

export function replaceDetectionImport(
  projectId: number | string, videoId: number | string, tracks: File, metadata: File, confirm = false
): Promise<Record<string, unknown>> {
  const form = new FormData();
  form.append("tracks_file", tracks, tracks.name);
  form.append("metadata_file", metadata, metadata.name);
  return apiRaw(`/projects/${projectId}/videos/${videoId}/detection-imports?confirm=${confirm}`, { method: "POST", body: form })
    .then(async (res) => {
      if (!res.ok) throw new ApiError(res.status, (await res.json().catch(() => ({})) as { detail?: string }).detail ?? "替换检测数据失败");
      return res.json() as Promise<Record<string, unknown>>;
    });
}

export function getCurrentDetectionImport(projectId: number | string, videoId: number | string): Promise<DetectionImport> {
  return apiFetch<DetectionImport>(`/projects/${projectId}/videos/${videoId}/detection-imports/current`);
}

export function getDetections(projectId: number | string, videoId: number | string, startFrame: number, endFrame: number): Promise<DetectionsResponse> {
  return apiFetch<DetectionsResponse>(`/projects/${projectId}/videos/${videoId}/detections?start_frame=${startFrame}&end_frame=${endFrame}`);
}

export function getCorrectedTracks(projectId: number | string, videoId: number | string, params: CorrectedTracksParams = {}): Promise<CorrectedTracksResponse> {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => { if (value != null && value !== "") qs.set(key, String(value)); });
  return apiFetch<CorrectedTracksResponse>(`/projects/${projectId}/videos/${videoId}/corrected-tracks${qs.size ? `?${qs}` : ""}`);
}

export function checkIdentityEdit(projectId: number | string, videoId: number | string, input: IdentityEditCheckRequest): Promise<IdentityEditCheckResponse> {
  return apiFetch<IdentityEditCheckResponse>(`/projects/${projectId}/videos/${videoId}/identity-edits/check`, { method: "POST", body: JSON.stringify(input) });
}

export function commitIdentityEdit(projectId: number | string, videoId: number | string, input: IdentityEditCommitRequest): Promise<IdentityEditResult> {
  return apiFetch<IdentityEditResult>(`/projects/${projectId}/videos/${videoId}/identity-edits`, { method: "POST", body: JSON.stringify(input) });
}

export function revertIdentityEdit(projectId: number | string, videoId: number | string, editId: number, base: { base_identity_revision: number; base_detection_import_revision: number }): Promise<IdentityEditResult> {
  return apiFetch<IdentityEditResult>(`/projects/${projectId}/videos/${videoId}/identity-edits/${editId}/revert`, { method: "POST", body: JSON.stringify(base) });
}

export function getIdentityEditHistory(projectId: number | string, videoId: number | string, limit = 1): Promise<IdentityEditRecord[]> {
  return apiFetch<IdentityEditRecord[]>(`/projects/${projectId}/videos/${videoId}/identity-edits/history?limit=${limit}`);
}

export function createSuppression(projectId: number | string, videoId: number | string, input: SuppressionCreateRequest): Promise<SuppressionResult> {
  return apiFetch<SuppressionResult>(`/projects/${projectId}/videos/${videoId}/detection-suppressions`, { method: "POST", body: JSON.stringify(input) });
}

export function listDetectionSuppressions(projectId: number | string, videoId: number | string): Promise<DetectionSuppression[]> {
  return apiFetch<DetectionSuppression[]>(`/projects/${projectId}/videos/${videoId}/detection-suppressions`);
}

export function revertSuppression(projectId: number | string, videoId: number | string, suppressionId: number, base: { base_identity_revision: number; base_detection_import_revision: number }): Promise<SuppressionResult> {
  return apiFetch<SuppressionResult>(`/projects/${projectId}/videos/${videoId}/detection-suppressions/${suppressionId}/revert`, { method: "POST", body: JSON.stringify(base) });
}

export function getCorrectedTracksExport(projectId: number | string, videoId: number | string): Promise<CorrectedTracksExport> {
  return apiFetch<CorrectedTracksExport>(`/projects/${projectId}/videos/${videoId}/detections/export`);
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

// ---------- 片段库（批次 5） ----------

/**
 * 片段列表（分页 + 类别/视频筛选 + 关键词搜索）：
 * GET /api/projects/:pid/clips?category_id=&video_id=&search=&page=&page_size= -> ClipListResponse。
 * 仅发送已声明的查询参数，过滤参数与分页全部类型化（ClipListParams）。
 * 注：片段库只含审核通过（review_status=approved）的标注；search 由服务端匹配类别名/视频文件名。
 */
export function listClips(projectId: number | string, params: ClipListParams): Promise<ClipListResponse> {
  const qs = new URLSearchParams();
  if (params.category_id != null) qs.set("category_id", String(params.category_id));
  if (params.video_id != null) qs.set("video_id", String(params.video_id));
  if (params.search && params.search.trim().length > 0) qs.set("search", params.search.trim());
  qs.set("page", String(params.page));
  qs.set("page_size", String(params.page_size));
  return apiFetch<ClipListResponse>(`/projects/${projectId}/clips?${qs.toString()}`);
}

/** 片段类别计数：GET /api/projects/:pid/clips/categories -> ClipCategoryCount[]。 */
export function listClipCategories(projectId: number | string): Promise<ClipCategoryCount[]> {
  return apiFetch<ClipCategoryCount[]>(`/projects/${projectId}/clips/categories`);
}

/**
 * 片段缩略图（批次 5）：thumbnail_path 为 thumbnails_dir 内相对文件名，
 * 经 /thumbnails/{name} 以 Bearer 拉取 blob 并生成 object URL（普通 <img> 无法携带
 * 认证请求头，与视频流同理）。缩略图路由尚不存在或拉取失败时返回 null，
 * 由调用方回退到 SVG 占位图——绝不用损坏的图片打断界面。
 */
export async function fetchClipThumbnailUrl(thumbnailPath: string): Promise<string | null> {
  try {
    const res = await apiRaw(`/thumbnails/${encodeURIComponent(thumbnailPath)}`);
    if (res.status === 401) {
      handleUnauthorized();
      return null;
    }
    if (!res.ok) return null;
    const blob = await res.blob();
    if (blob.size === 0) return null;
    return URL.createObjectURL(blob);
  } catch {
    return null;
  }
}

export type { User };

// ---------- 导出（批次 6） ----------

/**
 * 发起导出任务：POST /api/projects/:pid/export -> Job。
 * body 传 ExportRequestInput（category_ids 为空 / 缺省 = 导出全部类别）。
 * 409 表示上一个导出仍在进行中，由调用方提示「上一个导出仍在进行中」。
 */
export function createExport(
  projectId: number | string,
  input: ExportRequestInput
): Promise<Job> {
  return apiFetch<Job>(`/projects/${projectId}/export`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/**
 * 导出状态汇总：GET /api/projects/:pid/export/status -> ExportStatus。
 * 含最近任务（latest_job）与可导出 / 就绪 / 缺失计数，导出中按任务状态轮询本接口。
 */
export function getExportStatus(projectId: number | string): Promise<ExportStatus> {
  return apiFetch<ExportStatus>(`/projects/${projectId}/export/status`);
}

/** 从 Content-Disposition 解析文件名（优先 RFC 5987 filename*，其次 filename=）。 */
function parseContentDisposition(value: string | null): string | null {
  if (!value) return null;
  const star = /filename\*=UTF-8''([^;]+)/i.exec(value);
  if (star) {
    try {
      return decodeURIComponent(star[1]);
    } catch {
      // 解码失败回退到普通 filename
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(value);
  return plain ? plain[1] : null;
}

/**
 * 导出包下载：GET /api/projects/:pid/export/download -> blob ZIP。
 * ZIP 需要 Bearer 认证，与视频流同理用带 token 的请求拉取 blob 并生成 object URL；
 * 文件名以后端 Content-Disposition 为准，缺失时回退到默认名（供下载提示）。
 */
export async function fetchExportDownload(
  projectId: number | string
): Promise<{ blob: Blob; filename: string }> {
  const res = await apiRaw(`/projects/${projectId}/export/download`);
  if (res.status === 401) {
    // 与 apiFetch 一致：清除登录态并广播登出，避免 blob 请求 401 后界面停留
    handleUnauthorized();
    throw new ApiError(401, "登录已过期，请重新登录");
  }
  if (!res.ok) {
    let detail = `导出包下载失败（HTTP ${res.status}）`;
    try {
      const data: unknown = await res.json();
      const d = data && typeof data === "object" ? (data as { detail?: unknown }).detail : null;
      if (typeof d === "string" && d.length > 0) detail = d;
    } catch {
      // 响应体不是 JSON，保留默认错误信息
    }
    throw new ApiError(res.status, detail);
  }
  const blob = await res.blob();
  if (blob.size === 0) {
    throw new ApiError(404, "导出包文件为空或已过期，请重新发起导出");
  }
  const filename =
    parseContentDisposition(res.headers.get("Content-Disposition")) ??
    `project-${projectId}-export.zip`;
  return { blob, filename };
}
