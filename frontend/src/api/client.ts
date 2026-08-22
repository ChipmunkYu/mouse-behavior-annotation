/**
 * 统一 fetch 封装：
 * - 本地开发默认连接 http://localhost:8000/api；生产由 .env.production 注入同源 /api
 * - 自动附加 Bearer token
 * - 401 时清除登录态并广播 auth:unauthorized（由 AuthContext 处理退出）
 * - 统一抛出 ApiError，提取 FastAPI 的 {detail} 错误信息
 */
import { clearAuth, getToken } from "../auth/storage";
import { emitUnauthorized } from "../auth/events";

export const API_BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined) ?? "http://localhost:8000/api";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function extractDetail(data: unknown): string | null {
  if (!data || typeof data !== "object") return null;
  const detail = (data as { detail?: unknown }).detail;
  if (typeof detail === "string" && detail.length > 0) return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item === "object" && typeof (item as { msg?: unknown }).msg === "string") {
          return (item as { msg: string }).msg;
        }
        return String(item);
      })
      .join("；");
  }
  if (detail && typeof detail === "object") {
    const message = (detail as { message?: unknown }).message;
    const ids = (detail as { video_ids?: unknown }).video_ids;
    if (typeof message === "string") {
      const conflicts = (detail as { conflicts?: unknown }).conflicts;
      if (Array.isArray(conflicts) && conflicts.length) {
        const rows = conflicts.map((item) => {
          if (!item || typeof item !== "object") return "未知冲突";
          const c = item as Record<string, unknown>;
          return `标注 #${c.annotation_id ?? "?"} · 帧 ${c.start_frame ?? "?"}–${c.end_frame ?? "?"} / ${c.start_time ?? "?"}–${c.end_time ?? "?"} 秒 · ${c.role_name ?? c.role_key ?? "未知角色"} · track ID ${c.track_id ?? "?"}`;
        });
        return `${message}：${rows.join("；")}`;
      }
      const conflictFrames = (detail as { conflict_frames?: unknown }).conflict_frames;
      if (Array.isArray(conflictFrames) && conflictFrames.length) {
        return `${message}（冲突帧：${conflictFrames.join("、")}）`;
      }
      const invalidAnnotations = (detail as { invalid_annotations?: unknown }).invalid_annotations;
      if (Array.isArray(invalidAnnotations) && invalidAnnotations.length) {
        const rows = invalidAnnotations.map((item) => {
          if (!item || typeof item !== "object") return "未知标注";
          const row = item as Record<string, unknown>;
          return `标注 #${row.annotation_id ?? "?"}：${row.reason ?? "需要重新校验"}`;
        });
        return `${message}：${rows.join("；")}`;
      }
      return Array.isArray(ids) && ids.length ? `${message}（视频 ID：${ids.join("、")}）` : message;
    }
  }
  return null;
}

/** 常见状态码 → 补充说明，让 403/409/422 等错误更可读（详情优先，提示作补充）。 */
const STATUS_HINTS: Record<number, string> = {
  403: "你没有权限执行此操作，请确认当前项目角色是否允许",
  404: "目标资源不存在或已被删除",
  409: "当前状态不允许此操作（可能状态已变化），请刷新后重试",
  422: "提交的数据不合法，请检查输入后重试",
};

function friendlyDetail(status: number, detail: string): string {
  const hint = STATUS_HINTS[status];
  if (!hint) return detail;
  if (detail.startsWith("请求失败") || detail.startsWith("上传失败")) return hint;
  return `${detail}（${hint}）`;
}

/**
 * 统一处理 401：仅在已有会话时清除登录态并广播登出事件。
 * JSON 请求（handleResponse）与视频流等原始请求（apiRaw）共用，保证行为一致。
 */
export function handleUnauthorized(): void {
  if (getToken()) {
    clearAuth();
    emitUnauthorized();
  }
}

async function handleResponse<T>(res: Response): Promise<T> {
  if (res.status === 401) {
    handleUnauthorized();
    throw new ApiError(401, "登录已过期或凭据无效，请重新登录");
  }

  if (!res.ok) {
    let detail = `请求失败（HTTP ${res.status}）`;
    let rawDetail: unknown;
    try {
      rawDetail = await res.json();
      detail = extractDetail(rawDetail) ?? detail;
    } catch {
      // 响应体不是 JSON，保留默认错误信息
    }
    throw new ApiError(res.status, friendlyDetail(res.status, detail), rawDetail);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return fetch(`${API_BASE}${path}`, { ...init, headers }).then((res) => handleResponse<T>(res));
}

/** 返回原始 Response（用于视频流等二进制场景），仍自动携带 Bearer token。 */
export async function apiRaw(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return fetch(`${API_BASE}${path}`, { ...init, headers });
}

// ---------- 文件上传（multipart，支持真实进度与取消） ----------

export interface UploadProgress {
  /** 已上传字节数 */
  loaded: number;
  /** 总字节数（不可计算时为 0） */
  total: number;
  /** 0–100 整数百分比 */
  percent: number;
}

export interface UploadFileOptions {
  /** HTTP 方法；批次文件槽使用 PUT，其余上传默认 POST。 */
  method?: "POST" | "PUT";
  /** multipart 字段名，默认 "file" */
  field?: string;
  /** 服务端保存的文件名，默认取 File.name */
  filename?: string;
  /** 上传进度回调（仅当浏览器可计算总大小时触发） */
  onProgress?: (p: UploadProgress) => void;
  /** 传入 AbortSignal 可取消上传；取消时 Promise 以 AbortError 拒绝 */
  signal?: AbortSignal;
  /** 同一 multipart 请求中的额外文本字段。 */
  fields?: Record<string, string>;
}

/**
 * multipart 文件上传（XMLHttpRequest）：
 * - 自动附加 Bearer token，401 行为与 apiFetch 一致（清登录态并广播登出）
 * - 不设置 Content-Type，让浏览器自动生成 multipart boundary
 * - 通过 signal 取消：xhr.abort()，Promise 以 DOMException "AbortError" 拒绝
 * - 507 等错误仍提取后端 {detail}，无 detail 时给出友好兜底文案
 */
export function uploadFile<T>(path: string, file: Blob, options: UploadFileOptions = {}): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(options.method ?? "POST", `${API_BASE}${path}`);

    const token = getToken();
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);

    const onAbort = () => xhr.abort();
    if (options.signal) {
      if (options.signal.aborted) {
        reject(new DOMException("Aborted", "AbortError"));
        return;
      }
      options.signal.addEventListener("abort", onAbort, { once: true });
    }
    const cleanupSignal = () => options.signal?.removeEventListener("abort", onAbort);

    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable && e.total > 0) {
        options.onProgress?.({
          loaded: e.loaded,
          total: e.total,
          percent: Math.min(100, Math.round((e.loaded / e.total) * 100)),
        });
      }
    });

    xhr.addEventListener("load", () => {
      cleanupSignal();
      const status = xhr.status;

      if (status === 401) {
        handleUnauthorized();
        reject(new ApiError(401, "登录已过期或凭据无效，请重新登录"));
        return;
      }

      if (status >= 200 && status < 300) {
        if (status === 204 || xhr.responseText === "") {
          resolve(undefined as T);
        } else {
          try {
            resolve(JSON.parse(xhr.responseText) as T);
          } catch {
            reject(new ApiError(status, "上传响应解析失败"));
          }
        }
        return;
      }

      let msg = `上传失败（HTTP ${status}）`;
      try {
        const detail = extractDetail(JSON.parse(xhr.responseText) as unknown);
        if (detail) msg = detail;
      } catch {
        // 响应体不是 JSON，保留默认文案
      }
      msg = friendlyDetail(status, msg);
      if (status === 507) {
        msg = "服务器磁盘空间不足，无法保存该视频。请先清理服务器磁盘空间后重试。";
      }
      reject(new ApiError(status, msg));
    });

    xhr.addEventListener("error", () => {
      cleanupSignal();
      reject(new ApiError(0, "网络错误，上传中断，请检查网络后重试"));
    });

    xhr.addEventListener("abort", () => {
      cleanupSignal();
      reject(new DOMException("Aborted", "AbortError"));
    });

    const form = new FormData();
    form.append(options.field ?? "file", file, options.filename ?? (file as File).name ?? "upload");
    Object.entries(options.fields ?? {}).forEach(([key, value]) => form.append(key, value));
    xhr.send(form);
  });
}
