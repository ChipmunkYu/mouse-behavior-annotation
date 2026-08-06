/**
 * 演示模式 API 实现：函数签名与 src/api/index.ts 完全一致，
 * 并额外提供片段库 / 导出 / 项目管理等「后端尚未接入」的演示接口。
 * 所有变更写入 src/demo/store.ts（内存 + localStorage），不请求网络。
 */

import { ApiError } from "../api/client";
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
} from "../api/types";
import { DEFAULT_FPS } from "../api/types";
import { timeToFrame as ttf } from "../utils/format";
import type {
  Clip,
  ExportPreview,
  ExportScope,
  ExportTask,
  MemberRole,
  ProjectMember,
  StorageOverview,
  TaskAssignment,
} from "./types";
import {
  createDemoExportTask,
  demoSetCategoryActive,
  demoStorageOverview,
  deriveClips,
  getState,
  resetDemoData,
  saveState,
} from "./store";

// ---------- 认证 ----------
export function login(username: string, password: string): Promise<LoginResponse> {
  const s = getState();
  const user = s.users.find((u) => u.username === username);
  if (!user || password !== "demo123") {
    return Promise.reject(new ApiError(401, "用户名或密码错误，请重试"));
  }
  return Promise.resolve({
    access_token: "demo-token",
    token_type: "bearer",
    user: { ...user },
  });
}

// ---------- 项目 ----------
export function listProjects(): Promise<Project[]> {
  return Promise.resolve(getState().projects.map((p) => ({ ...p })));
}

export function createProject(input: ProjectCreateInput): Promise<Project> {
  const s = getState();
  const id = s.nextIds.project++;
  const project: Project = {
    id,
    name: input.name,
    description: input.description ?? null,
    status: "active",
    created_at: new Date().toISOString(),
    role: "owner",
  };
  s.projects.push(project);
  // 新建项目同样初始化 12 类行为类别（与真实后端行为一致）
  const groupNames: [string, string[]][] = [
    ["个体行为", ["奔跑", "行走", "静止"]],
    ["社交行为", ["一起", "接近", "追逐", "回避", "攻击行为", "鼻头接触", "鼻尾接触"]],
    ["群体行为", ["扎堆行为", "孤立行为"]],
  ];
  const baseColors = [
    "#E6194B", "#3CB44B", "#FFE119", "#4363D8", "#F58231", "#911EB4",
    "#46F0F0", "#F032E6", "#BCF60C", "#FABEBE", "#008080", "#E6BEFF",
  ];
  let idx = 0;
  for (const [group, names] of groupNames) {
    for (const name of names) {
      const catId = s.categories.length + 1 + idx;
      s.categories.push({
        id: catId,
        project_id: id,
        name,
        group,
        color: baseColors[idx % baseColors.length],
        sort_order: idx,
        is_active: true,
      });
      idx += 1;
    }
  }
  saveState();
  return Promise.resolve(project);
}

// ---------- 类别 ----------
export function listCategories(projectId: number | string): Promise<Category[]> {
  const pid = Number(projectId);
  return Promise.resolve(getState().categories.filter((c) => c.project_id === pid).map((c) => ({ ...c })));
}

// ---------- 视频 ----------
export function listVideos(projectId: number | string): Promise<Video[]> {
  const pid = Number(projectId);
  return Promise.resolve(getState().videos.filter((v) => v.project_id === pid).map((v) => ({ ...v })));
}

function nextVideoId(s: ReturnType<typeof getState>): number {
  return s.videos.reduce((m, v) => Math.max(m, v.id), 0) + 1;
}

export function createVideo(projectId: number | string, input: VideoCreateInput): Promise<Video> {
  const s = getState();
  const pid = Number(projectId);
  const id = nextVideoId(s);
  const video: Video = {
    id,
    project_id: pid,
    filename: input.filename,
    duration: input.duration ?? null,
    fps: input.fps ?? null,
    width: input.width ?? null,
    height: input.height ?? null,
    storage_path: input.storage_path ?? null,
    status: input.status ?? "metadata",
    workflow_status: "draft",
    annotation_revision: 1,
    submitted_at: null,
    approved_at: null,
    approved_by: null,
    created_at: new Date().toISOString(),
  };
  s.videos.push(video);
  saveState();
  return Promise.resolve({ ...video });
}

export function uploadVideo(
  projectId: number | string,
  file: File,
  options: { onProgress?: (p: { loaded: number; total: number; percent: number }) => void; signal?: AbortSignal }
): Promise<Video> {
  const pid = Number(projectId);
  return new Promise<Video>((resolve, reject) => {
    const total = file.size > 0 ? file.size : 1;
    let loaded = 0;
    const onAbort = () => {
      clearInterval(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    if (options.signal) {
      if (options.signal.aborted) {
        reject(new DOMException("Aborted", "AbortError"));
        return;
      }
      options.signal.addEventListener("abort", onAbort, { once: true });
    }
    const timer = setInterval(() => {
      loaded += Math.max(1, Math.round(total * 0.09));
      if (loaded >= total) {
        clearInterval(timer);
        options.signal?.removeEventListener("abort", onAbort);
        const s = getState();
        const id = nextVideoId(s);
        const video: Video = {
          id,
          project_id: pid,
          filename: file.name,
          duration: 180 + (id % 90),
          fps: DEFAULT_FPS,
          width: 1920,
          height: 1080,
          storage_path: `data/videos/${file.name}`,
          status: "ready",
          workflow_status: "draft",
          annotation_revision: 1,
          submitted_at: null,
          approved_at: null,
          approved_by: null,
          created_at: new Date().toISOString(),
        };
        s.videos.push(video);
        saveState();
        resolve({ ...video });
      } else {
        options.onProgress?.({
          loaded,
          total,
          percent: Math.min(99, Math.round((loaded / total) * 100)),
        });
      }
    }, 160);
  });
}

export function fetchVideoStreamUrl(): Promise<string> {
  return Promise.reject(
    new ApiError(404, "演示模式不提供真实视频流，使用内置顶视小鼠演示画面")
  );
}

// ---------- 标注 ----------
export function listAnnotations(_projectId: number | string, videoId: number | string): Promise<Annotation[]> {
  const vid = Number(videoId);
  return Promise.resolve(getState().annotations.filter((a) => a.video_id === vid).map((a) => ({ ...a })));
}

/** 修改标注会令非 draft 视频退回草稿（与真实后端工作流一致）。 */
function downgradeVideoIfLocked(s: ReturnType<typeof getState>, vid: number): void {
  const v = s.videos.find((item) => item.id === vid);
  if (v && v.workflow_status !== "draft") {
    v.workflow_status = "draft";
    v.submitted_at = null;
    v.approved_at = null;
    v.approved_by = null;
  }
}

export function createAnnotation(
  _projectId: number | string,
  videoId: number | string,
  input: AnnotationCreateInput
): Promise<Annotation> {
  const s = getState();
  const vid = Number(videoId);
  const cat = s.categories.find((c) => c.id === input.category_id);
  const self = s.users.find((u) => u.id === 1);
  const fps = s.videos.find((v) => v.id === vid)?.fps ?? DEFAULT_FPS;
  const ann: Annotation = {
    id: s.nextIds.annotation++,
    video_id: vid,
    annotator_id: 1,
    category_id: input.category_id,
    start_time: input.start_time,
    end_time: input.end_time,
    start_frame: input.start_frame ?? ttf(input.start_time, fps),
    end_frame: input.end_frame ?? ttf(input.end_time, fps),
    confidence: input.confidence ?? "certain",
    review_status: input.review_status ?? "pending",
    crop_region: input.crop_region ?? null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    annotator: self?.username ?? null,
    category_name: cat?.name ?? null,
  };
  s.annotations.push(ann);
  downgradeVideoIfLocked(s, vid);
  saveState();
  return Promise.resolve({ ...ann });
}

export function updateAnnotation(
  _projectId: number | string,
  videoId: number | string,
  annotationId: number,
  patch: AnnotationPatchInput
): Promise<Annotation> {
  const s = getState();
  const vid = Number(videoId);
  const ann = s.annotations.find((a) => a.id === annotationId && a.video_id === vid);
  if (!ann) return Promise.reject(new ApiError(404, "标注不存在或已删除"));
  const cat = patch.category_id != null ? s.categories.find((c) => c.id === patch.category_id) : undefined;
  Object.assign(ann, patch, {
    category_name: cat?.name ?? ann.category_name,
    updated_at: new Date().toISOString(),
  });
  downgradeVideoIfLocked(s, vid);
  saveState();
  return Promise.resolve({ ...ann });
}

export function deleteAnnotation(_projectId: number | string, videoId: number | string, annotationId: number): Promise<void> {
  const s = getState();
  const vid = Number(videoId);
  const idx = s.annotations.findIndex((a) => a.id === annotationId && a.video_id === vid);
  if (idx >= 0) {
    s.annotations.splice(idx, 1);
    downgradeVideoIfLocked(s, vid);
    saveState();
  }
  return Promise.resolve();
}

export function exportAnnotations(_projectId: number | string, videoId: number | string): Promise<ExportEvent[]> {
  const vid = Number(videoId);
  const events: ExportEvent[] = getState()
    .annotations.filter((a) => a.video_id === vid)
    .map((a) => ({
      video_id: String(a.video_id),
      start_time: a.start_time,
      end_time: a.end_time,
      start_frame: a.start_frame,
      end_frame: a.end_frame,
      behavior: a.category_name,
      crop_region: a.crop_region,
      confidence: a.confidence,
      annotator: a.annotator,
      reviewer: null,
      review_status: a.review_status,
    }));
  return Promise.resolve(events);
}

// ---------- 审核工作流 ----------
export function submitVideoForReview(_projectId: number | string, videoId: number | string): Promise<Video> {
  const s = getState();
  const vid = Number(videoId);
  const v = s.videos.find((item) => item.id === vid);
  if (!v) return Promise.reject(new ApiError(404, "视频不存在"));
  const hasAnn = s.annotations.some((a) => a.video_id === vid);
  if (!hasAnn) return Promise.reject(new ApiError(400, "至少需要一条标注才能提交审核"));
  v.workflow_status = "submitted";
  v.submitted_at = new Date().toISOString();
  v.annotation_revision = (v.annotation_revision ?? 1) + 1;
  saveState();
  return Promise.resolve({ ...v });
}

export function listReviewQueue(projectId: number | string): Promise<Video[]> {
  const pid = Number(projectId);
  const queued = getState()
    .videos.filter((v) => v.project_id === pid && v.workflow_status === "submitted")
    .map((v) => ({ ...v }))
    .sort((a, b) => {
      const ta = a.submitted_at ? new Date(a.submitted_at).getTime() : 0;
      const tb = b.submitted_at ? new Date(b.submitted_at).getTime() : 0;
      return ta - tb;
    });
  return Promise.resolve(queued);
}

export function listVideoReviews(_projectId: number | string, videoId: number | string): Promise<Review[]> {
  const vid = Number(videoId);
  return Promise.resolve(getState().reviews.filter((r) => r.video_id === vid).map((r) => ({ ...r })));
}

export function createVideoReview(
  projectId: number | string,
  videoId: number | string,
  input: ReviewCreateInput
): Promise<Review> {
  const s = getState();
  const vid = Number(videoId);
  const v = s.videos.find((item) => item.id === vid);
  if (!v) return Promise.reject(new ApiError(404, "视频不存在"));
  const review: Review = {
    id: s.nextIds.review++,
    project_id: Number(projectId),
    video_id: vid,
    reviewer_id: 1,
    result: input.result,
    comment: input.comment ?? null,
    annotation_revision: v.annotation_revision ?? 1,
    created_at: new Date().toISOString(),
    reviewer: "demo",
  };
  s.reviews.push(review);
  if (input.result === "approved") {
    v.workflow_status = "approved";
    v.approved_at = new Date().toISOString();
    v.approved_by = 1;
    for (const a of s.annotations) {
      if (a.video_id === vid) a.review_status = "approved";
    }
  } else {
    v.workflow_status = "rejected";
  }
  saveState();
  return Promise.resolve(review);
}

// ---------- 片段库（后端尚未接入，演示实现） ----------
export function listClips(projectId: number | string): Promise<Clip[]> {
  return Promise.resolve(deriveClips(Number(projectId)));
}

// ---------- 导出中心（后端尚未接入，演示实现） ----------
function buildExportTree(clips: Clip[], taskId: number): string {
  const lines: string[] = [];
  lines.push(`exports/`);
  lines.push(`└── task_${taskId}/                # 本次导出目录`);
  lines.push(`    ├── annotations.json         # ${clips.length} 条事件（JSON 摘要）`);
  lines.push(`    └── clips/`);
  clips.forEach((c, i) => {
    const last = i === clips.length - 1;
    const prefix = last ? "        └── " : "        ├── ";
    lines.push(`${prefix}clip_${String(i + 1).padStart(4, "0")}.mp4   # ${c.video_filename} ${fmtRange(c.start_time, c.end_time)} ${c.category_name}`);
  });
  if (clips.length === 0) lines.push("        └── (无符合范围的片段)");
  return lines.join("\n");
}

function fmtRange(s: number, e: number): string {
  const f = (t: number) => {
    const m = Math.floor(t / 60);
    const sec = Math.floor(t % 60);
    return `${m}:${String(sec).padStart(2, "0")}`;
  };
  return `${f(s)}–${f(e)}`;
}

export function getExportPreview(projectId: number | string, scope: ExportScope): Promise<ExportPreview> {
  const clips = deriveClips(Number(projectId), scope);
  const byCategory = new Map<number, { category_id: number; name: string; color: string | null; count: number }>();
  for (const c of clips) {
    const cur = byCategory.get(c.category_id) ?? {
      category_id: c.category_id,
      name: c.category_name,
      color: c.category_color,
      count: 0,
    };
    cur.count += 1;
    byCategory.set(c.category_id, cur);
  }
  const preview: ExportPreview = {
    clip_count: clips.length,
    video_count: new Set(clips.map((c) => c.video_id)).size,
    total_seconds: clips.reduce((sum, c) => sum + c.duration, 0),
    by_category: [...byCategory.values()],
    tree: buildExportTree(clips, 0),
    summary: {
      total_events: clips.length,
      sample: clips.slice(0, 3).map((c) => ({
        video_id: String(c.video_id),
        start_time: c.start_time,
        end_time: c.end_time,
        start_frame: c.start_frame,
        end_frame: c.end_frame,
        behavior: c.category_name,
        confidence: "certain",
        review_status: c.review_status,
        annotator: c.annotator,
      })),
    },
  };
  return Promise.resolve(preview);
}

export function createExportTask(projectId: number | string, scope: ExportScope): Promise<ExportTask> {
  const task = createDemoExportTask(Number(projectId), scope);
  return Promise.resolve({ ...task });
}

export function listExportTasks(): Promise<ExportTask[]> {
  return Promise.resolve(getState().exports.map((t) => ({ ...t })));
}

// ---------- 项目管理（后端尚未接入，演示实现） ----------
export function listProjectMembers(): Promise<ProjectMember[]> {
  return Promise.resolve(getState().members.map((m) => ({ ...m })));
}

export function setMemberRole(_projectId: number | string, memberId: number, role: MemberRole): Promise<ProjectMember[]> {
  const s = getState();
  const member = s.members.find((m) => m.id === memberId);
  if (member && member.is_self) {
    return Promise.reject(new ApiError(400, "不能修改自己的项目角色（演示限制）"));
  }
  if (member) {
    member.role = role;
    saveState();
  }
  return Promise.resolve(s.members.map((m) => ({ ...m })));
}

export function addMember(_projectId: number | string, username: string, role: MemberRole): Promise<ProjectMember[]> {
  const s = getState();
  const name = username.trim();
  if (!name) return Promise.reject(new ApiError(422, "用户名不能为空"));
  if (s.members.some((m) => m.username === name)) {
    return Promise.reject(new ApiError(409, "该用户已是项目成员"));
  }
  s.members.push({
    id: s.nextIds.member++,
    username: name,
    role,
    is_self: false,
    joined_at: new Date().toISOString(),
  });
  saveState();
  return Promise.resolve(s.members.map((m) => ({ ...m })));
}

export function removeMember(_projectId: number | string, memberId: number): Promise<ProjectMember[]> {
  const s = getState();
  const member = s.members.find((m) => m.id === memberId);
  if (member?.is_self) return Promise.reject(new ApiError(400, "不能移除自己（演示限制）"));
  const idx = s.members.findIndex((m) => m.id === memberId);
  if (idx >= 0) s.members.splice(idx, 1);
  saveState();
  return Promise.resolve(s.members.map((m) => ({ ...m })));
}

export function listAssignments(projectId: number | string): Promise<TaskAssignment[]> {
  const pid = Number(projectId);
  return Promise.resolve(
    getState()
      .assignments.filter((a) => a.video_id !== undefined)
      .filter((a) => getState().videos.some((v) => v.id === a.video_id && v.project_id === pid))
      .map((a) => ({ ...a }))
  );
}

export function setAssignment(_projectId: number | string, videoId: number, annotatorId: number | null): Promise<TaskAssignment[]> {
  const s = getState();
  let row = s.assignments.find((a) => a.video_id === videoId);
  if (!row) {
    const v = s.videos.find((item) => item.id === videoId);
    if (!v) return Promise.reject(new ApiError(404, "视频不存在"));
    row = {
      video_id: videoId,
      video_filename: v.filename,
      video_workflow: v.workflow_status,
      annotator_id: null,
      annotator_name: null,
      status: "assigned",
    };
    s.assignments.push(row);
  }
  row.annotator_id = annotatorId;
  row.annotator_name = annotatorId ? s.users.find((u) => u.id === annotatorId)?.username ?? null : null;
  row.status = annotatorId ? "assigned" : "assigned";
  saveState();
  return Promise.resolve(s.assignments.map((a) => ({ ...a })));
}

export function setCategoryActive(projectId: number | string, categoryId: number, isActive: boolean): Promise<Category[]> {
  return Promise.resolve(demoSetCategoryActive(Number(projectId), categoryId, isActive).map((c) => ({ ...c })));
}

export function getStorageOverview(projectId: number | string): Promise<StorageOverview> {
  return Promise.resolve(demoStorageOverview(Number(projectId)));
}

// ---------- 重置 ----------
export function resetDemo(): void {
  resetDemoData();
}

export type { User };
