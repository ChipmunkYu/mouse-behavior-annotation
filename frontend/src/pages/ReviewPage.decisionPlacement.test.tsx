// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Project, Video } from "../api/types";

const mocks = vi.hoisted(() => ({ listProjects: vi.fn(), listReviewQueue: vi.fn(), listVideos: vi.fn(), listVideoReviews: vi.fn(), getBehaviorReviewState: vi.fn(), listCategories: vi.fn(), createVideoReview: vi.fn(), putBehaviorDecision: vi.fn(), reopenBehaviorReview: vi.fn() }));
vi.mock("../api", () => mocks);
vi.mock("../media", () => ({ useMediaSource: () => ({ status: "idle", reload: vi.fn(), cancel: vi.fn() }) }));
vi.mock("./HeuristicJudgmentPanel", () => ({ default: () => <div data-testid="heuristic-panel" /> }));
import ReviewPage from "./ReviewPage";

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
  mocks.listVideoReviews.mockResolvedValue([]);
  mocks.listCategories.mockResolvedValue([]);
  mocks.getBehaviorReviewState.mockResolvedValue({ submission_id: 42, submission_status: "submitted", decision_revision: 0, counts: { pending: 0, approved: 0, rejected: 0 }, annotations: [] });
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals(); });
async function render(source: "manual" | "heuristic") {
  mocks.listReviewQueue.mockResolvedValue([{ ...video, source }]); mocks.listVideos.mockResolvedValue([{ ...video, source }]);
  await act(async () => root.render(<MemoryRouter initialEntries={["/projects/1/review?video=4"]}><Routes><Route path="/projects/:projectId/review" element={<ReviewPage />} /></Routes></MemoryRouter>));
}

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
