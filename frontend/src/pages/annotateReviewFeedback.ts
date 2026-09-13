import type { BehaviorReviewFeedbackItem, Review } from "../api/types";

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

/**
 * 退回行为对应到当前行为标注：优先 source_annotation_id（退回时行为在当前列表中的 ID），
 * 缺失时回退到 current.id；只有该 ID 确实存在于当前标注列表时才返回，
 * 否则返回空目标（已删除的行为不能选中幽灵 ID，由调用方给出提示）。
 */
export function resolveFeedbackTarget(
  item: BehaviorReviewFeedbackItem,
  annotations: Array<{ id: number; start_time: number }>,
): { annotationId: number | null; seekTime: number | null } {
  const sourceId = item.source_annotation_id ?? null;
  const currentId = item.current?.id ?? null;
  const match = annotations.find((annotation) => annotation.id === sourceId || annotation.id === currentId);
  return {
    annotationId: match?.id ?? null,
    seekTime: match?.start_time ?? null,
  };
}

/**
 * 退回导航的选中替换：目标存在时用目标替换任何旧选中，保证左侧行为列表与
 * 时间轴同时只有一个选中行为；目标已删除（null）时保留现有选中，由调用方提示。
 * 只计算「唯一选中 id」，不引入第二份选中状态。
 */
export function resolveFeedbackSelection(
  currentSelectedId: number | null,
  feedbackTargetId: number | null,
): number | null {
  return feedbackTargetId ?? currentSelectedId;
}

/** 有效标记状态 = 服务端已标记，或本次会话的乐观本地标记。 */
export function isFeedbackMarked(item: BehaviorReviewFeedbackItem, localMarked: ReadonlySet<number>): boolean {
  return localMarked.has(item.submission_annotation_id) || Boolean(item.marked);
}

/**
 * 「标记已修改」后的本地排序：未标记在前、已标记在后；
 * 组内先按标记时间（服务端与本地一致），再按 submission_annotation_id 保持稳定。
 */
export function sortFeedbackItems(
  items: BehaviorReviewFeedbackItem[],
  localMarked: ReadonlySet<number>,
): BehaviorReviewFeedbackItem[] {
  return [...items].sort((a, b) =>
    Number(isFeedbackMarked(a, localMarked)) - Number(isFeedbackMarked(b, localMarked))
    || markedTime(a) - markedTime(b)
    || a.submission_annotation_id - b.submission_annotation_id
  );
}

function markedTime(item: BehaviorReviewFeedbackItem): number {
  const value = item.marked_at;
  const timestamp = value == null ? NaN : Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : Number.NEGATIVE_INFINITY;
}
