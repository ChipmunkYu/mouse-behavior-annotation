import type { StreamTicket } from "../api/types";

export type MediaReadyReason = "initial" | "retry-restored";
export type MediaHttpStatus = 401 | 403 | 404 | null;
export type MediaControllerState =
  | { status: "idle"; generation: number; readyReason: null }
  | { status: "loading"; generation: number; readyReason: null }
  | { status: "ready"; generation: number; readyReason: MediaReadyReason }
  | { status: "error"; generation: number; readyReason: null; message: string; canRetry: true; httpStatus: MediaHttpStatus };

export const MEDIA_LOAD_ERROR = "视频加载失败，请检查网络后重试。";

const MEDIA_HTTP_ERROR_MESSAGES: Record<Exclude<MediaHttpStatus, null>, string> = {
  401: "登录已过期，请重新登录后重试。",
  403: "你没有权限访问此视频，请联系项目管理员。",
  404: "视频不存在或已被删除，请返回视频列表。",
};

export interface MediaElementLike {
  src: string;
  currentTime: number;
  paused: boolean;
  pause(): void;
  play(): Promise<void>;
  load(): void;
  removeAttribute(name: string): void;
  addEventListener(type: "loadedmetadata" | "canplay" | "error", listener: () => void): void;
  removeEventListener(type: "loadedmetadata" | "canplay" | "error", listener: () => void): void;
}

export interface MediaControllerDependencies {
  element: MediaElementLike;
  native: boolean;
  fetchTicket: (videoId: number | string, signal: AbortSignal) => Promise<StreamTicket>;
  fetchLegacyUrl: (videoId: number | string, signal: AbortSignal) => Promise<string>;
  revokeObjectUrl: (url: string) => void;
  onState: (state: MediaControllerState) => void;
  onReady?: (reason: MediaReadyReason, element: MediaElementLike) => void;
}

export interface MediaController {
  load(videoId: number | string): number;
  dispose(): void;
  getState(): MediaControllerState;
}

interface ActiveGeneration {
  id: number;
  videoId: number | string;
  abort: AbortController;
  operation: number;
  retryUsed: boolean;
  objectUrl: string | null;
  removeListeners: (() => void) | null;
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function classifyHttpStatus(error: unknown): MediaHttpStatus {
  if (!error || typeof error !== "object") return null;
  try {
    if (!Object.prototype.hasOwnProperty.call(error, "status")) return null;
    const status = (error as { status?: unknown }).status;
    return status === 401 || status === 403 || status === 404 ? status : null;
  } catch {
    return null;
  }
}

function errorState(generation: number, httpStatus: MediaHttpStatus): MediaControllerState {
  return {
    status: "error",
    generation,
    readyReason: null,
    message: httpStatus === null ? MEDIA_LOAD_ERROR : MEDIA_HTTP_ERROR_MESSAGES[httpStatus],
    canRetry: true,
    httpStatus,
  };
}

/** DOM 无关框架之外的纯生命周期控制器；状态和错误绝不包含媒体 URL。 */
export function createMediaController(deps: MediaControllerDependencies): MediaController {
  let sequence = 0;
  let active: ActiveGeneration | null = null;
  let state: MediaControllerState = { status: "idle", generation: 0, readyReason: null };

  const publish = (next: MediaControllerState): void => {
    state = next;
    deps.onState(next);
  };

  const cleanElement = (): void => {
    try { deps.element.pause(); } catch { /* best-effort cleanup */ }
    deps.element.removeAttribute("src");
    try { deps.element.load(); } catch { /* best-effort cleanup */ }
  };

  const invalidate = (): void => {
    const old = active;
    // 先使 generation 失效并清控制器引用，再取消请求和触碰旧 element。
    active = null;
    if (!old) return;
    const objectUrl = old.objectUrl;
    old.objectUrl = null;
    old.abort.abort();
    old.operation += 1;
    old.removeListeners?.();
    old.removeListeners = null;
    cleanElement();
    if (objectUrl) deps.revokeObjectUrl(objectUrl);
  };

  const installSource = (
    generation: ActiveGeneration,
    operation: number,
    url: string,
    reason: MediaReadyReason,
    restore?: { time: number; wasPlaying: boolean }
  ): void => {
    if (active !== generation || generation.operation !== operation) {
      if (!deps.native) deps.revokeObjectUrl(url);
      return;
    }
    let readyHandled = false;
    const onReady = (): void => {
      if (active !== generation || generation.operation !== operation || readyHandled) return;
      readyHandled = true;
      if (restore) {
        try { deps.element.currentTime = restore.time; } catch { /* media may reject an invalid seek */ }
        if (restore.wasPlaying) void deps.element.play().catch(() => undefined);
      }
      deps.onReady?.(reason, deps.element);
      publish({ status: "ready", generation: generation.id, readyReason: reason });
    };
    const onError = (): void => {
      if (active !== generation || generation.operation !== operation) return;
      if (!deps.native || generation.retryUsed) {
        // 原生媒体错误不暴露可靠的 HTTP 状态，不能从 MediaError 推断分类。
        publish(errorState(generation.id, null));
        return;
      }
      generation.retryUsed = true;
      const resume = { time: deps.element.currentTime, wasPlaying: !deps.element.paused };
      void requestSource(generation, "retry-restored", resume);
    };
    deps.element.addEventListener("loadedmetadata", onReady);
    deps.element.addEventListener("canplay", onReady);
    deps.element.addEventListener("error", onError);
    generation.removeListeners = () => {
      deps.element.removeEventListener("loadedmetadata", onReady);
      deps.element.removeEventListener("canplay", onReady);
      deps.element.removeEventListener("error", onError);
    };
    if (!deps.native) generation.objectUrl = url;
    deps.element.src = url;
    deps.element.load();
  };

  const requestSource = async (
    generation: ActiveGeneration,
    reason: MediaReadyReason,
    restore?: { time: number; wasPlaying: boolean }
  ): Promise<void> => {
    if (active !== generation) return;
    generation.operation += 1;
    const operation = generation.operation;
    generation.abort.abort();
    generation.abort = new AbortController();
    generation.removeListeners?.();
    generation.removeListeners = null;
    const oldObjectUrl = generation.objectUrl;
    generation.objectUrl = null;
    cleanElement();
    if (oldObjectUrl) deps.revokeObjectUrl(oldObjectUrl);
    publish({ status: "loading", generation: generation.id, readyReason: null });
    try {
      const url = deps.native
        ? (await deps.fetchTicket(generation.videoId, generation.abort.signal)).url
        : await deps.fetchLegacyUrl(generation.videoId, generation.abort.signal);
      if (active !== generation || generation.operation !== operation) {
        if (!deps.native) deps.revokeObjectUrl(url);
        return;
      }
      installSource(generation, operation, url, reason, restore);
    } catch (error) {
      if (active !== generation || generation.operation !== operation || isAbort(error)) return;
      publish(errorState(generation.id, classifyHttpStatus(error)));
    }
  };

  return {
    load(videoId) {
      invalidate();
      const generation: ActiveGeneration = {
        id: ++sequence,
        videoId,
        abort: new AbortController(),
        operation: 0,
        retryUsed: false,
        objectUrl: null,
        removeListeners: null,
      };
      active = generation;
      void requestSource(generation, "initial");
      return generation.id;
    },
    dispose() {
      invalidate();
      publish({ status: "idle", generation: sequence, readyReason: null });
    },
    getState: () => state,
  };
}
