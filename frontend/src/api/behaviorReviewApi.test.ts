// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { createVideoReview, putBehaviorDecision, submitVideoForReview } from ".";

afterEach(() => vi.restoreAllMocks());

describe("behavior review API wiring", () => {
  it("sends snapshot identity and decision revision to the decision endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ annotations: [], counts: { pending: 0, approved: 0, rejected: 0 }, decision_revision: 8, can_finalize_approval: false, feedback_items: [], locked_annotation_ids: [], can_reopen: false }), { status: 200, headers: { "Content-Type": "application/json" } }));
    await putBehaviorDecision(2, 4, 7, 31, { status: "pending", feedback: null, expected_decision_revision: 6 });
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/projects\/2\/videos\/4\/submissions\/7\/annotations\/31\/decision$/);
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({ status: "pending", feedback: null, expected_decision_revision: 6 });
  });

  it("always carries submission and revision guards for review and resubmission", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify({ id: 1 }), { status: 200, headers: { "Content-Type": "application/json" } }));
    await createVideoReview(2, 4, { result: "rejected", comment: "调整", expected_submission_id: 7, expected_decision_revision: 6 });
    await submitVideoForReview(2, 4, { expected_submission_id: 7, expected_decision_revision: 6 });
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({ expected_submission_id: 7, expected_decision_revision: 6 });
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({ expected_submission_id: 7, expected_decision_revision: 6 });
  });
});
