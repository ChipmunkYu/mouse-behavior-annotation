import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import { rejectedSubmitBlockerMessage, REVIEW_COMPARISON_LABELS } from "./AnnotatePage";
import { behaviorDecisionAccess, behaviorDecisionRestriction, reviewSubmitBlockers, shouldClearBehaviorFocus } from "./ReviewPage";

describe("behavior review errors", () => {
  const error = new ApiError(409, "conflict", {
    detail: {
      code: "rejected_annotations_not_addressed",
      items: [
        { submission_annotation_id: 10, source_annotation_id: 21, comparison: "unchanged" },
        { submission_annotation_id: 11, source_annotation_id: 22, comparison: "reverted" },
      ],
    },
  });

  it("identifies every blocked behavior on resubmit", () => {
    expect(rejectedSubmitBlockerMessage(error)).toBe("以下退回行为尚未处理，不能重新提交：标注 #21（未修改）、标注 #22（改后又还原）");
  });

  it("extracts blocker ids for the review page", () => {
    expect(reviewSubmitBlockers(error)).toEqual([21, 22]);
  });

  it("does not describe a modification as approval", () => {
    expect(REVIEW_COMPARISON_LABELS.modified).toBe("已修改");
  });

  it("only allows new decisions while submitted and permits revoke after rejection or withdrawal", () => {
    expect(behaviorDecisionAccess("submitted", false)).toEqual({ decisionOpen: true, revokeOpen: false });
    expect(behaviorDecisionAccess("rejected", false)).toEqual({ decisionOpen: false, revokeOpen: true });
    expect(behaviorDecisionAccess("withdrawn", false)).toEqual({ decisionOpen: false, revokeOpen: true });
    expect(behaviorDecisionAccess("approved", false)).toEqual({ decisionOpen: false, revokeOpen: false });
    expect(behaviorDecisionAccess("submitted", true)).toEqual({ decisionOpen: false, revokeOpen: false });
  });

  it("explains legal processed-decision options and clears focus only after approve/reject", () => {
    expect(behaviorDecisionRestriction("submitted")).toBeNull();
    expect(behaviorDecisionRestriction("rejected")).toContain("只能撤销已有裁决");
    expect(behaviorDecisionRestriction("withdrawn")).toContain("只能撤销已有裁决");
    expect(behaviorDecisionRestriction("approved")).toContain("先在左侧重新打开审核");
    expect(shouldClearBehaviorFocus("approved")).toBe(true);
    expect(shouldClearBehaviorFocus("rejected")).toBe(true);
    expect(shouldClearBehaviorFocus("pending")).toBe(false);
  });

  it("accepts a structured detail supplied directly by the transport", () => {
    const direct = new ApiError(409, "conflict", { code: "rejected_annotations_not_addressed", items: [{ source_annotation_id: null, submission_annotation_id: 31, comparison: "unchanged" }] });
    expect(rejectedSubmitBlockerMessage(direct)).toBe("以下退回行为尚未处理，不能重新提交：标注 #31（未修改）");
    expect(reviewSubmitBlockers(direct)).toEqual([31]);
  });
});
