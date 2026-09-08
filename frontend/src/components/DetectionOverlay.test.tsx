// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DetectionImport, DetectionsResponse, DetectionWithTrack } from "../api/types";

const apiMocks = vi.hoisted(() => ({
  getCurrentDetectionImport: vi.fn<() => Promise<DetectionImport>>(),
  getDetections: vi.fn<(
    projectId: number | string,
    videoId: number | string,
    startFrame: number,
    endFrame: number,
    signal?: AbortSignal,
  ) => Promise<DetectionsResponse>>(),
}));

vi.mock("../api", () => apiMocks);

import DetectionOverlay, { detectionBlockForFrame, detectionRangeComplete, type OverlayFrameData } from "./DetectionOverlay";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const detectionImport: DetectionImport = {
  id: 1,
  revision: 1,
  schema_version: "1",
  status: "active",
  fps: 30,
};

const detection = (frame_index: number, display_track_id: number): DetectionWithTrack => ({
  detection_id: display_track_id,
  frame_index,
  raw_track_id: display_track_id,
  display_track_id,
  box_xyxy_px: null,
  keypoints: null,
  import_revision: 1,
  identity_revision: 1,
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  apiMocks.getCurrentDetectionImport.mockReset().mockResolvedValue(detectionImport);
  apiMocks.getDetections.mockReset();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    disconnect() {}
  });
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

describe("检测缓存块", () => {
  it("按固定 31 帧边界对齐", () => {
    expect(detectionBlockForFrame(0)).toEqual({ start: 0, end: 30 });
    expect(detectionBlockForFrame(30)).toEqual({ start: 0, end: 30 });
    expect(detectionBlockForFrame(31)).toEqual({ start: 31, end: 61 });
  });

  it("只在 total 不超过实际返回数时标记范围完整", () => {
    expect(detectionRangeComplete(500, 500)).toBe(true);
    expect(detectionRangeComplete(501, 500)).toBe(false);
    expect(detectionRangeComplete(0, 0)).toBe(true);
  });
});

describe("DetectionOverlay 异步缓存", () => {
  it("同块连续帧只请求一次，跨块 abort 且忽略旧请求晚到", async () => {
    const oldRange = deferred<DetectionsResponse>();
    const newRange = deferred<DetectionsResponse>();
    apiMocks.getDetections
      .mockImplementationOnce(() => oldRange.promise)
      .mockImplementationOnce(() => newRange.promise);
    const onFrameData = vi.fn<(data: OverlayFrameData) => void>();
    const video = document.createElement("video");

    const renderFrame = async (frame: number) => {
      video.currentTime = frame / 30;
      await act(async () => {
        root.render(<DetectionOverlay projectId={1} videoId={2} video={video} currentTime={frame / 30} onFrameData={onFrameData} />);
        await Promise.resolve();
      });
    };

    await renderFrame(0);
    await renderFrame(1);
    await renderFrame(30);
    expect(apiMocks.getDetections).toHaveBeenCalledTimes(1);
    expect(apiMocks.getDetections).toHaveBeenNthCalledWith(1, 1, 2, 0, 30, expect.any(AbortSignal));
    const oldSignal = apiMocks.getDetections.mock.calls[0][4]!;

    await renderFrame(31);
    expect(oldSignal.aborted).toBe(true);
    expect(apiMocks.getDetections).toHaveBeenCalledTimes(2);
    expect(apiMocks.getDetections).toHaveBeenNthCalledWith(2, 1, 2, 31, 61, expect.any(AbortSignal));

    await act(async () => newRange.resolve({ detections: [detection(31, 31)], total: 1 }));
    const current = onFrameData.mock.calls[onFrameData.mock.calls.length - 1][0];
    expect(current).toMatchObject({ frame: 31, status: "complete" });
    expect(current.detections.map((item) => item.display_track_id)).toEqual([31]);
    const callbackCount = onFrameData.mock.calls.length;

    await act(async () => oldRange.resolve({ detections: [detection(30, 30)], total: 1 }));
    expect(onFrameData).toHaveBeenCalledTimes(callbackCount);
    expect(onFrameData.mock.calls[onFrameData.mock.calls.length - 1][0].detections.map((item) => item.display_track_id)).toEqual([31]);
    expect(host.textContent).not.toContain("检测读取失败");
  });

  it("切块产生的 AbortError 不显示为用户错误", async () => {
    apiMocks.getDetections.mockImplementation((_projectId, _videoId, start, _end, signal) => {
      if (start !== 0) return Promise.resolve({ detections: [detection(31, 31)], total: 1 });
      return new Promise((_resolve, reject) => {
        signal?.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")));
      });
    });
    const video = document.createElement("video");

    video.currentTime = 0;
    await act(async () => {
      root.render(<DetectionOverlay projectId={1} videoId={2} video={video} currentTime={0} />);
      await Promise.resolve();
    });
    video.currentTime = 31 / 30;
    await act(async () => {
      root.render(<DetectionOverlay projectId={1} videoId={2} video={video} currentTime={31 / 30} />);
      await Promise.resolve();
    });

    expect(apiMocks.getDetections).toHaveBeenCalledTimes(2);
    expect(host.textContent).not.toContain("检测读取失败");
  });
});
