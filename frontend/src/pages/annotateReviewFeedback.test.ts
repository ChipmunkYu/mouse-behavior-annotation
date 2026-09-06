import { describe, expect, it } from "vitest";
import type { Review } from "../api/types";
import { EMPTY_REJECTION_COMMENT, latestVisibleRejection, rejectionComment } from "./annotateReviewFeedback";

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
