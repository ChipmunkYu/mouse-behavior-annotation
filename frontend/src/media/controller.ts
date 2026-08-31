import type { VideoStreamBlob, VideoStreamProgress } from "../api";

export type MediaReadyReason = "initial" | "retry-restored";
export type MediaHttpStatus = 401 | 403 | 404 | 409 | null;
export type MediaPendingReason = "display-pending";
export type MediaFailureReason = "display-failed" | "http" | "network";

interface MediaStateBase {
  generation: number;
  readyReason: MediaReadyReason | null;
}

export type MediaControllerState =
  | (MediaStateBase & { status: "idle" })
  | (MediaStateBase & { status: "downloading"; loadedBytes: number; totalBytes: number | null })
  | (MediaStateBase & { status: "ready"; readyReason: MediaReadyReason; contentLength: number | null; blobSize: number })
  | (MediaStateBase & { status: "pending"; message: string; canRetry: true; reason: MediaPendingReason })
  | (MediaStateBase & { status: "failed"; message: string; canRetry: true; httpStatus: MediaHttpStatus; reason: MediaFailureReason })
  | (MediaStateBase & { status: "cancelled"; message: string; canRetry: true });

export const MEDIA_DOWNLOAD_MESSAGE = "正在下载，完成后才能播放。";
export const MEDIA_CANCELLED_MESSAGE = "已取消下载。";
export const MEDIA_PENDING_MESSAGE = "播放资源正在处理中，请稍后重试。";
export const MEDIA_PROXY_FAILED_MESSAGE = "播放资源处理失败，请联系项目管理员。";
export const MEDIA_LOAD_ERROR = "视频下载失败，请检查网络后重试。";

const MEDIA_HTTP_ERROR_MESSAGES: Record<401 | 403 | 404, string> = {
  401: "登录已过期，请重新登录后重试。",
  403: "你没有权限访问此视频，请联系项目管理员。",
  404: "视频不存在或已被删除，请返回视频列表。",
};

export interface MediaElementLike {
  src: string;
  pause(): void;
  load(): void;
  removeAttribute(name: string): void;
  addEventListener(type: "loadedmetadata" | "canplay" | "error", listener: () => void): void;
  removeEventListener(type: "loadedmetadata" | "canplay" | "error", listener: () => void): void;
}

export interface MediaControllerDependencies {
  element: MediaElementLike;
  fetchBlob: (videoId: number | string, signal: AbortSignal, onProgress: (progress: VideoStreamProgress) => void) => Promise<VideoStreamBlob>;
  createObjectUrl: (blob: Blob) => string;
  revokeObjectUrl: (url: string) => void;
  onState: (state: MediaControllerState) => void;
  onReady?: (reason: MediaReadyReason, element: MediaElementLike) => void;
}

export interface MediaController {
  load(videoId: number | string): number;
  cancel(): void;
  dispose(): void;
  getState(): MediaControllerState;
}

interface ActiveGeneration {
  id: number;
  videoId: number | string;
  operation: number;
  abort: AbortController | null;
  objectUrl: string | null;
  removeListeners: (() => void) | null;
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function errorFields(error: unknown): { status: MediaHttpStatus; detail: unknown } {
  if (!error || typeof error !== "object") return { status: null, detail: null };
  try {
    const value = error as { status?: unknown; detail?: unknown };
    const status = value.status;
    return {
      status: status === 401 || status === 403 || status === 404 || status === 409 ? status : null,
      detail: value.detail,
    };
  } catch {
    return { status: null, detail: null };
  }
}

function classifiedError(generation: number, error: unknown): MediaControllerState {
  const { status, detail } = errorFields(error);
  if (status === 409 && detail === "DISPLAY_PROXY_PENDING") {
    return { status: "pending", generation, readyReason: null, message: MEDIA_PENDING_MESSAGE, canRetry: true, reason: "display-pending" };
  }
  const displayFailed = status === 409 && detail === "DISPLAY_PROXY_FAILED";
  return {
    status: "failed",
    generation,
    readyReason: null,
    message: displayFailed ? MEDIA_PROXY_FAILED_MESSAGE : status === 401 || status === 403 || status === 404 ? MEDIA_HTTP_ERROR_MESSAGES[status] : MEDIA_LOAD_ERROR,
    canRetry: true,
    httpStatus: status,
    reason: displayFailed ? "display-failed" : status === null ? "network" : "http",
  };
}

/** 完整下载、generation、取消和 object URL 所有权都集中在此控制器。 */
export function createMediaController(deps: MediaControllerDependencies): MediaController {
  let sequence = 0;
  let active: ActiveGeneration | null = null;
  let state: MediaControllerState = { status: "idle", generation: 0, readyReason: null };

  const publish = (next: MediaControllerState): void => {
    state = next;
    deps.onState(next);
  };

  const cleanElement = (): void => {
    try { deps.element.pause(); } catch { /* best effort */ }
    deps.element.removeAttribute("src");
    try { deps.element.load(); } catch { /* best effort */ }
  };

  const releaseGeneration = (generation: ActiveGeneration): void => {
    generation.operation += 1;
    generation.abort?.abort();
    generation.abort = null;
    generation.removeListeners?.();
    generation.removeListeners = null;
    const url = generation.objectUrl;
    generation.objectUrl = null;
    cleanElement();
    if (url) deps.revokeObjectUrl(url);
  };

  const invalidate = (): void => {
    const old = active;
    active = null;
    if (old) releaseGeneration(old);
  };

  const failGeneration = (generation: ActiveGeneration, operation: number, error: unknown): void => {
    if (active !== generation || generation.operation !== operation) return;
    const failedState = classifiedError(generation.id, error);
    releaseGeneration(generation);
    publish(failedState);
  };

  const request = async (generation: ActiveGeneration): Promise<void> => {
    if (active !== generation) return;
    const operation = ++generation.operation;
    const abort = new AbortController();
    generation.abort = abort;
    publish({ status: "downloading", generation: generation.id, readyReason: null, loadedBytes: 0, totalBytes: null });
    try {
      const result = await deps.fetchBlob(generation.videoId, abort.signal, ({ loaded, total }) => {
        if (active !== generation || generation.operation !== operation || abort.signal.aborted) return;
        const loadedBytes = Number.isFinite(loaded) ? Math.max(0, loaded) : 0;
        const totalBytes = total !== null && Number.isFinite(total) && total >= 0 ? total : null;
        publish({ status: "downloading", generation: generation.id, readyReason: null, loadedBytes, totalBytes });
      });
      if (active !== generation || generation.operation !== operation) return;
      generation.abort = null;
      const objectUrl = deps.createObjectUrl(result.blob);
      if (active !== generation || generation.operation !== operation) {
        deps.revokeObjectUrl(objectUrl);
        return;
      }
      generation.objectUrl = objectUrl;
      let readyHandled = false;
      const onReady = (): void => {
        if (active !== generation || generation.operation !== operation || readyHandled) return;
        readyHandled = true;
        deps.onReady?.("initial", deps.element);
        publish({ status: "ready", generation: generation.id, readyReason: "initial", contentLength: result.contentLength, blobSize: result.blob.size });
      };
      const onError = (): void => {
        failGeneration(generation, operation, null);
      };
      deps.element.addEventListener("loadedmetadata", onReady);
      deps.element.addEventListener("canplay", onReady);
      deps.element.addEventListener("error", onError);
      generation.removeListeners = () => {
        deps.element.removeEventListener("loadedmetadata", onReady);
        deps.element.removeEventListener("canplay", onReady);
        deps.element.removeEventListener("error", onError);
      };
      deps.element.src = objectUrl;
      deps.element.load();
    } catch (error) {
      if (active !== generation || generation.operation !== operation || isAbort(error)) return;
      failGeneration(generation, operation, error);
    }
  };

  return {
    load(videoId) {
      invalidate();
      const generation: ActiveGeneration = {
        id: ++sequence, videoId, operation: 0, abort: null, objectUrl: null, removeListeners: null,
      };
      active = generation;
      void request(generation);
      return generation.id;
    },
    cancel() {
      const generation = active;
      if (!generation || state.status !== "downloading") return;
      releaseGeneration(generation);
      publish({ status: "cancelled", generation: generation.id, readyReason: null, message: MEDIA_CANCELLED_MESSAGE, canRetry: true });
    },
    dispose() {
      invalidate();
      publish({ status: "idle", generation: sequence, readyReason: null });
    },
    getState: () => state,
  };
}
