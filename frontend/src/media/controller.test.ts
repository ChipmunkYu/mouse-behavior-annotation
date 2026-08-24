import { describe, expect, it, vi } from "vitest";
import { createMediaController, MEDIA_LOAD_ERROR, type MediaControllerState, type MediaElementLike } from "./controller";

class FakeMediaElement implements MediaElementLike {
  src = "";
  currentTime = 0;
  paused = true;
  readonly log: string[];
  private listeners = new Map<string, Set<() => void>>();
  constructor(log: string[]) { this.log = log; }
  pause(): void { this.log.push("pause"); this.paused = true; }
  play(): Promise<void> { this.log.push("play"); this.paused = false; return Promise.resolve(); }
  load(): void { this.log.push("load"); }
  removeAttribute(name: string): void { this.log.push(`remove:${name}`); if (name === "src") this.src = ""; }
  addEventListener(type: "loadedmetadata" | "canplay" | "error", listener: () => void): void {
    const set = this.listeners.get(type) ?? new Set(); set.add(listener); this.listeners.set(type, set);
  }
  removeEventListener(type: "loadedmetadata" | "canplay" | "error", listener: () => void): void { this.listeners.get(type)?.delete(listener); }
  emit(type: "loadedmetadata" | "canplay" | "error"): void { [...(this.listeners.get(type) ?? [])].forEach((fn) => fn()); }
  snapshot(type: "loadedmetadata" | "canplay" | "error"): Array<() => void> { return [...(this.listeners.get(type) ?? [])]; }
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("media controller", () => {
  it("aborts stale work, performs cleanup in strict order, and revokes a stale blob", async () => {
    const log: string[] = [];
    const element = new FakeMediaElement(log);
    const first = deferred<string>();
    const second = deferred<string>();
    let call = 0;
    const fetchLegacyUrl = vi.fn((_id: number | string, signal: AbortSignal) => {
      signal.addEventListener("abort", () => log.push("abort"));
      return (++call === 1 ? first : second).promise;
    });
    const revoke = vi.fn((url: string) => log.push(`revoke:${url}`));
    const controller = createMediaController({
      element, native: false, fetchLegacyUrl, revokeObjectUrl: revoke,
      fetchTicket: vi.fn(), onState: vi.fn(),
    });
    controller.load(1);
    log.length = 0;
    controller.load(2);
    expect(log.slice(0, 4)).toEqual(["abort", "pause", "remove:src", "load"]);
    first.resolve("blob:stale");
    await Promise.resolve();
    expect(revoke).toHaveBeenCalledWith("blob:stale");
    second.resolve("blob:active");
    await Promise.resolve();
    element.emit("loadedmetadata");
    expect(controller.getState()).toMatchObject({ status: "ready", generation: 2, readyReason: "initial" });
    controller.dispose();
    expect(revoke).toHaveBeenCalledWith("blob:active");
  });

  it("synchronously reports initial readiness with the media element", async () => {
    const element = new FakeMediaElement([]);
    const onReady = vi.fn();
    const controller = createMediaController({
      element, native: true, onReady,
      fetchTicket: vi.fn().mockResolvedValue({ url: "/media/initial", expires_at: "later" }),
      fetchLegacyUrl: vi.fn(), revokeObjectUrl: vi.fn(), onState: vi.fn(),
    });

    controller.load(1);
    await Promise.resolve();
    element.emit("loadedmetadata");

    expect(onReady).toHaveBeenCalledOnce();
    expect(onReady).toHaveBeenCalledWith("initial", element);
  });

  it("renews a native ticket at most once and restores time/play before retry ready", async () => {
    const log: string[] = [];
    const element = new FakeMediaElement(log);
    const states: MediaControllerState[] = [];
    const fetchTicket = vi.fn()
      .mockResolvedValueOnce({ url: "/media/first", expires_at: "soon" })
      .mockResolvedValueOnce({ url: "/media/second", expires_at: "later" });
    const onReady = vi.fn((reason, readyElement) => {
      log.push(`ready:${reason}:${readyElement.currentTime}:${readyElement.paused ? "paused" : "playing"}`);
    });
    const controller = createMediaController({
      element, native: true, fetchTicket, fetchLegacyUrl: vi.fn(),
      revokeObjectUrl: vi.fn(), onState: (state) => states.push(state), onReady,
    });
    controller.load(1);
    await Promise.resolve();
    element.currentTime = 12.5;
    element.paused = false;
    element.emit("error");
    await Promise.resolve();
    element.currentTime = 0;
    element.paused = true;
    element.emit("canplay");
    expect(element.currentTime).toBe(12.5);
    expect(log).toContain("play");
    expect(log.slice(-2)).toEqual(["play", "ready:retry-restored:12.5:playing"]);
    expect(onReady).toHaveBeenCalledWith("retry-restored", element);
    expect(states[states.length - 1]).toMatchObject({ status: "ready", readyReason: "retry-restored" });
    Object.assign(element, {
      currentSrc: "https://secret.example/video?token=element-secret",
      error: { code: 4, message: "HTTP 403 private media detail", status: 403 },
    });
    element.emit("error");
    expect(fetchTicket).toHaveBeenCalledTimes(2);
    expect(controller.getState()).toEqual({
      status: "error", generation: 1, readyReason: null, message: MEDIA_LOAD_ERROR, canRetry: true, httpStatus: null,
    });
    expect(JSON.stringify(controller.getState())).not.toContain("secret.example");
    expect(JSON.stringify(controller.getState())).not.toContain("HTTP 403");
  });

  it("ignores queued callbacks from the old source while renewing within one generation", async () => {
    const element = new FakeMediaElement([]);
    const renewal = deferred<{ url: string; expires_at: string }>();
    const fetchTicket = vi.fn()
      .mockResolvedValueOnce({ url: "/media/first", expires_at: "soon" })
      .mockReturnValueOnce(renewal.promise);
    const onReady = vi.fn();
    const controller = createMediaController({
      element, native: true, fetchTicket, fetchLegacyUrl: vi.fn(),
      revokeObjectUrl: vi.fn(), onState: vi.fn(), onReady,
    });

    controller.load(1);
    await Promise.resolve();
    const oldReady = [...element.snapshot("loadedmetadata"), ...element.snapshot("canplay")];
    const [oldError] = element.snapshot("error");
    element.currentTime = 8;
    element.paused = false;

    oldError();
    expect(controller.getState()).toMatchObject({ status: "loading", generation: 1 });
    expect(element.src).toBe("");
    oldReady.forEach((listener) => listener());
    oldError();
    expect(controller.getState()).toMatchObject({ status: "loading", generation: 1 });
    expect(onReady).not.toHaveBeenCalled();
    expect(fetchTicket).toHaveBeenCalledTimes(2);

    renewal.resolve({ url: "/media/second", expires_at: "later" });
    await Promise.resolve();
    expect(element.src).toBe("/media/second");
    oldReady.forEach((listener) => listener());
    oldError();
    expect(controller.getState()).toMatchObject({ status: "loading", generation: 1 });
    expect(element.src).toBe("/media/second");
    expect(onReady).not.toHaveBeenCalled();
    expect(fetchTicket).toHaveBeenCalledTimes(2);

    element.emit("canplay");
    expect(element.currentTime).toBe(8);
    expect(element.paused).toBe(false);
    expect(onReady).toHaveBeenCalledOnce();
    expect(onReady).toHaveBeenCalledWith("retry-restored", element);
    expect(controller.getState()).toMatchObject({ status: "ready", generation: 1, readyReason: "retry-restored" });
  });

  it.each([
    [401, "登录已过期，请重新登录后重试。"],
    [403, "你没有权限访问此视频，请联系项目管理员。"],
    [404, "视频不存在或已被删除，请返回视频列表。"],
  ] as const)("classifies a structured %i ticket error with a fixed safe message", async (status, message) => {
    const malicious = `private detail https://secret.example/video?token=status-${status}`;
    const error = Object.assign(new Error(malicious), { status, detail: malicious, url: malicious });
    const controller = createMediaController({
      element: new FakeMediaElement([]), native: true,
      fetchTicket: vi.fn().mockRejectedValue(error), fetchLegacyUrl: vi.fn(),
      revokeObjectUrl: vi.fn(), onState: vi.fn(),
    });

    controller.load(1);
    await Promise.resolve();

    expect(controller.getState()).toEqual({
      status: "error", generation: 1, readyReason: null, message, canRetry: true, httpStatus: status,
    });
    const serialized = JSON.stringify(controller.getState());
    expect(serialized).not.toContain("private detail");
    expect(serialized).not.toContain("secret.example");
    expect(serialized).not.toContain("token=");
  });

  it("uses the generic classification for unknown and non-numeric statuses", async () => {
    const fetchLegacyUrl = vi.fn()
      .mockRejectedValueOnce({ status: 500, message: "https://secret.example/server" })
      .mockRejectedValueOnce({ status: "403", message: "private detail" });
    const controller = createMediaController({
      element: new FakeMediaElement([]), native: false,
      fetchTicket: vi.fn(), fetchLegacyUrl, revokeObjectUrl: vi.fn(), onState: vi.fn(),
    });

    controller.load(1);
    await Promise.resolve();
    expect(controller.getState()).toEqual({
      status: "error", generation: 1, readyReason: null, message: MEDIA_LOAD_ERROR, canRetry: true, httpStatus: null,
    });
    expect(JSON.stringify(controller.getState())).not.toContain("secret.example");

    controller.load(2);
    await Promise.resolve();
    expect(controller.getState()).toMatchObject({ status: "error", message: MEDIA_LOAD_ERROR, httpStatus: null });
  });

  it("resets retry allowance for a new video and exposes only a sanitized error", async () => {
    const element = new FakeMediaElement([]);
    const fetchTicket = vi.fn().mockRejectedValue(new Error("private source detail"));
    const controller = createMediaController({
      element, native: true, fetchTicket, fetchLegacyUrl: vi.fn(), revokeObjectUrl: vi.fn(), onState: vi.fn(),
    });
    controller.load(1);
    await Promise.resolve();
    expect(JSON.stringify(controller.getState())).toBe(JSON.stringify({
      status: "error", generation: 1, readyReason: null, message: MEDIA_LOAD_ERROR, canRetry: true, httpStatus: null,
    }));
    fetchTicket.mockResolvedValue({ url: "/media/new", expires_at: "later" });
    controller.load(2);
    await Promise.resolve();
    element.emit("error");
    await Promise.resolve();
    expect(fetchTicket).toHaveBeenCalledTimes(3);
  });
});
