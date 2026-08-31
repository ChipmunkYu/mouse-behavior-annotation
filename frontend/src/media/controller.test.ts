import { describe, expect, it, vi } from "vitest";
import {
  createMediaController,
  MEDIA_CANCELLED_MESSAGE,
  MEDIA_PENDING_MESSAGE,
  MEDIA_PROXY_FAILED_MESSAGE,
  type MediaControllerState,
  type MediaElementLike,
} from "./controller";

class FakeMediaElement implements MediaElementLike {
  src = "";
  private listeners = new Map<string, Set<() => void>>();
  pause = vi.fn();
  load = vi.fn();
  removeAttribute = vi.fn((name: string) => { if (name === "src") this.src = ""; });
  addEventListener(type: "loadedmetadata" | "canplay" | "error", listener: () => void): void {
    const listeners = this.listeners.get(type) ?? new Set();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }
  removeEventListener(type: "loadedmetadata" | "canplay" | "error", listener: () => void): void {
    this.listeners.get(type)?.delete(listener);
  }
  emit(type: "loadedmetadata" | "canplay" | "error"): void {
    [...(this.listeners.get(type) ?? [])].forEach((listener) => listener());
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function setup(fetchBlob = vi.fn()) {
  const element = new FakeMediaElement();
  const states: MediaControllerState[] = [];
  const createObjectUrl = vi.fn(() => `blob:${createObjectUrl.mock.calls.length}`);
  const revokeObjectUrl = vi.fn();
  const controller = createMediaController({
    element,
    fetchBlob,
    createObjectUrl,
    revokeObjectUrl,
    onState: (state) => states.push(state),
  });
  return { controller, element, states, createObjectUrl, revokeObjectUrl, fetchBlob };
}

describe("complete-download media controller", () => {
  it("does not become ready or create an object URL before the full blob resolves", async () => {
    const download = deferred<{ blob: Blob; contentLength: number | null }>();
    const ctx = setup(vi.fn(() => download.promise));
    ctx.controller.load(1);

    expect(ctx.controller.getState().status).toBe("downloading");
    expect(ctx.createObjectUrl).not.toHaveBeenCalled();
    download.resolve({ blob: new Blob(["complete"]), contentLength: 8 });
    await Promise.resolve();

    expect(ctx.createObjectUrl).toHaveBeenCalledOnce();
    expect(ctx.controller.getState().status).toBe("downloading");
    ctx.element.emit("loadedmetadata");
    expect(ctx.controller.getState()).toMatchObject({ status: "ready", contentLength: 8, blobSize: 8 });
  });

  it("publishes real byte progress and ignores progress from an aborted generation", async () => {
    const callbacks: Array<(progress: { loaded: number; total: number | null }) => void> = [];
    const downloads = [
      deferred<{ blob: Blob; contentLength: number | null }>(),
      deferred<{ blob: Blob; contentLength: number | null }>(),
    ];
    const ctx = setup(vi.fn((_id, _signal, onProgress) => {
      callbacks.push(onProgress);
      return downloads[callbacks.length - 1].promise;
    }));

    ctx.controller.load(1);
    callbacks[0]({ loaded: 4, total: 10 });
    expect(ctx.controller.getState()).toMatchObject({ status: "downloading", loadedBytes: 4, totalBytes: 10 });

    ctx.controller.load(2);
    callbacks[0]({ loaded: 9, total: 10 });
    expect(ctx.controller.getState()).toMatchObject({ status: "downloading", generation: 2, loadedBytes: 0, totalBytes: null });
    callbacks[1]({ loaded: 3, total: null });
    expect(ctx.controller.getState()).toMatchObject({ status: "downloading", generation: 2, loadedBytes: 3, totalBytes: null });

    downloads[1].resolve({ blob: new Blob(["fresh"]), contentLength: null });
    await Promise.resolve();
  });

  it.each([
    ["DISPLAY_PROXY_PENDING", "pending", MEDIA_PENDING_MESSAGE, "display-pending"],
    ["DISPLAY_PROXY_FAILED", "failed", MEDIA_PROXY_FAILED_MESSAGE, "display-failed"],
  ] as const)("classifies 409 detail %s", async (detail, status, message, reason) => {
    const ctx = setup(vi.fn().mockRejectedValue({ status: 409, detail }));
    ctx.controller.load(1);
    await Promise.resolve();
    expect(ctx.controller.getState()).toMatchObject({ status, message, reason });
  });

  it("aborts cancellation without publishing a failure", async () => {
    let signal: AbortSignal | undefined;
    const ctx = setup(vi.fn((_id, currentSignal) => {
      signal = currentSignal;
      return new Promise((_resolve, reject) => currentSignal.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError"))));
    }));
    ctx.controller.load(1);
    ctx.controller.cancel();
    await Promise.resolve();

    expect(signal?.aborted).toBe(true);
    expect(ctx.controller.getState()).toMatchObject({ status: "cancelled", message: MEDIA_CANCELLED_MESSAGE });
    expect(ctx.states.some((state) => state.status === "failed")).toBe(false);
  });

  it("ignores a late generation and gives retry a fresh AbortController", async () => {
    const first = deferred<{ blob: Blob; contentLength: number | null }>();
    const second = deferred<{ blob: Blob; contentLength: number | null }>();
    const signals: AbortSignal[] = [];
    const fetchBlob = vi.fn((_id, signal) => {
      signals.push(signal);
      return signals.length === 1 ? first.promise : second.promise;
    });
    const ctx = setup(fetchBlob);
    ctx.controller.load(1);
    ctx.controller.load(1);

    expect(signals).toHaveLength(2);
    expect(signals[0]).not.toBe(signals[1]);
    expect(signals[0].aborted).toBe(true);
    first.resolve({ blob: new Blob(["stale"]), contentLength: 5 });
    await Promise.resolve();
    expect(ctx.createObjectUrl).not.toHaveBeenCalled();

    second.resolve({ blob: new Blob(["fresh"]), contentLength: null });
    await Promise.resolve();
    ctx.element.emit("canplay");
    expect(ctx.controller.getState()).toMatchObject({ status: "ready", generation: 2, blobSize: 5 });
  });

  it("retries after failure and revokes an installed URL exactly once with idempotent disposal", async () => {
    const blob = new Blob(["video"]);
    const fetchBlob = vi.fn()
      .mockRejectedValueOnce({ status: 500 })
      .mockResolvedValueOnce({ blob, contentLength: blob.size });
    const ctx = setup(fetchBlob);
    ctx.controller.load(1);
    await Promise.resolve();
    expect(ctx.controller.getState().status).toBe("failed");

    ctx.controller.load(1);
    await Promise.resolve();
    ctx.element.emit("loadedmetadata");
    expect(ctx.controller.getState().status).toBe("ready");
    expect(fetchBlob.mock.calls[0][1]).not.toBe(fetchBlob.mock.calls[1][1]);

    ctx.controller.dispose();
    ctx.controller.dispose();
    expect(ctx.revokeObjectUrl).toHaveBeenCalledTimes(1);
    expect(ctx.revokeObjectUrl).toHaveBeenCalledWith("blob:1");
  });

  it("immediately cleans and revokes once on a media element error", async () => {
    const retry = deferred<{ blob: Blob; contentLength: number | null }>();
    const fetchBlob = vi.fn()
      .mockResolvedValueOnce({ blob: new Blob(["video"]), contentLength: 5 })
      .mockReturnValueOnce(retry.promise);
    const ctx = setup(fetchBlob);
    ctx.controller.load(1);
    await Promise.resolve();

    ctx.element.emit("error");

    expect(ctx.controller.getState().status).toBe("failed");
    expect(ctx.element.src).toBe("");
    expect(ctx.revokeObjectUrl).toHaveBeenCalledTimes(1);
    expect(ctx.revokeObjectUrl).toHaveBeenCalledWith("blob:1");

    ctx.controller.load(1);
    ctx.controller.dispose();
    expect(ctx.revokeObjectUrl).toHaveBeenCalledTimes(1);
  });

  it("immediately cleans and revokes once when element.load throws", async () => {
    const retry = deferred<{ blob: Blob; contentLength: number | null }>();
    const fetchBlob = vi.fn()
      .mockResolvedValueOnce({ blob: new Blob(["video"]), contentLength: 5 })
      .mockReturnValueOnce(retry.promise);
    const ctx = setup(fetchBlob);
    ctx.element.load.mockImplementationOnce(() => { throw new Error("load failed"); });

    ctx.controller.load(1);
    await Promise.resolve();

    expect(ctx.controller.getState().status).toBe("failed");
    expect(ctx.element.src).toBe("");
    expect(ctx.revokeObjectUrl).toHaveBeenCalledTimes(1);
    expect(ctx.revokeObjectUrl).toHaveBeenCalledWith("blob:1");

    ctx.controller.load(1);
    ctx.controller.dispose();
    expect(ctx.revokeObjectUrl).toHaveBeenCalledTimes(1);
  });
});
