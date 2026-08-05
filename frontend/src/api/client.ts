/**
 * 统一 fetch 封装：
 * - base 默认 http://localhost:8000/api（可用 VITE_API_BASE 覆盖）
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

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
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
  return null;
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
    try {
      const data: unknown = await res.json();
      detail = extractDetail(data) ?? detail;
    } catch {
      // 响应体不是 JSON，保留默认错误信息
    }
    throw new ApiError(res.status, detail);
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
