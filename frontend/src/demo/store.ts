/**
 * 演示模式存储层：localStorage 持久化 + 内存状态 + 导出任务进度模拟。
 * - 页面 / API 层不直接触碰此文件，统一经由 src/demo/api.ts 与 src/api/index.ts。
 * - 「重置演示数据」= 清除本地存储并重新种子。
 */

import { DEMO_STATE_VERSION, buildSeed, type DemoState } from "./fixtures";
import type { ExportScope, ExportTask, Clip, StorageOverview } from "./types";
import type { Category } from "../api/types";

const STORAGE_KEY = "mba_demo_state_v1";

let state: DemoState | null = null;
/** 导出任务进度定时器（模块级：跨页面导航持续模拟后台任务）。 */
const exportTimers = new Map<number, ReturnType<typeof setInterval>>();
/** 导出任务的预置结果（创建时随机决定，避免渲染过程反复随机）。 */
const exportOutcomes = new Map<number, "completed" | "failed">();
/** 派生片段缓存（数据变更后由 saveState 清除）。 */
let clipsCache: { key: string; clips: Clip[] } | null = null;

function seed(): DemoState {
  return buildSeed();
}

/** 为所有「进行中」的导出任务补启动度模拟（种子任务 / 刷新页面后恢复）。 */
function ensureRunningWorkers(): void {
  if (!state) return;
  for (const t of state.exports) {
    if (t.status === "running") startExportWorker(t.id);
  }
}

export function getState(): DemoState {
  if (state) return state;
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as DemoState;
      if (parsed && parsed.version === DEMO_STATE_VERSION) {
        state = parsed;
        ensureRunningWorkers();
        return state;
      }
    }
  } catch {
    // 本地数据损坏时重新种子
  }
  state = seed();
  saveState();
  ensureRunningWorkers();
  return state;
}

export function saveState(): void {
  if (!state) return;
  clipsCache = null;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // 存储满等极端情况忽略，内存态仍可用
  }
}

export function resetDemoData(): void {
  for (const t of exportTimers.values()) clearInterval(t);
  exportTimers.clear();
  exportOutcomes.clear();
  clipsCache = null;
  state = null;
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    // 忽略
  }
  state = seed();
  saveState();
  ensureRunningWorkers();
}

/** 由审核通过的标注派生行为片段（片段库数据源）。 */
export function deriveClips(projectId: number, scope?: ExportScope): Clip[] {
  const s = getState();
  const key = `${projectId}|${s.videos.length}|${s.annotations.length}|${scope?.category_id ?? "all"}|${scope?.approved_only ?? true}`;
  if (clipsCache && clipsCache.key === key) return clipsCache.clips;

  const videoById = new Map(s.videos.filter((v) => v.project_id === projectId).map((v) => [v.id, v]));
  const categoryById = new Map(s.categories.map((c) => [c.id, c]));
  const userById = new Map(s.users.map((u) => [u.id, u.username]));

  const clips: Clip[] = s.annotations
    .filter(
      (a) =>
        a.review_status === "approved" &&
        videoById.has(a.video_id) &&
        (!scope?.category_id || a.category_id === scope.category_id)
    )
    .map((a) => {
      const v = videoById.get(a.video_id)!;
      const cat = categoryById.get(a.category_id);
      return {
        id: a.id,
        project_id: projectId,
        video_id: v.id,
        video_filename: v.filename,
        category_id: a.category_id,
        category_name: cat?.name ?? a.category_name ?? `类别 #${a.category_id}`,
        category_color: cat?.color ?? null,
        start_time: a.start_time,
        end_time: a.end_time,
        start_frame: a.start_frame,
        end_frame: a.end_frame,
        duration: Math.max(0.1, a.end_time - a.start_time),
        review_status: a.review_status,
        annotator: a.annotator ?? userById.get(a.annotator_id) ?? null,
        approved_at: v.approved_at,
        thumb: makeClipThumb({
          color: cat?.color ?? "#8b929b",
          name: cat?.name ?? "",
          start: a.start_time,
          end: a.end_time,
        }),
      };
    })
    .sort((x, y) => x.video_id - y.video_id || x.start_time - y.start_time);

  clipsCache = { key, clips };
  return clips;
}

/** 片段缩略图占位：内联 SVG data URI（不引入任何二进制资源）。 */
export function makeClipThumb(p: {
  color: string;
  name: string;
  start: number;
  end: number;
}): string {
  const fmt = (sec: number): string => {
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    return `${m}:${String(s).padStart(2, "0")}`;
  };
  const label = `${p.name ?? ""} · ${fmt(p.start)}–${fmt(p.end)}`;
  const svg =
    `<svg xmlns='http://www.w3.org/2000/svg' width='280' height='158'>` +
    `<rect width='280' height='158' fill='#14161a'/>` +
    `<rect width='280' height='4' fill='${p.color}'/>` +
    // 顶视鼠笼示意：地面格线 + 笼壁
    `<g stroke='#2a2e34' stroke-width='1'>` +
    `<path d='M40 0V158M80 0V158M120 0V158M160 0V158M200 0V158M240 0V158M0 40H280M0 80H280M0 120H280'/>` +
    `<rect x='1' y='1' width='278' height='156' fill='none' stroke='#3b4046' stroke-width='2'/>` +
    `</g>` +
    // 三只示意小鼠（椭圆身体 + 头部 + 耳）
    `<g fill='#cfd3d8' opacity='0.9'>` +
    `<ellipse cx='90' cy='70' rx='16' ry='9' transform='rotate(-18 90 70)'/>` +
    `<circle cx='104' cy='63' r='5'/>` +
    `<circle cx='102' cy='56' r='3' fill='#aeb3ba'/><circle cx='109' cy='58' r='3' fill='#aeb3ba'/>` +
    `<ellipse cx='160' cy='100' rx='16' ry='9' transform='rotate(14 160 100)'/>` +
    `<circle cx='147' cy='105' r='5'/>` +
    `<circle cx='149' cy='112' r='3' fill='#aeb3ba'/><circle cx='142' cy='110' r='3' fill='#aeb3ba'/>` +
    `<ellipse cx='200' cy='52' rx='14' ry='8' transform='rotate(-30 200 52)'/>` +
    `<circle cx='212' cy='46' r='4.5'/>` +
    `<circle cx='210' cy='40' r='2.6' fill='#aeb3ba'/><circle cx='216' cy='42' r='2.6' fill='#aeb3ba'/>` +
    `</g>` +
    `<text x='10' y='148' font-family='monospace' font-size='12' fill='#8b929b'>${label}</text>` +
    `<text x='270' y='18' text-anchor='end' font-family='monospace' font-size='10' fill='#5b6169'>片段示意</text>` +
    `</svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

// ---------- 导出任务模拟 ----------

function isoLater(days: number): string {
  return new Date(Date.now() + days * 86400000).toISOString();
}

function startExportWorker(taskId: number): void {
  if (exportTimers.has(taskId)) return;
  const timer = setInterval(() => {
    const s = getState();
    const task = s.exports.find((t) => t.id === taskId);
    if (!task || task.status !== "running") {
      clearInterval(timer);
      exportTimers.delete(taskId);
      return;
    }
    const inc = 4 + Math.floor(Math.random() * 7);
    task.progress = Math.min(100, task.progress + inc);
    if (task.progress >= 100) {
      const outcome = exportOutcomes.get(taskId) ?? "completed";
      task.status = outcome;
      task.completed_at = new Date().toISOString();
      if (outcome === "failed") {
        task.error = "模拟失败：ffmpeg 转码进程异常退出（exit 1）。演示模式不会真正执行 ffmpeg。";
      }
      clearInterval(timer);
      exportTimers.delete(taskId);
    }
    saveState();
  }, 420);
  exportTimers.set(taskId, timer);
}

/** 创建演示导出任务并启动后台进度模拟（成功概率约 7 成，失败用于演示错误路径）。 */
export function createDemoExportTask(projectId: number, scope: ExportScope): ExportTask {
  const s = getState();
  const clips = deriveClips(projectId, scope);
  const id = s.nextIds.exportTask++;
  const now = new Date().toISOString();
  const task: ExportTask = {
    id,
    name: scope.category_id
      ? `已通过片段 · 按类别导出`
      : `全部已通过片段 · 完整导出`,
    scope,
    status: "running",
    progress: 0,
    clip_count: clips.length,
    created_at: now,
    expires_at: isoLater(7),
    started_at: now,
    completed_at: null,
    error: null,
  };
  s.exports.unshift(task);
  exportOutcomes.set(id, Math.random() < 0.72 ? "completed" : "failed");
  saveState();
  startExportWorker(id);
  return task;
}

/** 存储概览（尺寸为按元数据推算的演示值）。 */
export function demoStorageOverview(projectId: number): StorageOverview {
  const s = getState();
  const vids = s.videos.filter((v) => v.project_id === projectId);
  const bytes = vids.reduce((sum, v) => sum + Math.round((v.duration ?? 0) * 1.15e6 + v.id * 4.7e6), 0);
  const byWorkflow: Record<string, number> = {};
  const byStatus: Record<string, number> = {};
  for (const v of vids) {
    byWorkflow[v.workflow_status] = (byWorkflow[v.workflow_status] ?? 0) + 1;
    byStatus[v.status] = (byStatus[v.status] ?? 0) + 1;
  }
  return {
    total_videos: vids.length,
    total_bytes: bytes,
    by_workflow: byWorkflow,
    by_status: byStatus,
    disk_used_gb: Math.round((bytes / 1e9) * 10) / 10,
    disk_total_gb: 1024,
  };
}

/** 类别启用状态切换（项目管理）。 */
export function demoSetCategoryActive(projectId: number, categoryId: number, isActive: boolean): Category[] {
  const s = getState();
  const cat = s.categories.find((c) => c.project_id === projectId && c.id === categoryId);
  if (cat) {
    cat.is_active = isActive;
    saveState();
  }
  return s.categories.filter((c) => c.project_id === projectId);
}
