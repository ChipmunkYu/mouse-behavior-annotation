// @vitest-environment jsdom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { beforeEach, describe, expect, it, vi } from "vitest";
import pageSource from "./ProjectManagementPage.tsx?raw";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const apiMocks = vi.hoisted(() => ({
  listProjects: vi.fn(),
  listMembers: vi.fn(),
  getAssignmentStats: vi.fn(),
  getBehaviorStats: vi.fn(),
  getProjectInvite: vi.fn(),
  getCategoryScheme: vi.fn(),
  listCategorySchemeAudit: vi.fn(),
  lockCategoryScheme: vi.fn(),
  putCategoryScheme: vi.fn(),
  removeMember: vi.fn(),
  resetProjectInvite: vi.fn(),
  updateMember: vi.fn(),
}));

vi.mock("../api", () => ({
  listProjects: apiMocks.listProjects,
  listMembers: apiMocks.listMembers,
  getAssignmentStats: apiMocks.getAssignmentStats,
  getBehaviorStats: apiMocks.getBehaviorStats,
  getProjectInvite: apiMocks.getProjectInvite,
  getCategoryScheme: apiMocks.getCategoryScheme,
  listCategorySchemeAudit: apiMocks.listCategorySchemeAudit,
  lockCategoryScheme: apiMocks.lockCategoryScheme,
  putCategoryScheme: apiMocks.putCategoryScheme,
  removeMember: apiMocks.removeMember,
  resetProjectInvite: apiMocks.resetProjectInvite,
  updateMember: apiMocks.updateMember,
}));

vi.mock("../components/ConfirmDialog", () => ({ useConfirm: () => [null, vi.fn()] }));

vi.mock("react-router-dom", () => ({
  useParams: () => ({ projectId: "1" }),
  Link: ({ children }: { children?: ReactNode }) => <span>{children}</span>,
}));

import { BehaviorStatsTable } from "./ProjectManagementPage";
import ProjectManagementPage from "./ProjectManagementPage";
import { CollapsibleCard } from "../components/ui";

const behaviorData = {
  items: [
    { category_id: 2, category_name: "理毛", category_group: "社会行为", approved: 3, pending: 1, rejected: 0, possible_total: 4 },
    { category_id: 7, category_name: "未出现行为", category_group: "其他", approved: 0, pending: 0, rejected: 0, possible_total: 0 },
  ],
};

const assignmentStats = { total: 10, draft: 3, submitted: 2, approved: 4, rejected: 1, unassigned: 2, claimable: 1, by_assignee: [] };

beforeEach(() => {
  document.body.innerHTML = "";
  localStorage.clear();
  vi.clearAllMocks();
  apiMocks.listProjects.mockResolvedValue([{ id: 1, name: "测试项目", role: "admin", membership_id: 1, category_scheme_locked_at: null, category_scheme_version: 1 }]);
  apiMocks.listMembers.mockResolvedValue([]);
  apiMocks.getProjectInvite.mockResolvedValue({ invite_code: "INVITE" });
  apiMocks.getAssignmentStats.mockResolvedValue(assignmentStats);
  apiMocks.getBehaviorStats.mockResolvedValue(behaviorData);
});

function renderPage(): { container: HTMLElement; root: Root } {
  const container = document.createElement("div");
  document.body.appendChild(container);
  return { container, root: createRoot(container) };
}

async function flush() {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}

describe("ProjectManagementPage 头部", () => {
  it("删除管理范围副标题但保留页面标题", () => {
    expect(pageSource).not.toContain("管理类别方案、成员权限、邀请码与责任分配概况");
    expect(pageSource).toContain("<h1>项目管理</h1>");
  });
});

describe("管理统计表", () => {
  it("行为统计表渲染列头、行值与零值类别", async () => {
    const { container, root } = renderPage();
    await act(async () => { root.render(<BehaviorStatsTable data={behaviorData} />); });
    expect(Array.from(container.querySelectorAll("th")).map((th) => th.textContent)).toEqual(["行为", "已通过", "待审核", "退回", "可能总数"]);
    const rows = container.querySelectorAll("tbody tr");
    expect(rows).toHaveLength(2);
    expect(Array.from(rows[0].querySelectorAll("td")).map((td) => td.textContent)).toEqual(["理毛社会行为", "3", "1", "0", "4"]);
    expect(Array.from(rows[1].querySelectorAll("td")).map((td) => td.textContent)).toEqual(["未出现行为其他", "0", "0", "0", "0"]);
    await act(async () => { root.unmount(); });
  });

  it("不再渲染视频状态统计表", () => {
    expect(pageSource).not.toContain("VideoStatusTable");
    expect(pageSource).not.toContain("视频状态统计表");
  });
});

describe("可折叠卡片", () => {
  it("切换按钮改变 aria-expanded 并隐藏内容，且记住状态", async () => {
    const first = renderPage();
    await act(async () => { first.root.render(<CollapsibleCard id="test-section" title="测试区"><p>内容</p></CollapsibleCard>); });
    const button = first.container.querySelector("button.card-toggle")!;
    expect(button.getAttribute("aria-expanded")).toBe("true");
    const body = first.container.querySelector(`#${button.getAttribute("aria-controls")}`)!;
    expect(body.hasAttribute("hidden")).toBe(false);
    expect(body.textContent).toContain("内容");

    await act(async () => { button.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(body.hasAttribute("hidden")).toBe(true);
    await act(async () => { first.root.unmount(); });

    const second = renderPage();
    await act(async () => { second.root.render(<CollapsibleCard id="test-section" title="测试区"><p>内容</p></CollapsibleCard>); });
    expect(second.container.querySelector("button.card-toggle")!.getAttribute("aria-expanded")).toBe("false");
    await act(async () => { second.root.unmount(); });
  });
});

describe("统计刷新", () => {
  it("所有管理区块都渲染可折叠开关", async () => {
    apiMocks.listProjects.mockResolvedValue([{ id: 1, name: "测试项目", role: "owner", membership_id: 1, category_scheme_locked_at: null, category_scheme_version: 1 }]);
    apiMocks.getCategoryScheme.mockResolvedValue({ id: 1, project_id: 1, category_scheme_version: 1, category_scheme_locked_at: null, categories: [] });
    apiMocks.listCategorySchemeAudit.mockResolvedValue([]);
    const { container, root } = renderPage();
    await act(async () => { root.render(<ProjectManagementPage />); });
    await flush();
    const titles = Array.from(container.querySelectorAll(".card-toggle .card-title")).map((el) => el.textContent);
    expect(titles).toEqual(["行为统计表", "项目邀请码", "成员（0）", "类别方案", "方案历史"]);
    expect(titles.indexOf("行为统计表")).toBeLessThan(titles.indexOf("项目邀请码"));
    expect(titles.indexOf("项目邀请码")).toBeLessThan(titles.indexOf("成员（0）"));
    expect(titles.indexOf("成员（0）")).toBeLessThan(titles.indexOf("类别方案"));
    expect(titles.indexOf("类别方案")).toBeLessThan(titles.indexOf("方案历史"));
    expect(container.querySelectorAll("button.card-toggle")).toHaveLength(5);
    expect(container.querySelector(".video-status-table")).toBeNull();
    expect(container.querySelector(".behavior-stats-table")).toBeTruthy();
    expect(container.querySelector(".stats-strip")).toBeTruthy();
    await act(async () => { root.unmount(); });
  });

  it("刷新按钮重新拉取两类统计，且没有定时轮询", async () => {
    const { container, root } = renderPage();
    await act(async () => { root.render(<ProjectManagementPage />); });
    await flush();
    expect(apiMocks.getBehaviorStats).toHaveBeenCalledTimes(1);
    expect(apiMocks.getAssignmentStats).toHaveBeenCalledTimes(1);

    const refresh = Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.trim() === "刷新");
    expect(refresh).toBeTruthy();
    await act(async () => { refresh!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    await flush();
    expect(apiMocks.getBehaviorStats).toHaveBeenCalledTimes(2);
    expect(apiMocks.getAssignmentStats).toHaveBeenCalledTimes(2);

    expect(pageSource).not.toContain("setInterval");
    expect(pageSource).toContain("visibilitychange");
    expect(pageSource).toContain('"focus"');
    await act(async () => { root.unmount(); });
  });

  it("时间戳按北京时间显示，并在刷新后更新", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.setSystemTime(new Date("2026-09-13T16:12:00Z")); // 北京时间为 2026-09-14 00:12
      const { container, root } = renderPage();
      await act(async () => { root.render(<ProjectManagementPage />); });
      await flush();
      const label = () => container.querySelector(".stats-fetched-at")?.textContent ?? "";
      expect(label()).toBe("当前结果统计截至 2026-09-14 00:12");
      expect(label()).toMatch(/^当前结果统计截至 \d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);

      vi.setSystemTime(new Date("2026-09-13T16:13:00Z")); // 北京时间为 2026-09-14 00:13
      const refresh = Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.trim() === "刷新");
      await act(async () => { refresh!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
      await flush();
      expect(label()).toBe("当前结果统计截至 2026-09-14 00:13");

      await act(async () => { root.unmount(); });
    } finally {
      vi.useRealTimers();
    }
  });

  it("行为统计失败只影响该区块并显示错误态", async () => {
    apiMocks.getBehaviorStats.mockRejectedValue(new Error("行为统计不可用"));
    const { container, root } = renderPage();
    await act(async () => { root.render(<ProjectManagementPage />); });
    await flush();
    expect(container.querySelector(".error-box")?.textContent).toContain("行为统计加载失败");
    expect(container.textContent).not.toContain("视频状态统计表");
    expect(container.querySelector(".stats-strip")).toBeTruthy();
    await act(async () => { root.unmount(); });
  });
});
