// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import reviewSource from "./ReviewPage.tsx?raw";
import { nextReviewQueueOpen, resolveReviewSelection, scheduleBehaviorNoticeDismiss, sortReviewAnnotations } from "./ReviewPage";

describe("review workspace layout", () => {
  it("toggles the queue and closes it after selection or Escape", () => {
    expect(nextReviewQueueOpen(true, "toggle")).toBe(false);
    expect(nextReviewQueueOpen(false, "toggle")).toBe(true);
    expect(nextReviewQueueOpen(true, "select")).toBe(false);
    expect(nextReviewQueueOpen(true, "escape")).toBe(false);
  });

  it("keeps queue/history navigation and removes the duplicate header selector", () => {
    expect(reviewSource).toContain('role="tablist"');
    expect(reviewSource).toContain('aria-selected={railTab === "queue"}');
    expect(reviewSource).toContain("待审核队列");
    expect(reviewSource).toContain("查找历史/复核视频");
    expect(reviewSource).toContain('listVideos(pid, { view: "all" })');
    expect(reviewSource).toContain(".filter(isReviewRelevantVideo)");
    expect(reviewSource).toContain('video.submitted_at != null');
    expect(reviewSource).not.toContain('videos.filter((video) => video.workflow_status !== "draft")');
    expect(reviewSource).not.toContain("review-video-selector");
    expect(reviewSource).not.toContain('aria-expanded={queueOpen}');
    expect(reviewSource).toContain('aria-controls="review-queue-panel"');
    expect(reviewSource).toContain('queueTabRef.current?.focus()');
    expect(reviewSource).toContain('<details className="review-history-details">');
    expect(reviewSource).not.toContain('<details className="review-history-details" open');
    expect(reviewSource).not.toContain("MediaStatusPanel");
    expect(reviewSource).not.toContain('title="视频片段生成"');
  });

  it("places a non-overlaying, independently scrolling behavior list to the right", () => {
    expect(reviewSource.indexOf('className="review-main"')).toBeLessThan(reviewSource.indexOf('className="review-behaviors"'));
    expect(reviewSource).toContain('className="review-nav-panel"');
    expect(reviewSource).toContain('className="review-behaviors" aria-label="行为审核列表"');
    expect(reviewSource).toContain("保存失败 · 此意见尚未保存");
    expect(reviewSource).not.toContain("当前时刻无行为");
    expect(reviewSource).not.toContain("CurrentBehaviorSummary");
    expect(reviewSource).not.toContain("review-current-behavior");
  });

  it("stably orders pending, rejected, then approved behaviors", () => {
    const rows = [
      { id: 1, decision: { status: "approved" } },
      { id: 2, decision: { status: "pending" } },
      { id: 3, decision: { status: "rejected" } },
      { id: 4, decision: { status: "approved" } },
      { id: 5, decision: { status: "pending" } },
    ];
    expect(sortReviewAnnotations(rows).map((row) => row.id)).toEqual([2, 5, 3, 1, 4]);
    expect(rows.map((row) => row.id)).toEqual([1, 2, 3, 4, 5]);
  });

  it("keeps a processed card selected independently from the current video overlay", () => {
    const processed = [{ id: 7, decision: { status: "rejected" } }, { id: 8, decision: { status: "approved" } }];
    expect(resolveReviewSelection(processed, 7)).toBe(7);
    expect(resolveReviewSelection(processed, 8)).toBe(8);
    expect(resolveReviewSelection(processed, 9)).toBeNull();
    const behaviorList = reviewSource.slice(reviewSource.indexOf("<ReadOnlyAnnotationList"), reviewSource.indexOf("<ReadOnlyAnnotationList") + 1200);
    expect(behaviorList).toContain("focusedAnnotationId={selectedBehaviorId}");
    expect(behaviorList).not.toContain("focusedAnnotationId={overlayState.focusedAnnotationId}");
    expect(reviewSource).not.toContain("focusedAnnotationId != null && overlayState.focusedAnnotationId == null");
  });

  it("shows grouped actions only for the selected behavior without a redundant help row", () => {
    const editor = reviewSource.slice(reviewSource.indexOf('className="behavior-decision-editor"'), reviewSource.indexOf('className="behavior-decision-meta"'));
    expect(editor.match(/<textarea/g)).toHaveLength(1);
    expect(reviewSource).toContain('{focused ? <div id={`behavior-editor-${a.id}`} className="behavior-decision-editor">');
    expect(editor.indexOf("退回意见")).toBeLessThan(editor.indexOf('className="behavior-decision-primary"'));
    expect(editor).toContain("退回时必填");
    expect(editor).toContain('disabled={!decisionOpen || !rejectionReady}');
    expect(editor).toContain("退回此行为");
    expect(editor).toContain("通过此行为");
    expect(editor).toContain("撤销裁决");
    expect(editor).toContain("behavior-decision-restriction");
    expect(reviewSource).not.toContain("通过可直接保存；退回须先填写上方意见。");
    expect(reviewSource).not.toContain("behavior-feedback-hint");
  });

  it("closes a successfully decided editor and restores focus to the compact card", () => {
    expect(reviewSource).toContain("if (shouldClearBehaviorFocus(status))");
    expect(reviewSource).toContain("setFocusedAnnotationId(null)");
    expect(reviewSource).toContain('data-review-annotation-id={a.id}');
    expect(reviewSource).toContain('.review-annotation-focus`)?.focus()');
  });

  it("dismisses only the latest per-behavior success notice after two seconds", () => {
    vi.useFakeTimers();
    try {
      const dismissed: string[] = [];
      const cancelFirst = scheduleBehaviorNoticeDismiss("此行为已通过", (message) => dismissed.push(message));
      cancelFirst();
      scheduleBehaviorNoticeDismiss("此行为已退回，意见已保存", (message) => dismissed.push(message));
      vi.advanceTimersByTime(1999);
      expect(dismissed).toEqual([]);
      vi.advanceTimersByTime(1);
      expect(dismissed).toEqual(["此行为已退回，意见已保存"]);
    } finally {
      vi.useRealTimers();
    }
    expect(reviewSource).toContain("clearBehaviorNoticeTimer();");
    expect(reviewSource).toContain("current === dismissed ? null : current");
  });
});
