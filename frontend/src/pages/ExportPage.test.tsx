// @vitest-environment jsdom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ExportJob, ExportPreparation, PreparedExportStatus } from "../api";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const api = vi.hoisted(() => ({
  getExportStatus: vi.fn(), createExport: vi.fn(), fetchExportDownload: vi.fn(),
  listCategories: vi.fn(), listClipCategories: vi.fn(), listProjects: vi.fn(),
}));
vi.mock("../api", () => api);
vi.mock("react-router-dom", () => ({
  useParams: () => ({ projectId: "2" }),
  Link: ({ children }: { children?: ReactNode }) => <span>{children}</span>,
}));
import ExportPage from "./ExportPage";

const base: PreparedExportStatus = {
  source: "manual", exportable_count: 7, ready_count: 7, missing_count: 0,
  missing_clips: [], latest_job: null,
};
function preparation(state: ExportPreparation["state"], counts = {}): ExportPreparation {
  return { state, total_count: 8, ready_count: 2, failed_count: 0, deferred_count: 0, ...counts };
}
// 故意允许不合契约的原始异常，验证旧服务也不会泄露内部信息。
function job(overrides: Partial<Omit<ExportJob, "error">> & { error?: string } = {}): ExportJob {
  return {
    id: 42, project_id: 2, type: "export", status: "running", progress: 35,
    created_at: "2026-11-18T12:00:00Z", ...overrides,
  } as ExportJob;
}
let root: Root;
let container: HTMLDivElement;
async function render(status: PreparedExportStatus = base) {
  api.getExportStatus.mockResolvedValue(status);
  await act(async () => { root.render(<ExportPage />); });
}
async function tick() {
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
}
function exportButton() {
  return Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "开始导出 ZIP")!;
}
beforeEach(() => {
  vi.useFakeTimers();
  vi.resetAllMocks();
  api.listProjects.mockResolvedValue([{ id: 2, name: "测试项目", role: "owner" }]);
  api.listCategories.mockResolvedValue([]);
  api.listClipCategories.mockResolvedValue([]);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => { root.unmount(); });
  container.remove();
  vi.useRealTimers();
});

describe("当前驻留素材与旧 API 兼容", () => {
  it.each([
    ["empty", "暂无待准备素材"], ["pending", "等待后台准备"],
    ["preparing", "正在后台准备"], ["partial", "部分素材已就绪"],
    ["failed", "素材准备失败"], ["ready", "当前素材已就绪"],
  ] as const)("展示 %s 且不阻止冷导出", async (state, label) => {
    await render({ ...base, preparation: preparation(state) });
    expect(container.textContent).toContain(label);
    expect(container.textContent).toContain("2 / 8");
    expect(container.textContent).toContain("缓存可能被回收");
    expect(container.textContent).toContain("不等同于视频片段数");
    expect(container.querySelector('[aria-label="导出就绪进度"]')).toBeNull();
    expect(exportButton().disabled).toBe(false);
  });

  it("独立显示待准备、容量延后和失败数量", async () => {
    await render({ ...base, preparation: preparation("partial", { failed_count: 1, deferred_count: 3 }) });
    const counts = container.querySelector("dl")!;
    expect(Array.from(counts.querySelectorAll("dd")).map((item) => item.textContent)).toEqual(["2 / 8", "2", "3", "1"]);
    expect(container.textContent).toContain("因缓存容量限制延后准备");
    expect(container.textContent).toContain("导出时会重试");
    await tick();
    expect(api.getExportStatus).toHaveBeenCalledTimes(1);
  });

  it.each([false, true])("0 ready partial 不轮询且可导出（从 preparing 转入：%s）", async (fromPreparing) => {
    const status = { ...base, preparation: preparation("partial", { ready_count: 0 }) };
    await render(fromPreparing ? { ...base, preparation: preparation("preparing") } : status);
    if (fromPreparing) {
      api.getExportStatus.mockResolvedValue(status);
      await tick();
    }
    expect(container.textContent).toContain("素材将在导出时准备");
    expect(container.textContent).not.toContain("部分素材已就绪");
    expect(container.textContent).toContain("0 / 8");
    expect(exportButton().disabled).toBe(false);
    const calls = api.getExportStatus.mock.calls.length;
    await tick();
    await tick();
    expect(api.getExportStatus).toHaveBeenCalledTimes(calls);
    api.createExport.mockResolvedValue(job({ status: "queued" }));
    api.getExportStatus.mockResolvedValue({ ...status, latest_job: job() });
    await act(async () => { exportButton().click(); });
    expect(api.createExport).toHaveBeenCalledWith(2, { source: "manual", category_ids: undefined });
  });

  it("旧响应不假称已预热，不启动空闲轮询，仍可导出", async () => {
    await render();
    expect(container.textContent).toContain("当前服务未提供素材准备状态");
    expect(exportButton().disabled).toBe(false);
    await tick();
    expect(api.getExportStatus).toHaveBeenCalledTimes(1);
  });

  it("预热期间点击确实创建导出任务，保留来源与类别语义", async () => {
    const status = { ...base, preparation: preparation("preparing") };
    await render(status);
    api.createExport.mockResolvedValue(job({ status: "queued" }));
    api.getExportStatus.mockResolvedValue({ ...status, latest_job: job() });
    await act(async () => { exportButton().click(); });
    expect(api.createExport).toHaveBeenCalledWith(2, { source: "manual", category_ids: undefined });
    expect(container.textContent).toContain("导出任务已发起");
  });

  it("预览仅显示每独立片段四文件，不展示内部缓存 manifest", async () => {
    await render();
    const tree = container.querySelector("pre")!.textContent;
    for (const name of ["clip.mp4", "annotation.json", "tracks.json", "metadata.json"]) expect(tree).toContain(name);
    for (const old of ["annotations.json", "clips/", "corrected_tracks/", "README", "manifest"]) expect(tree).not.toContain(old);
  });
});

describe("轮询生命周期", () => {
  it("pending → preparing → ready 每四秒更新，就绪后停止", async () => {
    await render({ ...base, preparation: preparation("pending") });
    api.getExportStatus.mockResolvedValueOnce({ ...base, preparation: preparation("preparing") });
    await tick();
    expect(container.textContent).toContain("正在后台准备");
    api.getExportStatus.mockResolvedValue({ ...base, preparation: preparation("ready") });
    await tick();
    await tick();
    expect(api.getExportStatus).toHaveBeenCalledTimes(3);
  });

  it.each(["empty", "partial", "failed"] as const)("preparing → %s 后停止", async (state) => {
    await render({ ...base, preparation: preparation("preparing") });
    api.getExportStatus.mockResolvedValue({ ...base, preparation: preparation(state) });
    await tick();
    await tick();
    expect(api.getExportStatus).toHaveBeenCalledTimes(2);
  });

  it("缓存就绪但导出任务仍活动时继续，任务完成才停止", async () => {
    await render({ ...base, preparation: preparation("ready"), latest_job: job({ phase: "validating" }) });
    await tick();
    expect(api.getExportStatus).toHaveBeenCalledTimes(2);
    api.getExportStatus.mockResolvedValue({ ...base, preparation: preparation("ready"), latest_job: job({ status: "succeeded", phase: "completed" }) });
    await tick();
    await tick();
    expect(api.getExportStatus).toHaveBeenCalledTimes(3);
    expect(container.textContent).toContain("下载导出 ZIP");
  });

  it("卸载取消轮询", async () => {
    await render({ ...base, preparation: preparation("preparing") });
    await act(async () => { root.unmount(); });
    root = createRoot(container);
    await tick();
    expect(api.getExportStatus).toHaveBeenCalledTimes(1);
  });
});

describe("导出阶段与安全错误", () => {
  it.each([
    ["queued", "等待导出"], ["building", "准备素材与打包"],
    ["validating", "校验压缩包"], ["publishing", "发布导出包"], ["cleanup", "清理临时文件"],
  ] as const)("展示真实阶段 %s 和既有任务进度", async (phase, label) => {
    await render({ ...base, latest_job: job({ phase }) });
    expect(container.textContent).toContain(label);
    expect(container.querySelector('[aria-label="导出任务进度"]')?.getAttribute("aria-valuenow")).toBe("35");
  });

  it("旧任务没有 phase 仍显示状态和进度", async () => {
    await render({ ...base, latest_job: job() });
    expect(container.textContent).toContain("导出处理中");
    expect(container.textContent).toContain("35%");
  });

  it.each([
    ["storage_insufficient", "存储空间不足"],
    ["cleanup_incomplete", "临时文件清理未完成"],
    ["processing_failed", "处理未完成"], [null, "处理未完成"],
  ] as const)("失败 %s 使用固定中文，不泄露内部错误", async (error_code, label) => {
    await render({ ...base, latest_job: job({ status: "failed", phase: "failed", error_code, error: "secret /srv/internal/path traceback" }) });
    expect(container.textContent).toContain(label);
    expect(container.textContent).toContain("#42");
    expect(container.textContent).not.toContain("secret");
    await tick();
    expect(api.getExportStatus).toHaveBeenCalledTimes(1);
  });

  it("取消任务不回显异常", async () => {
    await render({ ...base, latest_job: job({ status: "cancelled", phase: "cancelled", error: "internal secret" }) });
    expect(container.textContent).toContain("导出已取消");
    expect(container.textContent).not.toContain("secret");
  });
});
