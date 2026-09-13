import { describe, expect, it } from "vitest";
import type { BehaviorReviewFeedbackItem, Review } from "../api/types";
import {
  EMPTY_REJECTION_COMMENT,
  isFeedbackMarked,
  latestVisibleRejection,
  rejectionComment,
  resolveFeedbackSelection,
  resolveFeedbackTarget,
  sortFeedbackItems,
} from "./annotateReviewFeedback";

const reviews = [
  review(1, "rejected", "较早意见", "2026-09-01T10:00:00Z"),
  review(2, "rejected", "同一时刻较小 ID", "2026-09-02T10:00:00Z"),
  review(3, "rejected", "最新退回意见", "2026-09-02T10:00:00Z"),
];

describe("annotation rejection feedback", () => {
  it("selects the latest rejected review using id as a stable timestamp tie-breaker", () => {
    expect(latestVisibleRejection(reviews, "rejected")?.id).toBe(3);
  });

  it.each(["rejected", "draft"] as const)("shows feedback while workflow status is %s", (status) => {
    expect(latestVisibleRejection(reviews, status)?.comment).toBe("最新退回意见");
  });

  it.each(["submitted", "approved"] as const)("hides feedback while workflow status is %s", (status) => {
    expect(latestVisibleRejection(reviews, status)).toBeNull();
  });

  it("returns no feedback without a rejected review", () => {
    expect(latestVisibleRejection([], "rejected")).toBeNull();
    expect(latestVisibleRejection([review(1, "approved", null, "2026-09-01T10:00:00Z")], "draft")).toBeNull();
  });

  it("does not revive an older rejection after a later approval", () => {
    const history = [...reviews, review(4, "approved", "通过", "2026-09-03T10:00:00Z")];
    expect(latestVisibleRejection(history, "draft")).toBeNull();
  });

  it("uses an explicit fallback for an empty rejection comment", () => {
    expect(rejectionComment(review(1, "rejected", "   ", "2026-09-01T10:00:00Z"))).toBe(EMPTY_REJECTION_COMMENT);
  });
});

function review(id: number, result: "approved" | "rejected", comment: string | null, createdAt: string): Review {
  return { id, result, comment, created_at: createdAt } as Review;
}

function feedbackItem(
  submissionAnnotationId: number,
  overrides: Partial<BehaviorReviewFeedbackItem> = {},
): BehaviorReviewFeedbackItem {
  return {
    submission_annotation_id: submissionAnnotationId,
    comparison: "unchanged",
    baseline: { category_id: 1, category_name: "追逐", start_frame: 0, end_frame: 5, mouse_ids: [] },
    current: null,
    ...overrides,
  } as BehaviorReviewFeedbackItem;
}

describe("退回行为定位当前行为", () => {
  const annotations = [
    { id: 21, start_time: 4.5 },
    { id: 22, start_time: 9 },
  ];

  it("优先用 source_annotation_id 定位并给出跳转时间", () => {
    const item = feedbackItem(1, { source_annotation_id: 21, current: { id: 21, start_time: 4.5 } as never });
    expect(resolveFeedbackTarget(item, annotations)).toEqual({ annotationId: 21, seekTime: 4.5 });
  });

  it("source_annotation_id 缺失时回退到 current.id", () => {
    const item = feedbackItem(2, { source_annotation_id: null, current: { id: 22, start_time: 9 } as never });
    expect(resolveFeedbackTarget(item, annotations)).toEqual({ annotationId: 22, seekTime: 9 });
  });

  it("行为已删除时返回空目标", () => {
    const item = feedbackItem(3, { source_annotation_id: null, current: null });
    expect(resolveFeedbackTarget(item, annotations)).toEqual({ annotationId: null, seekTime: null });
  });

  it("已删除的退回行为即使残留 source_annotation_id 也不选中幽灵 ID", () => {
    const item = feedbackItem(5, { comparison: "deleted", source_annotation_id: 99, current: null });
    expect(resolveFeedbackTarget(item, annotations)).toEqual({ annotationId: null, seekTime: null });
  });

  it("当前列表缺少该 ID 时返回空目标，不选中幽灵 ID", () => {
    const item = feedbackItem(4, { source_annotation_id: 99, current: { id: 99, start_time: 12 } as never });
    expect(resolveFeedbackTarget(item, annotations)).toEqual({ annotationId: null, seekTime: null });
  });
});

describe("退回导航的选中替换", () => {
  it("有目标时用目标替换旧选中，只保留一个选中行为", () => {
    expect(resolveFeedbackSelection(21, 22)).toBe(22);
    expect(resolveFeedbackSelection(null, 22)).toBe(22);
  });

  it("连续定位不同退回行为始终只保留最后一次选中", () => {
    const afterFirst = resolveFeedbackSelection(21, 22);
    expect(resolveFeedbackSelection(afterFirst, 23)).toBe(23);
    expect(resolveFeedbackSelection(afterFirst, 22)).toBe(22);
  });

  it("目标已删除（null）时保留现有选中，不清空也不多选", () => {
    expect(resolveFeedbackSelection(21, null)).toBe(21);
    expect(resolveFeedbackSelection(null, null)).toBeNull();
  });
});

describe("退回行为标记已修改后的本地排序", () => {
  it("未标记在前、已标记在后，组内按标记时间再按稳定 ID", () => {
    const items = [
      feedbackItem(30, { marked: true, marked_at: "2026-09-03T10:00:00Z" }),
      feedbackItem(10, { marked: true, marked_at: "2026-09-01T10:00:00Z" }),
      feedbackItem(20),
    ];
    expect(sortFeedbackItems(items, new Set()).map((item) => item.submission_annotation_id)).toEqual([20, 10, 30]);
  });

  it("本地标记后无需刷新即重排，且不修改入参数组", () => {
    const items = [feedbackItem(3), feedbackItem(1), feedbackItem(2)];
    expect(sortFeedbackItems(items, new Set([1])).map((item) => item.submission_annotation_id)).toEqual([2, 3, 1]);
    expect(items.map((item) => item.submission_annotation_id)).toEqual([3, 1, 2]);
  });

  it("服务端已标记的条目无需本地状态也排在未标记之后", () => {
    const items = [feedbackItem(7, { marked: true, marked_at: "2026-09-01T00:00:00Z" }), feedbackItem(2)];
    expect(sortFeedbackItems(items, new Set()).map((item) => item.submission_annotation_id)).toEqual([2, 7]);
  });

  it("有效标记状态合并服务端与本地标记", () => {
    expect(isFeedbackMarked(feedbackItem(1, { marked: true }), new Set())).toBe(true);
    expect(isFeedbackMarked(feedbackItem(1), new Set([1]))).toBe(true);
    expect(isFeedbackMarked(feedbackItem(1), new Set())).toBe(false);
  });
});
