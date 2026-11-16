// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Project, Video } from "../api/types";

const mocks = vi.hoisted(() => ({ getVideo: vi.fn(), panel: vi.fn(), listProjects: vi.fn(), listReviewQueue: vi.fn(), listVideos: vi.fn(), listVideoReviews: vi.fn(), getBehaviorReviewState: vi.fn(), listCategories: vi.fn(), createVideoReview: vi.fn(), putBehaviorDecision: vi.fn(), reopenBehaviorReview: vi.fn() }));
vi.mock("../api", () => mocks);
vi.mock("../media", () => ({ useMediaSource: () => ({ status: "idle", reload: vi.fn(), cancel: vi.fn() }) }));
vi.mock("./HeuristicJudgmentPanel", () => ({ default: (props: unknown) => { mocks.panel(props); return <div data-testid="heuristic-panel" />; } }));
import ReviewPage from "./ReviewPage";
import AnnotatePage from "./AnnotatePage";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const project: Project = { id: 1, name: "project", role: "owner", membership_id: 1, can_review: true, status: "active", created_at: "2026-11-09", category_scheme_version: 1 };
const video: Video = { id: 4, project_id: 1, filename: "event.mp4", source: "heuristic", workflow_status: "submitted", annotation_revision: 1, detection_import_revision: 1, identity_revision: 0, status: "ready", playback_status: "ready", created_at: "2026-11-09", submitted_at: "2026-11-09", submission_annotations: [] };
let root: Root, host: HTMLDivElement;
beforeEach(() => {
  vi.resetAllMocks(); host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.stubGlobal("matchMedia", () => ({ matches: false }));
  mocks.listProjects.mockResolvedValue([project]);
  mocks.getVideo.mockResolvedValue(video);
  mocks.listVideoReviews.mockResolvedValue([]);
  mocks.listCategories.mockResolvedValue([]);
  mocks.getBehaviorReviewState.mockResolvedValue({ submission_id: 42, submission_status: "submitted", decision_revision: 0, counts: { pending: 0, approved: 0, rejected: 0 }, annotations: [] });
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals(); });
function Location() { const location = useLocation(); return <output data-testid="location">{location.pathname}{location.search}</output>; }
async function render(source: "manual" | "heuristic", hidden = false) {
  mocks.listReviewQueue.mockResolvedValue(hidden ? [] : [{ ...video, source }]); mocks.listVideos.mockResolvedValue(hidden ? [] : [{ ...video, source }]);
  await act(async () => root.render(<MemoryRouter initialEntries={["/projects/1/review?video=4&keep=1"]}><Location /><Routes><Route path="/projects/:projectId/review" element={<ReviewPage />} /></Routes></MemoryRouter>));
}

async function changed() { await act(async () => { await mocks.panel.mock.lastCall![0].onChanged(); }); }
const unusable: Video = { ...video, workflow_status: "unusable", judgment_status: "unusable" };

it("复核本次不可用后清空选中及video参数，不保留播放器或重新打开隐藏项", async () => {
  await render("heuristic");
  mocks.getVideo.mockResolvedValue(unusable);
  mocks.listReviewQueue.mockResolvedValue([]); mocks.listVideos.mockResolvedValue([]);
  const stateCalls = mocks.getBehaviorReviewState.mock.calls.length;
  await changed();
  expect(host.querySelector('[data-testid="heuristic-panel"]')).toBeNull();
  expect(host.querySelector('[data-testid="location"]')?.textContent).toBe("/projects/1/review?keep=1");
  expect(host.textContent).toContain("已标记不可用");
  expect(host.querySelector('[role="alert"]')).toBeNull();
  expect(mocks.getBehaviorReviewState).toHaveBeenCalledTimes(stateCalls);
});

it("不可用复核历史直链走单详情，不将隐藏项插回队列或历史列表", async () => {
  mocks.getVideo.mockResolvedValue(unusable);
  await render("heuristic", true);
  expect(mocks.getVideo).toHaveBeenCalledWith(1, 4);
  expect(host.querySelector('[data-testid="heuristic-panel"]')).not.toBeNull();
  expect(mocks.panel.mock.lastCall![0].video.workflow_status).toBe("unusable");
  await act(async () => [...host.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent?.trim() === "刷新")!.click());
  expect(host.querySelector('[data-testid="heuristic-panel"]')).not.toBeNull();
  expect(host.querySelector('.review-rail [data-queue-item]')).toBeNull();
  expect(host.querySelector('[data-testid="location"]')?.textContent).toContain("video=4");
  await act(async () => [...host.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find((button) => button.textContent === "查找历史/复核视频")!.click());
  expect(host.querySelector('.review-rail [data-queue-item]')).toBeNull();
});

it("复核正常保存刷新快照但保留当前视频", async () => {
  await render("heuristic");
  mocks.getVideo.mockResolvedValue({ ...video, annotation_revision: 2 });
  await changed();
  expect(mocks.panel.mock.lastCall![0].video.annotation_revision).toBe(2);
  expect(host.querySelector('[data-testid="location"]')?.textContent).toContain("video=4");
  expect(host.querySelector('[data-testid="heuristic-panel"]')).not.toBeNull();
});

async function renderAnnotate(value = video) {
  mocks.getVideo.mockResolvedValue(value);
  await act(async () => root.render(<MemoryRouter initialEntries={["/projects/1/videos/4/annotate"]}><Location /><Routes><Route path="/projects/:projectId/videos/:videoId/annotate" element={<AnnotatePage />} /><Route path="/projects/:projectId/videos" element={<div>视频库</div>} /></Routes></MemoryRouter>));
}

it("判断页历史直达仅用单详情加载，不依赖已隐藏的视频列表", async () => {
  await renderAnnotate(unusable);
  expect(mocks.getVideo).toHaveBeenCalledWith(1, 4);
  expect(mocks.listVideos).not.toHaveBeenCalled();
  expect(host.querySelector('[data-testid="heuristic-panel"]')).not.toBeNull();
  expect(host.querySelector('[data-testid="location"]')?.textContent).toContain("/4/annotate");
});

it("判断操作确认不可用后返回视频库", async () => {
  await renderAnnotate();
  mocks.getVideo.mockResolvedValue(unusable);
  await changed();
  expect(host.querySelector('[data-testid="location"]')?.textContent).toBe("/projects/1/videos");
  expect(host.querySelector('[data-testid="heuristic-panel"]')).toBeNull();
});

it("判断正常保存可用后刷新详情但不跳走", async () => {
  await renderAnnotate();
  mocks.getVideo.mockResolvedValue({ ...video, workflow_status: "draft", judgment_status: "usable", annotation_revision: 2 });
  await changed();
  expect(host.querySelector('[data-testid="location"]')?.textContent).toContain("/4/annotate");
  expect(mocks.panel.mock.lastCall![0].video.annotation_revision).toBe(2);
});

it("heuristic左栏仅任务导航/状态，不保留旧视频发布或第二套行为决定", async () => {
  await render("heuristic");
  expect(host.querySelector('[data-testid="heuristic-panel"]')).not.toBeNull();
  const rail = host.querySelector('.review-rail')!;
  expect(rail.textContent).toContain("任务状态");
  expect(rail.textContent).toContain("待审核队列");
  expect(rail.querySelector('.review-decision-panel')).toBeNull();
  expect(rail.querySelector('textarea')).toBeNull();
  expect(host.querySelector('.review-behaviors')).toBeNull();
  expect(host.textContent).not.toContain("发布审核结果");
  expect(host.textContent).not.toContain("通过此行为");
});

it("manual保持原左栏视频说明/发布审核结果与右侧逐条决定区", async () => {
  await render("manual");
  const rail = host.querySelector('.review-rail')!;
  expect(rail.querySelector('.review-decision-panel')).not.toBeNull();
  expect(rail.querySelector('#review-comment')).not.toBeNull();
  expect(rail.textContent).toContain("发布审核结果");
  expect(host.querySelector('.review-behaviors')).not.toBeNull();
  expect(host.querySelector('[data-testid="heuristic-panel"]')).toBeNull();
});
