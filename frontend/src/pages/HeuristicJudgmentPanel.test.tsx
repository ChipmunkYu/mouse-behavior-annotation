// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Category, Judgment, Project, Video } from "../api/types";
import { ApiError } from "../api/client";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mocks = vi.hoisted(() => ({ getJudgment: vi.fn(), listCategories: vi.fn(), getCorrectedTracks: vi.fn(), getIdentityEditHistory: vi.fn(), putJudgment: vi.fn(), submitVideoForReview: vi.fn(), withdrawVideo: vi.fn(), checkIdentityEdit: vi.fn(), commitIdentityEdit: vi.fn(), revertIdentityEdit: vi.fn(), confirm: vi.fn(), overlay: vi.fn(), media: vi.fn(), getBehaviorReviewState: vi.fn(), putBehaviorDecision: vi.fn(), createVideoReview: vi.fn() }));
vi.mock("../api", () => mocks);
vi.mock("../media", () => ({ useMediaSource: (input: { videoId: number | null }) => { mocks.media(input); return { status: input.videoId == null ? "idle" : "ready", reload: vi.fn(), cancel: vi.fn() }; } }));
vi.mock("../components/ConfirmDialog", () => ({ useConfirm: () => [null, mocks.confirm] }));
vi.mock("../components/DetectionOverlay", () => ({ default: (props: unknown) => { mocks.overlay(props); return null; } }));
import HeuristicJudgmentPanel, { judgmentSelection, judgmentVersions } from "./HeuristicJudgmentPanel";

const unordered: Category = { id: 7, project_id: 1, group: "社交", name: "测试类别", sort_order: 1, is_active: true, participant_mode: "unordered", mouse_count_min: 1, mouse_count_max: 3, role_definitions: [] };
const roleBased: Category = { ...unordered, id: 8, name: "角色类别", participant_mode: "role_based", mouse_count_min: 2, role_definitions: [{ key: "role_project_actor", name: "发起鼠", min_count: 1, max_count: 1, role_sort_order: 0 }, { key: "role_project_target", name: "接受鼠", min_count: 1, max_count: 1, role_sort_order: 1 }] };
const video: Video = { id: 4, project_id: 1, filename: "clip.mp4", source: "heuristic", judgment_status: "pending", status: "ready", playback_status: "ready", created_at: "2026-11-09", annotation_revision: 1, detection_import_revision: 1, identity_revision: 0, workflow_status: "draft", submission_annotations: [], fps: 30 };
const project: Project = { id: 1, name: "测试项目", role: "member", membership_id: 1, can_review: true, created_at: "2026-11-09", status: "active", category_scheme_version: 1, category_scheme_locked_at: "2026-11-09" };
function pending(): Judgment { return { video_id: 4, source: "heuristic", workflow_status: "draft", judgment_status: "pending", annotation_revision: 1, detection_import_revision: 1, identity_revision: 0, input: { behavior: "测试类别", mouse_ids: [12, 31], participants: [{ actor_id: 12 }], frame_range: { start: 15, end: 74 }, time_range: { start: 0.5, end: 2.5 } }, selection: null, submission_id: null, decision_revision: 0 }; }
function reviewJudgment(): Judgment { return { ...pending(), workflow_status: "submitted", judgment_status: "usable", submission_id: 42, decision_revision: 3, selection: { annotation_id: 10, category_id: 7, mouse_ids: [12], participant_roles: {} } }; }
let root: Root;
let container: HTMLDivElement;

async function render(review = false, onChanged = vi.fn().mockResolvedValue(undefined)) {
  await act(async () => root.render(<RouterProvider router={createMemoryRouter([{ path: "/", element: <HeuristicJudgmentPanel projectId={1} video={video} project={project} review={review} onChanged={onChanged} /> }])} />));
}
async function click(text: string) {
  const button = [...container.querySelectorAll("button")].find((item) => item.textContent === text);
  expect(button, `button ${text}`).toBeDefined();
  await act(async () => button!.click());
}
async function selectCategory(id: number) {
  await act(async () => { const select = container.querySelector("select")!; select.value = String(id); select.dispatchEvent(new Event("change", { bubbles: true })); });
}
async function checkTrack(id: number, group = 0) {
  const slots = container.querySelectorAll<HTMLButtonElement>(".judgment-fields .role-slot");
  if (slots.length) await act(async () => slots[group].click());
  const button = [...container.querySelectorAll<HTMLButtonElement>(".judgment-fields .mouse-id-item")].find((item) => new RegExp(`(?:Track|track ID) ${id}$`).test(item.querySelector("b")!.textContent!))!;
  await act(async () => button.click());
}
async function feedback(value: string) {
  await act(async () => {
    const input = container.querySelector('textarea')!;
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  container = document.createElement("div"); document.body.appendChild(container); root = createRoot(container);
  mocks.getJudgment.mockResolvedValue(pending());
  mocks.listCategories.mockResolvedValue([unordered, roleBased]);
  mocks.getCorrectedTracks.mockResolvedValue({ items: [{ display_track_id: 12 }, { display_track_id: 31 }], pages: 1 });
  mocks.getIdentityEditHistory.mockResolvedValue([]);
  mocks.confirm.mockResolvedValue(true);
  mocks.getBehaviorReviewState.mockResolvedValue({ submission_id: 42, submission_status: "submitted", decision_revision: 3, annotations: [{ id: 10 }] });
  mocks.putBehaviorDecision.mockResolvedValue({ submission_id: 42, submission_status: "submitted", decision_revision: 4 });
  mocks.createVideoReview.mockImplementation(async (_pid, _vid, input) => { mocks.getJudgment.mockResolvedValue({ ...reviewJudgment(), workflow_status: input.result, decision_revision: 4 }); });
  mocks.submitVideoForReview.mockImplementation(async () => {
    const results = mocks.putJudgment.mock.results;
    const value = await results[results.length - 1]?.value;
    mocks.getJudgment.mockResolvedValue({ ...value, workflow_status: "submitted", submission_id: 42 });
  });
  mocks.putJudgment.mockImplementation(async (_pid, _vid, input) => ({ ...pending(), judgment_status: input.judgment_status, workflow_status: input.judgment_status === "unusable" ? "unusable" : "draft", annotation_revision: 2, selection: input.judgment_status === "unusable" ? null : { annotation_id: 10, category_id: input.category_id, mouse_ids: input.mouse_ids ?? [12, 31], participant_roles: input.participant_roles } }));
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });

describe("启发式判断面板", () => {
  it.each([false, true])("常驻摘要即时反映草稿、角色和空值，切换模式仍显示（复核=%s）", async (review) => {
    await render(review);
    const summary = container.querySelector('[aria-label="当前行为摘要"]')!;
    expect(summary.closest('.judgment-side-header')).not.toBeNull();
    expect(summary.closest('[role="tabpanel"]')).toBeNull();
    expect(summary.textContent).toBe('行为类别：未选择参与对象：未选择');
    expect(summary.textContent).not.toContain('Track 12'); // input中的原始建议不是草稿。
    await selectCategory(7); await checkTrack(12); await checkTrack(31);
    expect(summary.textContent).toBe('行为类别：测试类别参与对象：Track 12、Track 31');
    await checkTrack(12);
    expect(summary.textContent).toBe('行为类别：测试类别参与对象：Track 31');
    await click('Track 修正');
    expect(container.querySelector('[aria-label="当前行为摘要"]')).toBe(summary);
    expect(summary.textContent).toContain('Track 31');
    await click('行为信息'); await selectCategory(8);
    expect(summary.textContent).toBe('行为类别：角色类别发起鼠：未选择接受鼠：未选择');
    await checkTrack(31, 0); await checkTrack(12, 1);
    expect([...summary.querySelectorAll('.participant-summary-item')].map((item) => item.textContent)).toEqual(['发起鼠：Track 31', '接受鼠：Track 12']);
    await click('Track 修正');
    expect(summary.textContent).toBe('行为类别：角色类别发起鼠：Track 31接受鼠：Track 12');
    expect(mocks.putJudgment).not.toHaveBeenCalled();
  });

  it("默认右栏行为信息，模式互斥渲染且保留草稿；视频与处置按钮始终在左主区", async () => {
    await render();
    const side = container.querySelector('.annotate-side')!;
    const main = container.querySelector('.annotate-main')!;
    const videoElement = main.querySelector('video');
    expect(side.querySelector('[role="tablist"]')?.getAttribute('aria-label')).toBe('判断工作模式');
    expect(side.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe('行为信息');
    expect(side.querySelectorAll('[role="tabpanel"]')).toHaveLength(1);
    expect(side.querySelector('.judgment-fields')).not.toBeNull();
    expect(container.querySelector('.identity-panel')).toBeNull();
    expect(main.querySelector('select')).toBeNull();
    await selectCategory(7); await checkTrack(12);
    await click('Track 修正');
    expect(side.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe('Track 修正');
    expect(side.querySelectorAll('[role="tabpanel"]')).toHaveLength(1);
    expect(side.querySelector('.identity-panel')).not.toBeNull();
    expect(container.querySelector('.judgment-fields')).toBeNull();
    expect(main.querySelector('video')).toBe(videoElement);
    expect([...main.querySelectorAll('.judgment-player-controls button')].map((button) => button.textContent)).toEqual(['前一帧', '后一帧', '通过', '不可用']);
    expect(side.textContent).not.toContain('不可用');
    await click('行为信息');
    expect(container.querySelector('select')?.value).toBe('7');
    expect(container.querySelectorAll('.mouse-id-item.selected')).toHaveLength(1);
    expect(container.querySelector('.identity-panel')).toBeNull();
    const behaviorTab = side.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]')!;
    await act(async () => behaviorTab.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })));
    expect(side.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe('Track 修正');
    expect(document.activeElement?.textContent).toBe('Track 修正');
    expect(mocks.putJudgment).not.toHaveBeenCalled();
  });

  it("同名原建议也不选类别、参与鼠或角色；无旧快捷键和通用标注操作", async () => {
    await render();
    expect(container.querySelector("select")?.value).toBe("");
    expect(container.textContent).toContain("测试类别");
    expect(container.textContent).toContain("15–74");
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { code: "Digit1", key: "1" })); window.dispatchEvent(new KeyboardEvent("keydown", { code: "Delete", key: "Delete" })); });
    expect(container.querySelector("select")?.value).toBe("");
    expect(mocks.putJudgment).not.toHaveBeenCalled();
    expect(container.querySelector('input[type="number"]')).toBeNull();
    expect(container.textContent).not.toContain("新增行为");
    await selectCategory(8);
    expect(container.querySelector('.role-track-chip')).toBeNull();
    expect(container.textContent).not.toContain("原始建议");
    expect(container.querySelector("pre")).toBeNull();
    const overlay = mocks.overlay.mock.calls[mocks.overlay.mock.calls.length - 1][0];
    expect(overlay.video).toBe(container.querySelector("video"));
    expect(overlay.showOnlySelected).toBe(false);
    expect(overlay.selectedIds).toEqual([]);
    expect(mocks.media.mock.calls.some(([input]) => input.videoId === 4)).toBe(true);
    await checkTrack(31, 0); await checkTrack(12, 1);
    const pass = [...container.querySelectorAll("button")].find((button) => button.textContent === "通过")!;
    expect(pass.closest('.judgment-player-controls')).not.toBeNull();
    expect(pass.closest('.judgment-player-controls')?.textContent).toContain("前一帧");
    expect(pass.closest('.judgment-player-controls')?.textContent).toContain("后一帧");
    await click("通过");
    expect(mocks.putJudgment.mock.calls[0][2]).toEqual({ judgment_status: "usable", category_id: 8, participant_roles: { role_project_actor: [31], role_project_target: [12] }, ...judgmentVersions(pending()) });
    expect(mocks.putJudgment.mock.calls[0][2]).not.toHaveProperty("mouse_ids");
    expect(mocks.submitVideoForReview.mock.calls[0][2]).toMatchObject({ expected_annotation_revision: 2 });
    expect(container.textContent).toContain("已提交复核，尚未最终通过");
    expect([...container.querySelectorAll('button')].some((button) => button.textContent === '通过')).toBe(false);
  });

  it("首次判断可直接不可用，保留证据、无删除、无复核提交且不可恢复", async () => {
    await render();
    await click("不可用");
    expect(mocks.putJudgment.mock.calls[0][2]).toEqual({ judgment_status: "unusable", ...judgmentVersions(pending()) });
    expect(container.textContent).toContain("只读，不可重新打开或恢复");
    expect(container.textContent).not.toContain("提交复核");
    expect(container.querySelector("select")?.disabled).toBe(false); // fieldset 负责原生禁用
    expect(container.querySelector("select")?.closest("fieldset")?.disabled).toBe(true);
    expect(container.textContent).not.toContain("原始建议");
    expect(mocks.submitVideoForReview).not.toHaveBeenCalled();
  });

  it("复核同面板直接改角色，用当前快照 CAS 保存并触发父页刷新（允许自审）", async () => {
    const submitted: Judgment = { ...pending(), workflow_status: "submitted", judgment_status: "usable", submission_id: 42, decision_revision: 3, selection: { annotation_id: 10, category_id: 7, mouse_ids: [12], participant_roles: {} } };
    mocks.getJudgment.mockResolvedValue(submitted);
    mocks.putJudgment.mockResolvedValue({ ...submitted, submission_id: 43, decision_revision: 0, annotation_revision: 2 });
    const refreshed = vi.fn().mockResolvedValue(undefined);
    await render(true, refreshed);
    await selectCategory(8); await checkTrack(31, 0); await checkTrack(12, 1);
    await click('Track 修正');
    expect(container.querySelector('.annotate-side .identity-panel')).not.toBeNull();
    expect(container.querySelector('.judgment-fields')).toBeNull();
    expect([...container.querySelectorAll('.annotate-main button')].some((button) => button.textContent === '保存修改并刷新快照')).toBe(false);
    await click('行为信息');
    expect([...container.querySelectorAll('.annotate-side button')].some((button) => button.textContent === '保存修改并刷新快照')).toBe(true);
    expect(container.querySelector('.identity-panel')).toBeNull();
    await click("保存修改并刷新快照");
    expect(mocks.putJudgment.mock.calls[0][2]).toMatchObject({ expected_submission_id: 42, expected_decision_revision: 3, expected_annotation_revision: 1 });
    expect(refreshed).toHaveBeenCalledOnce();
    expect(container.textContent).toContain("已生成新快照，请重新裁决");
    expect(container.textContent).not.toContain("重新打开审核");
    expect([...container.querySelectorAll('.judgment-disposition-actions button')].map((button) => button.textContent)).toEqual(['通过', '不可用', '退回']);
    expect(container.querySelector('.annotate-side .judgment-disposition-actions')).toBeNull();
  });

  it("split 歧义先预览，显式确认后才提交，包含判断 CAS 和确认后的参与关系", async () => {
    const usable = { ...pending(), judgment_status: "usable" as const, selection: { annotation_id: 10, category_id: 7, mouse_ids: [12], participant_roles: {} } };
    mocks.getJudgment.mockResolvedValue(usable);
    mocks.checkIdentityEdit.mockResolvedValue({ operation: "split", new_display_track_id: 32, participant_confirmation_required: true, proposed_selection: null });
    await render();
    await click("Track 修正");
    await act(async () => container.querySelector<HTMLButtonElement>(".identity-track")!.click());
    await click("从当前帧 Split");
    expect(mocks.checkIdentityEdit.mock.calls[0][2]).toEqual({ operation: "split", track_ids: [12], frame: 15, ...judgmentVersions(usable) });
    expect(mocks.commitIdentityEdit).not.toHaveBeenCalled();
    expect([...container.querySelectorAll("button")].find((button) => button.textContent === "确认执行")?.disabled).toBe(true);
    const explicit = [...container.querySelectorAll("label")].find((label) => label.textContent?.includes("我已核对并确认"))!.querySelector("input")!;
    await act(async () => explicit.click());
    await click("确认执行");
    expect(mocks.commitIdentityEdit.mock.calls[0][2]).toMatchObject({ expected_annotation_revision: 1, confirmed_selection: judgmentSelection(unordered, [12], {}) });
  });

  it("已完成复核完全只读，不提供修改、ID执行或重新打开", async () => {
    mocks.getJudgment.mockResolvedValue({ ...pending(), workflow_status: "approved", judgment_status: "usable", submission_id: 43, selection: { annotation_id: 10, category_id: 7, mouse_ids: [12], participant_roles: {} } });
    await render(true);
    expect(container.querySelector("select")?.closest("fieldset")?.disabled).toBe(true);
    expect(container.textContent).toContain("只读，不可重新打开或恢复");
    expect(container.textContent).not.toContain("保存修改并刷新快照");
    expect(container.textContent).not.toContain("预览 Merge");
    expect(container.textContent).not.toContain("重新打开审核");
    expect([...container.querySelectorAll<HTMLButtonElement>('.judgment-disposition-actions button')].every((button) => button.disabled)).toBe(true);
  });

  it("保存冲突刷新CAS而保留选择，不自动重试或提交", async () => {
    const usable: Judgment = { ...pending(), judgment_status: "usable", annotation_revision: 5, selection: { annotation_id: 10, category_id: 7, mouse_ids: [12], participant_roles: {} } };
    mocks.getJudgment.mockResolvedValue(usable);
    mocks.putJudgment.mockRejectedValueOnce(new ApiError(409, "版本冲突"));
    await render();
    await checkTrack(31);
    await click("通过");
    expect(mocks.putJudgment).toHaveBeenCalledOnce();
    expect(container.textContent).toContain("未自动重试");
    expect(container.querySelectorAll('.mouse-id-item.selected')).toHaveLength(2);
    expect(mocks.submitVideoForReview).not.toHaveBeenCalled();
  });

  it("提交失败保留已保存选择，用保存返回的CAS提交，双击不重入", async () => {
    let resolve!: (value: Judgment) => void;
    mocks.putJudgment.mockImplementation(() => new Promise<Judgment>((done) => { resolve = done; }));
    mocks.submitVideoForReview.mockRejectedValue(new ApiError(500, "提交失败"));
    await render(); await selectCategory(7); await checkTrack(12);
    const button = [...container.querySelectorAll('button')].find((item) => item.textContent === '通过')!;
    await act(async () => { button.click(); button.click(); });
    expect(mocks.putJudgment).toHaveBeenCalledOnce();
    expect(button.disabled).toBe(true);
    const saved: Judgment = { ...pending(), judgment_status: "usable", annotation_revision: 9, selection: { annotation_id: 10, category_id: 7, mouse_ids: [12], participant_roles: {} } };
    await act(async () => resolve(saved));
    expect(mocks.submitVideoForReview).toHaveBeenCalledWith(1, 4, judgmentVersions(saved));
    expect(container.querySelectorAll('.mouse-id-item.selected')).toHaveLength(1);
    expect(container.textContent).toContain("提交失败");
    expect(button.disabled).toBe(false);
  });

  it("复用人工角色槽位的锁定、容量、移位确认和类别重选，不开放抑制", async () => {
    await render(); await selectCategory(8);
    expect(container.querySelectorAll<HTMLButtonElement>('.role-slot')[1].disabled).toBe(true);
    await checkTrack(12, 0); await checkTrack(31, 0);
    expect(container.textContent).toContain("已达到最多");
    await checkTrack(12, 1);
    expect(mocks.confirm).toHaveBeenCalledWith(expect.objectContaining({ confirmLabel: "移至当前角色" }));
    expect(container.querySelector('.role-track-chip')?.textContent).toContain("接受鼠");
    await selectCategory(7);
    expect(container.querySelector('.role-track-chip')).toBeNull();
    expect(container.querySelectorAll('.mouse-id-item.selected')).toHaveLength(0);
    expect(container.textContent).not.toContain("忽略整个 track");
    expect(container.textContent).not.toContain("Ctrl+Z");
  });

  it("merge冲突阻止确认与执行；无冲突也先预览再执行", async () => {
    mocks.checkIdentityEdit.mockResolvedValueOnce({ operation: "merge", conflict_frames: [2] }).mockResolvedValueOnce({ operation: "merge", retained_display_track_id: 12 });
    await render();
    await click("Track 修正");
    await act(async () => { container.querySelectorAll<HTMLButtonElement>('.identity-track').forEach((button) => button.click()); });
    await click("Merge 所选");
    expect(container.textContent).toContain("Merge 冲突");
    expect([...container.querySelectorAll("button")].some((button) => button.textContent === "确认执行")).toBe(false);
    expect(mocks.commitIdentityEdit).not.toHaveBeenCalled();
    await click("Merge 所选");
    expect(mocks.commitIdentityEdit).not.toHaveBeenCalled();
    await click("确认执行");
    expect(mocks.commitIdentityEdit).toHaveBeenCalledWith(1, 4, { operation: "merge", track_ids: [12, 31], ...judgmentVersions(pending()) });
  });

  it("复核唯一主区三按钮：通过先单条裁决再以返回CAS最终发布，不调用判断提交", async () => {
    mocks.getJudgment.mockResolvedValue(reviewJudgment());
    await render(true);
    const actions = container.querySelector('.annotate-main .player-controls [aria-label="启发式复核决策"]')!;
    expect([...actions.querySelectorAll('button')].map((button) => button.textContent)).toEqual(['通过', '不可用', '退回']);
    expect(container.querySelector('.annotate-side textarea')).toBeNull();
    expect(container.querySelector('.annotate-main textarea')).not.toBeNull();
    await click('通过');
    expect(mocks.confirm).toHaveBeenCalledWith(expect.objectContaining({ title: '确认最终通过？' }));
    expect(mocks.putBehaviorDecision).toHaveBeenCalledWith(1, 4, 42, 10, { status: 'approved', feedback: null, expected_decision_revision: 3 });
    expect(mocks.createVideoReview).toHaveBeenCalledWith(1, 4, { result: 'approved', comment: null, expected_submission_id: 42, expected_decision_revision: 4 });
    expect(mocks.putJudgment).not.toHaveBeenCalled();
    expect(mocks.submitVideoForReview).not.toHaveBeenCalled();
    expect(container.textContent).toContain('复核已最终通过');
  });

  it("退回空原因阻断，有原因才裁决并发布rejected", async () => {
    mocks.getJudgment.mockResolvedValue(reviewJudgment());
    await render(true);
    const buttons = [...container.querySelectorAll<HTMLButtonElement>('.judgment-disposition-actions button')];
    expect(buttons[1].disabled).toBe(false);
    expect(buttons[2].disabled).toBe(true);
    await click('退回');
    expect(mocks.putBehaviorDecision).not.toHaveBeenCalled();
    await feedback('   ');
    expect(buttons[2].disabled).toBe(true);
    await feedback('  参与鼠需要核对  ');
    await click('退回');
    expect(mocks.putBehaviorDecision).toHaveBeenCalledWith(1, 4, 42, 10, { status: 'rejected', feedback: '参与鼠需要核对', expected_decision_revision: 3 });
    expect(mocks.createVideoReview).toHaveBeenCalledWith(1, 4, { result: 'rejected', comment: '参与鼠需要核对', expected_submission_id: 42, expected_decision_revision: 4 });
    expect(mocks.withdrawVideo).not.toHaveBeenCalled();
    expect(mocks.putJudgment).not.toHaveBeenCalled();
  });

  it("复核不可用直接调用现有review API，带当前CAS且不撤回、不伪造单条退回", async () => {
    mocks.getJudgment.mockResolvedValue(reviewJudgment());
    await render(true); await click('不可用');
    expect(mocks.confirm).toHaveBeenCalledWith(expect.objectContaining({ confirmLabel: '标记不可用', danger: true }));
    expect(mocks.createVideoReview).toHaveBeenCalledWith(1, 4, { result: 'unusable', comment: null, expected_submission_id: 42, expected_decision_revision: 3 });
    expect(mocks.putBehaviorDecision).not.toHaveBeenCalled();
    expect(mocks.withdrawVideo).not.toHaveBeenCalled();
    expect(mocks.putJudgment).not.toHaveBeenCalled();
    expect(container.textContent).toContain('已标记不可用，不进入导出');
    expect([...container.querySelectorAll<HTMLButtonElement>('.judgment-disposition-actions button')].every((button) => button.disabled)).toBe(true);
  });

  it("复核快照变更只刷新不裁决；发布失败不假报最终成功且保留意见", async () => {
    mocks.getJudgment.mockResolvedValue(reviewJudgment());
    mocks.getBehaviorReviewState.mockResolvedValueOnce({ submission_id: 43, submission_status: 'submitted', decision_revision: 0, annotations: [{ id: 11 }] });
    await render(true); await feedback('原因'); await click('退回');
    expect(mocks.putBehaviorDecision).not.toHaveBeenCalled();
    expect(mocks.createVideoReview).not.toHaveBeenCalled();
    expect(container.querySelector('textarea')?.value).toBe('原因');
    await click('放弃未保存修改');
    mocks.createVideoReview.mockRejectedValueOnce(new ApiError(500, '发布失败'));
    await click('退回');
    expect(container.textContent).toContain('最终结果尚未确认');
    expect(container.textContent).not.toContain('已退回判断');
    expect(container.querySelector('textarea')?.value).toBe('原因');
  });

  it("复核确认期间防重入，确认取消不发裁决和发布请求", async () => {
    mocks.getJudgment.mockResolvedValue(reviewJudgment());
    let resolve!: (value: boolean) => void;
    mocks.confirm.mockImplementationOnce(() => new Promise<boolean>((done) => { resolve = done; }));
    await render(true);
    const button = [...container.querySelectorAll<HTMLButtonElement>('.judgment-disposition-actions button')][0];
    await act(async () => { button.click(); button.click(); });
    expect(mocks.confirm).toHaveBeenCalledOnce();
    expect(button.disabled).toBe(true);
    expect(mocks.getBehaviorReviewState).not.toHaveBeenCalled();
    await act(async () => resolve(false));
    expect(mocks.putBehaviorDecision).not.toHaveBeenCalled();
    expect(mocks.createVideoReview).not.toHaveBeenCalled();
    expect(button.disabled).toBe(false);
  });
});
