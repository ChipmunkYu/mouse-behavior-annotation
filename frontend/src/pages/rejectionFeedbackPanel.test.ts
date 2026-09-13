import { beforeAll, describe, expect, it, vi } from "vitest";
import annotateSource from "./AnnotatePage.tsx?raw";

vi.mock("../api/client", () => ({ apiFetch: vi.fn() }));
import { apiFetch } from "../api/client";
import { markFeedbackModified } from "./feedbackMarkApi";

// Vitest 默认把 CSS 导入替换为空，这里直接读取源文件以校验响应式布局约束。
async function readGlobalCss(): Promise<string> {
  const fs = await import(/* @vite-ignore */ "node:" + "fs");
  return fs.readFileSync(new URL("../styles/global.css", import.meta.url), "utf8");
}

let globalCss = "";
beforeAll(async () => {
  globalCss = await readGlobalCss();
});

describe("退回意见 / 退回行为面板布局", () => {
  it("面板位于 annotate-main 内、播放器之前，右侧工作区不再承载", () => {
    const mainIndex = annotateSource.indexOf('className="annotate-main"');
    const panelIndex = annotateSource.indexOf("<RejectionFeedbackPanel");
    const playerIndex = annotateSource.indexOf('className="card player-card"');
    const sideIndex = annotateSource.indexOf('className="annotate-side"');
    expect(mainIndex).toBeGreaterThan(-1);
    expect(panelIndex).toBeGreaterThan(mainIndex);
    expect(playerIndex).toBeGreaterThan(panelIndex);
    expect(sideIndex).toBeGreaterThan(panelIndex);
    const sideBlock = annotateSource.slice(sideIndex, annotateSource.indexOf("</aside>"));
    expect(sideBlock).not.toContain("<RejectionFeedbackPanel");
  });

  it("固定尺寸：外层裁切、面板体内部滚动", () => {
    const panelRules = globalCss.split("}").filter((rule) => rule.includes(".rejection-feedback-panel"));
    expect(panelRules.length).toBeGreaterThan(0);
    expect(globalCss).toMatch(/\.rejection-feedback-panel\s*\{[^}]*height:\s*\d+px/);
    expect(globalCss).toMatch(/\.rejection-feedback-panel\s*\{[^}]*overflow:\s*hidden/);
    expect(globalCss).toMatch(/\.rejection-feedback-panel \.behavior-feedback-list\s*\{[^}]*overflow-y:\s*auto/);
  });

  it("不覆盖视频 / 时间轴 / 主控件，也不横向溢出", () => {
    expect(globalCss).toMatch(/\.rejection-feedback-panel\s*\{[^}]*width:\s*100%/);
    expect(globalCss).toMatch(/\.rejection-feedback-panel\s*\{[^}]*min-width:\s*0/);
    expect(globalCss).toMatch(/\.rejection-feedback-panel\s*\{[^}]*max-width:\s*100%/);
    const panelRules = globalCss.split("}").filter((rule) => rule.includes(".rejection-feedback-panel"));
    for (const rule of panelRules) {
      expect(rule).not.toMatch(/position:\s*(absolute|fixed)/);
      expect(rule).not.toMatch(/(?:^|[;{\s])width:\s*\d+px/);
    }
  });

  it("窄屏保持单列堆叠，面板仍受固定高度约束", () => {
    expect(globalCss).toMatch(/@media \(max-width: 960px\)[\s\S]*?\.annotate-body\s*\{\s*grid-template-columns:\s*1fr/);
  });

  it("退回行为点击与 Timeline 聚焦、标注选择保持同步接线", () => {
    expect(annotateSource).toContain("focusedAnnotationId={selectedAnnotationId}");
    expect(annotateSource).toContain("setSelectedAnnotationId(resolveFeedbackSelection(selectedAnnotationId, target.annotationId))");
    expect(annotateSource).toContain("resolveFeedbackTarget(item, annotations)");
    expect(annotateSource).toContain("标记已修改");
    expect(annotateSource).toContain("markFeedbackModified(pid, vid, submissionId, id)");
  });

  it("面板不再展示「意见始终对应退回时的版本」文案", () => {
    expect(annotateSource).not.toContain("意见始终对应退回时的版本");
  });

  it("退回导航复用唯一选中状态，不引入第二份选中状态", () => {
    const focusFn = annotateSource.slice(
      annotateSource.indexOf("function focusFeedbackAnnotation"),
      annotateSource.indexOf("async function handleMarkFeedbackModified"),
    );
    // 选中通过既有 selectedAnnotationId + 替换函数计算，且只在 null 目标时保留旧值。
    expect(focusFn).toContain("resolveFeedbackSelection(selectedAnnotationId, target.annotationId)");
    expect(focusFn).toContain("if (target.annotationId == null)");
    expect(focusFn).not.toContain("focusedFeedback");
    expect(focusFn).not.toContain("selectedFeedback");
  });

  it("面板只保留定位与标记两个操作，用原生按钮承载键盘可达性", () => {
    expect(annotateSource).toContain('className="behavior-feedback-select"');
    expect(annotateSource).toContain("onFocus(item)");
    expect(annotateSource).toContain("onMarkModified(item)");
    // 不再用自定义 role/onKeyDown 模拟条目按钮。
    expect(annotateSource).not.toContain("shouldActivateFeedbackItem");
    expect(annotateSource).not.toContain('role="button"');
    expect(annotateSource).not.toContain("tabIndex={0}");
  });

  it("点击已删除的退回行为给出提示，不选中幽灵 ID", () => {
    expect(annotateSource).toContain("if (target.annotationId == null)");
    expect(annotateSource).toContain("该退回行为已删除，无法定位到当前行为");
  });

  it("标记动作使用 rejected_submission_id ?? submission_id", () => {
    expect(annotateSource).toContain("behaviorReviewState?.rejected_submission_id ?? behaviorReviewState?.submission_id");
  });

  it("长退回意见在固定面板内自身滚动", () => {
    expect(globalCss).toMatch(/\.annotation-rejection\s*\{[^}]*height:\s*\d+px/);
    expect(globalCss).toMatch(/\.annotation-rejection p\s*\{[^}]*overflow-y:\s*auto/);
    expect(globalCss).toMatch(/\.annotation-rejection p\s*\{[^}]*overflow-wrap:\s*anywhere/);
  });

  it("标记已修改走窄接口：PUT feedback-mark", async () => {
    const mocked = apiFetch as unknown as { mockResolvedValue: (value: unknown) => void };
    mocked.mockResolvedValue({});
    await markFeedbackModified(1, 2, 33, 44);
    expect(apiFetch).toHaveBeenCalledWith(
      "/projects/1/videos/2/submissions/33/annotations/44/feedback-mark",
      { method: "PUT" },
    );
  });
});
