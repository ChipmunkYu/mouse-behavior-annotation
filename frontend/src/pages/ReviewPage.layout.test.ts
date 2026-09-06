// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import type { SubmissionAnnotationSnapshot } from "../api/types";
import reviewSource from "./ReviewPage.tsx?raw";
import { deriveReviewBehaviorSummary, nextReviewQueueOpen } from "./ReviewPage";

const annotations = [
  annotation(1, "攻击", [1, 2]),
  annotation(2, "追逐", [2, 3]),
];

describe("review workspace layout", () => {
  it("toggles the queue and closes it after selection or Escape", () => {
    expect(nextReviewQueueOpen(true, "toggle")).toBe(false);
    expect(nextReviewQueueOpen(false, "toggle")).toBe(true);
    expect(nextReviewQueueOpen(true, "select")).toBe(false);
    expect(nextReviewQueueOpen(true, "escape")).toBe(false);
  });

  it("derives no, single, overlap, and focused behavior summaries", () => {
    expect(deriveReviewBehaviorSummary(annotations, [], null).mode).toBe("none");

    const single = deriveReviewBehaviorSummary(annotations, [1], null);
    expect(single.mode).toBe("single");
    expect(single.categories).toEqual(["攻击"]);
    expect(single.mouseIds).toEqual([1, 2]);

    const overlap = deriveReviewBehaviorSummary(annotations, [1, 2], null);
    expect(overlap.mode).toBe("overlap");
    expect(overlap.categories).toEqual(["攻击", "追逐"]);
    expect(overlap.mouseIds).toEqual([1, 2, 3]);

    const focused = deriveReviewBehaviorSummary(annotations, [1, 2], 2);
    expect(focused.mode).toBe("focused");
    expect(focused.items.map((item) => item.id)).toEqual([2]);
    expect(focused.mouseIds).toEqual([2, 3]);
  });

  it("keeps queue and history accessibility contracts without media generation UI", () => {
    expect(reviewSource).toContain('role="tablist"');
    expect(reviewSource).toContain('aria-selected={queueOpen}');
    expect(reviewSource).not.toContain('aria-expanded={queueOpen}');
    expect(reviewSource).toContain('aria-controls="review-queue-panel"');
    expect(reviewSource).toContain('queueTabRef.current?.focus()');
    expect(reviewSource).toContain('<details className="review-history-details">');
    expect(reviewSource).not.toContain('<details className="review-history-details" open');
    expect(reviewSource).not.toContain("MediaStatusPanel");
    expect(reviewSource).not.toContain('title="视频片段生成"');
  });
});

function annotation(id: number, name: string, mouseIds: number[]): SubmissionAnnotationSnapshot {
  return {
    id,
    category_id: id,
    category_name: name,
    mouse_ids: mouseIds,
    participant_roles: {},
    role_definitions: [],
    category_participant_mode: "unordered",
    start_frame: 0,
    end_frame: 30,
    start_time: 0,
    end_time: 1,
  } as SubmissionAnnotationSnapshot;
}
