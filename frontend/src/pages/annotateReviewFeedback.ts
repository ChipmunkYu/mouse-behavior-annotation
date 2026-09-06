import type { Review } from "../api/types";

export const EMPTY_REJECTION_COMMENT = "审核人未填写具体退回意见，请联系审核人确认修改要求。";

export function latestVisibleRejection(
  reviews: Review[],
  workflowStatus: string | undefined,
): Review | null {
  if (workflowStatus !== "rejected" && workflowStatus !== "draft") return null;
  const latest = [...reviews]
    .sort((a, b) => reviewTime(b.created_at) - reviewTime(a.created_at) || b.id - a.id)[0];
  return latest?.result === "rejected" ? latest : null;
}

export function rejectionComment(review: Review): string {
  return review.comment?.trim() || EMPTY_REJECTION_COMMENT;
}

function reviewTime(value: string): number {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : Number.NEGATIVE_INFINITY;
}
