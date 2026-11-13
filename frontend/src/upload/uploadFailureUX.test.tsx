// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { UploadManagerProvider, useUploadManager } from "./UploadManagerContext";
import UploadTaskTray from "./UploadTaskTray";

const mocks = vi.hoisted(() => ({ createImportBatch: vi.fn(), uploadBatchFile: vi.fn(), completeImportBatch: vi.fn(), getImportBatch: vi.fn(), cancelImportBatch: vi.fn(), uploadVideo: vi.fn() }));
vi.mock("../api", () => mocks);
vi.mock("../auth/AuthContext", () => ({ useAuth: () => ({ token: "test-session" }) }));
(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let manager: ReturnType<typeof useUploadManager>;
function Probe() { manager = useUploadManager(); return <UploadTaskTray />; }
const input = { projectId: 1, projectName: "project", assigneeMembershipId: null, source: "heuristic" as const, displayName: "event", files: { video: new File(["video"], "clip.mp4"), tracks: new File(["[]"], "tracks.json"), metadata: new File(["{}"], "metadata.json"), annotation: new File(["{}"], "annotation.json") } };
let root: Root, host: HTMLDivElement;
beforeEach(() => {
  vi.resetAllMocks(); host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  let nextId = 90;
  mocks.createImportBatch.mockImplementation(async () => ({ id: ++nextId, source: "heuristic", status: "uploading" }));
  mocks.uploadBatchFile.mockResolvedValue({});
  mocks.completeImportBatch.mockImplementation(async (_pid, id) => id === 91 ? { status: "failed", message: "Validation failed; cancel this batch and retry", validation_errors: { code: "bad_input", validation_errors: ["frame_range invalid"] } } : { status: "ready", source: "heuristic" });
  mocks.getImportBatch.mockResolvedValue({ status: "failed", source: "heuristic" });
  mocks.cancelImportBatch.mockResolvedValue(undefined);
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });
async function start() { await act(async () => root.render(<UploadManagerProvider><Probe /></UploadManagerProvider>)); }

it("失败项清理后只显示重试，继续后项且失败名汇总保留；重试新batch而不再查询/取消旧batch", async () => {
  await start();
  await act(async () => { manager.enqueueBatch(input); manager.enqueueBatch(input); });
  expect(manager.tasks.map((task) => task.status)).toEqual(["failed", "success"]);
  expect(manager.tasks[0]).toMatchObject({ batchId: null, batchCleaned: true });
  const row = host.querySelector('.upload-task-row.error')!;
  expect(row.textContent).toContain("该失败项已清理，可重试");
  expect(row.textContent).toContain("bad_input");
  expect([...row.querySelectorAll('button')].map((button) => button.textContent)).toEqual(["重试"]);
  expect(host.textContent).toContain("event · #1：");
  expect(host.textContent).toContain("成功 1 项，失败 1 项");
  expect(mocks.cancelImportBatch).toHaveBeenCalledTimes(1);
  const queryCount = mocks.getImportBatch.mock.calls.length;
  await act(async () => row.querySelector<HTMLButtonElement>('button')!.click());
  expect(mocks.createImportBatch).toHaveBeenCalledTimes(3);
  expect(mocks.completeImportBatch.mock.calls.map((call) => call[1])).toEqual([91, 92, 93]);
  expect(mocks.getImportBatch).toHaveBeenCalledTimes(queryCount);
  expect(mocks.cancelImportBatch).toHaveBeenCalledTimes(1);
  expect(manager.tasks.map((task) => task.status)).toEqual(["success", "success"]);
});

it("清理失败保留batch及清理操作；重试前GET发现已成功则不删除、不创建新batch", async () => {
  mocks.cancelImportBatch.mockRejectedValueOnce(new ApiError(0, "清理断线"));
  await start(); await act(async () => manager.enqueueBatch(input));
  expect(manager.tasks[0]).toMatchObject({ status: "failed", batchId: 91 });
  expect(manager.tasks[0].batchCleaned).not.toBe(true);
  const row = host.querySelector('.upload-task-row.error')!;
  expect(row.textContent).toContain("清理待重试");
  expect(row.textContent).not.toContain("该失败项已清理");
  expect([...row.querySelectorAll('button')].map((button) => button.textContent)).toEqual(["重试", "取消并删除批次"]);
  mocks.getImportBatch.mockResolvedValue({ status: "ready", source: "heuristic" });
  await act(async () => manager.retry(manager.tasks[0].taskId));
  expect(manager.tasks[0].status).toBe("success");
  expect(mocks.createImportBatch).toHaveBeenCalledTimes(1);
  expect(mocks.cancelImportBatch).toHaveBeenCalledTimes(1);
});

it("依赖preflight失败保留实际code并指向管理员；无batch的移除仅影响本地任务", async () => {
  mocks.createImportBatch.mockRejectedValueOnce(new ApiError(503, "generic", { detail: { code: "import_dependency_unavailable", message: "ffprobe unavailable" } }));
  await start(); await act(async () => manager.enqueueBatch(input));
  expect(manager.tasks[0]).toMatchObject({ status: "failed", batchId: null });
  expect(host.textContent).toContain("import_dependency_unavailable");
  expect(host.textContent).toContain("请联系管理员修复环境后重试");
  expect(host.textContent).not.toContain("该失败项已清理");
  const remove = [...host.querySelectorAll('button')].find((button) => button.textContent === '移除本地任务')!;
  await act(async () => remove.click());
  expect(manager.tasks).toEqual([]);
  expect(mocks.getImportBatch).not.toHaveBeenCalled();
  expect(mocks.cancelImportBatch).not.toHaveBeenCalled();
});
