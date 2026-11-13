// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { droppedBatchFiles } from "../components/VideoUploadPanel";
import { executeUploadTask, uploadErrorMessage } from "./uploadTasks";
import type { TaskExecutionCallbacks } from "./uploadTasks";
import type { UploadTask } from "./types";

const mocks = vi.hoisted(() => ({ createImportBatch: vi.fn(), uploadBatchFile: vi.fn(), completeImportBatch: vi.fn(), getImportBatch: vi.fn(), cancelImportBatch: vi.fn(), uploadVideo: vi.fn() }));
vi.mock("../api", () => mocks);

function task(): UploadTask {
  return { taskId: "local-1", kind: "batch", source: "heuristic", projectId: 1, projectName: "test", assigneeMembershipId: 8, name: "event · #1", status: "running", phase: "queued", progress: 0, error: null, batchId: null,
    slots: { tracks: { file: new File(["[]"], "tracks.json"), percent: 0 }, metadata: { file: new File(["{}"], "metadata.json"), percent: 0 }, annotation: { file: new File(["{}"], "annotation.json"), percent: 0 }, video: { file: new File(["video"], "clip.mp4"), percent: 0 } } };
}
function callbacks(): TaskExecutionCallbacks { return { update: vi.fn(), batchCreated: vi.fn(), succeeded: vi.fn(), failed: vi.fn(), isCurrent: () => true }; }

beforeEach(() => {
  vi.resetAllMocks();
  mocks.createImportBatch.mockResolvedValue({ id: 91, source: "heuristic", status: "uploading" });
  mocks.uploadBatchFile.mockResolvedValue({});
  mocks.completeImportBatch.mockResolvedValue({ status: "ready", source: "heuristic" });
  mocks.getImportBatch.mockResolvedValue({ id: 91, status: "failed", source: "heuristic" });
  mocks.cancelImportBatch.mockResolvedValue(undefined);
});

describe("启发式四文件沿用上传队列", () => {
  it("结构化详情保留实际code/消息/字段，嵌套数组兼容且不展示服务器路径或任意context", () => {
    const message = uploadErrorMessage(new ApiError(400, "generic", { detail: { code: "invalid_input", message: "Validation failed; cancel this batch and retry", validation_errors: [
      "fps mismatch", { code: "invalid_box", message: "检测框越界", path: "tracks[0].box_xyxy_px", context: { private: "SECRET" } },
      { message: "cannot read /srv/private/input.json", path: "C:\\private\\input.json" },
    ] } }));
    for (const text of ["invalid_input", "fps mismatch", "invalid_box", "检测框越界", "tracks[0].box_xyxy_px"]) expect(message).toContain(text);
    for (const text of ["SECRET", "/srv/", "C:\\private", "cancel this batch", "[object Object]"]) expect(message).not.toContain(text);
    expect(uploadErrorMessage(new ApiError(503, "generic", { detail: { code: "heuristic_dependency_unavailable", message: "numpy unavailable", validation_errors: { code: "missing_dependency" } } }))).toContain("请联系管理员修复环境后重试");
    expect(uploadErrorMessage(new ApiError(400, "generic", { detail: { internal_path: "/srv/secret" } }))).toBe("上传失败，未收到可展示的错误详情，请联系管理员。");
    expect(uploadErrorMessage(new ApiError(401, "登录已过期"))).toBe("登录已过期");
  });

  it("实际failed completion对象详情可见，自动清理成功清空batch引用；清理失败保留", async () => {
    mocks.completeImportBatch.mockResolvedValue({ status: "failed", message: "Validation failed; cancel this batch and retry", validation_errors: { code: "bad_annotation", validation_errors: ["frame_range invalid"] } });
    let current = task();
    const cb = callbacks();
    vi.mocked(cb.batchCreated).mockImplementation((id) => { current = { ...current, batchId: id }; });
    vi.mocked(cb.update).mockImplementation((change) => { current = change(current); });
    await executeUploadTask(current, new AbortController().signal, cb);
    expect(cb.failed).toHaveBeenCalledWith(expect.stringContaining("bad_annotation"));
    expect(cb.failed).toHaveBeenCalledWith(expect.stringContaining("frame_range invalid"));
    expect(cb.failed).not.toHaveBeenCalledWith(expect.stringContaining("cancel this batch"));
    expect(current).toMatchObject({ batchId: null, batchCleaned: true });
    current = task();
    mocks.cancelImportBatch.mockRejectedValueOnce(new ApiError(0, "清理断线"));
    await executeUploadTask(current, new AbortController().signal, cb);
    expect(current.batchId).toBe(91);
    expect(current.batchCleaned).not.toBe(true);
    expect(cb.failed).toHaveBeenLastCalledWith(expect.stringContaining("清理待重试"));
  });

  it("GET状态不确定时不DELETE；服务端已清理404同步本地引用", async () => {
    mocks.uploadBatchFile.mockRejectedValue(new ApiError(400, "文件不合法"));
    mocks.getImportBatch.mockRejectedValueOnce(new ApiError(0, "断线"));
    const cb = callbacks();
    await executeUploadTask(task(), new AbortController().signal, cb);
    expect(mocks.cancelImportBatch).not.toHaveBeenCalled();
    expect(cb.failed).toHaveBeenCalledWith(expect.stringContaining("清理待重试"));
    mocks.getImportBatch.mockRejectedValueOnce(new ApiError(404, "not found"));
    let current = task();
    vi.mocked(cb.update).mockImplementation((change) => { current = change(current); });
    await executeUploadTask(current, new AbortController().signal, cb);
    expect(current).toMatchObject({ batchId: null, batchCleaned: true });
    expect(mocks.cancelImportBatch).not.toHaveBeenCalled();
  });
  it("四 role 顺序、source、负责人保持 query 包装参数", async () => {
    const cb = callbacks();
    await executeUploadTask(task(), new AbortController().signal, cb);
    expect(mocks.createImportBatch).toHaveBeenCalledWith(1, expect.any(AbortSignal), "heuristic");
    expect(mocks.uploadBatchFile.mock.calls.map((call) => call[2])).toEqual(["tracks", "metadata", "annotation", "video"]);
    expect(mocks.completeImportBatch).toHaveBeenCalledWith(1, 91, 8, expect.any(AbortSignal));
    expect(cb.succeeded).toHaveBeenCalledOnce();
  });

  it("complete 断线后重查成功，不清理历史成功；processing 同样保留", async () => {
    mocks.completeImportBatch.mockRejectedValue(new ApiError(0, "断线"));
    mocks.getImportBatch.mockResolvedValue({ status: "ready", source: "heuristic" });
    const cb = callbacks();
    await executeUploadTask(task(), new AbortController().signal, cb);
    expect(cb.succeeded).toHaveBeenCalledOnce();
    expect(mocks.cancelImportBatch).not.toHaveBeenCalled();
    mocks.getImportBatch.mockResolvedValue({ status: "processing", source: "heuristic" });
    const pending = callbacks();
    await executeUploadTask(task(), new AbortController().signal, pending);
    expect(pending.failed).toHaveBeenCalledWith(expect.stringContaining("仍在处理"));
    expect(mocks.cancelImportBatch).not.toHaveBeenCalled();
  });

  it("单项失败受保护清理，继续下一项；清理失败明确保留提示", async () => {
    mocks.uploadBatchFile.mockRejectedValueOnce(new Error("tracks 坏文件"));
    const failed = callbacks();
    await executeUploadTask(task(), new AbortController().signal, failed);
    expect(mocks.cancelImportBatch).toHaveBeenCalledWith(1, 91, expect.any(AbortSignal));
    expect(failed.failed).toHaveBeenCalledWith("tracks 坏文件");
    const next = callbacks();
    await executeUploadTask({ ...task(), taskId: "local-2" }, new AbortController().signal, next);
    expect(next.succeeded).toHaveBeenCalledOnce();
    mocks.uploadBatchFile.mockRejectedValueOnce(new Error("坏文件"));
    mocks.cancelImportBatch.mockRejectedValue(new Error("清理断线"));
    const cleanup = callbacks();
    await executeUploadTask(task(), new AbortController().signal, cleanup);
    expect(cleanup.failed).toHaveBeenCalledWith(expect.stringContaining("清理待重试"));
  });

  it("四文件拖放分槽；重复文件原子拒绝，不覆盖现有槽；manual 三文件保留", () => {
    const empty = { video: null, tracks: null, metadata: null, annotation: null };
    const files = Object.values(task().slots!).map((slot) => slot!.file);
    const slots = droppedBatchFiles(files, true, empty);
    expect(slots.annotation?.name).toBe("annotation.json");
    expect(() => droppedBatchFiles([files[0], files[0]], true, empty)).toThrow("重复文件槽");
    expect(() => droppedBatchFiles(files, true, slots)).toThrow("重复文件槽");
    expect(empty.tracks).toBeNull();
    expect(droppedBatchFiles([new File([""], "tracks.jsonl")], false, empty).tracks?.name).toBe("tracks.jsonl");
    expect(() => droppedBatchFiles([new File([""], "tracks.jsonl")], true, empty)).toThrow("无法分槽");
  });

  it("启发式意外 video_only 不假报成功，也不删除已发布批次", async () => {
    mocks.completeImportBatch.mockResolvedValue({ status: "video_only", source: "heuristic" });
    mocks.getImportBatch.mockResolvedValue({ status: "video_only", source: "heuristic" });
    const cb = callbacks();
    await executeUploadTask(task(), new AbortController().signal, cb);
    expect(cb.succeeded).not.toHaveBeenCalled();
    expect(cb.failed).toHaveBeenCalledWith(expect.stringContaining("已发布批次不清理"));
    expect(mocks.cancelImportBatch).not.toHaveBeenCalled();
  });
});
