import { describe, expect, it } from "vitest";
import type { Category, SubmissionAnnotationSnapshot } from "../api/types";
import {
  REVIEW_FALLBACK_COLOR,
  deriveReviewOverlay,
  deriveReviewAnnotationView,
  safeCategoryColor,
  toggleReviewCategory,
} from "./reviewAnnotationView";

const annotations = [
  { id: 1, category_id: 10, category_name: "攻击", category_group: "社交行为" },
  { id: 2, category_id: 20, category_name: "理毛", category_group: "个体行为" },
  { id: 3, category_id: 10, category_name: "攻击", category_group: "社交行为" },
] as SubmissionAnnotationSnapshot[];

const categories = [
  { id: 10, color: "#E6194B", sort_order: 2 },
  { id: 20, color: "not-a-color", sort_order: 1 },
] as Category[];

describe("review annotation view", () => {
  it("toggles multiple categories while an empty set explicitly means all", () => {
    const onlyAttack = toggleReviewCategory(new Set(), 10);
    const both = toggleReviewCategory(onlyAttack, 20);
    expect([...both]).toEqual([10, 20]);
    expect([...toggleReviewCategory(both, 10)]).toEqual([20]);
    expect(toggleReviewCategory(new Set([20]), 20).size).toBe(0);
    expect(toggleReviewCategory(both, null).size).toBe(0);
  });

  it("filters by the category union and keeps counts from the full Submission snapshot", () => {
    const view = deriveReviewAnnotationView(annotations, categories, new Set([10, 20]));
    expect(view.annotations.map((annotation) => annotation.id)).toEqual([1, 2, 3]);
    expect(view.categories.map(({ id, count }) => [id, count])).toEqual([[20, 1], [10, 2]]);

    const attackOnly = deriveReviewAnnotationView(annotations, categories, new Set([10]));
    expect(attackOnly.annotations.map((annotation) => annotation.id)).toEqual([1, 3]);
    expect(attackOnly.categories.map(({ count }) => count)).toEqual([1, 2]);
  });

  it("uses current colors only as metadata and falls back for invalid or missing colors", () => {
    const view = deriveReviewAnnotationView(annotations, categories, new Set());
    expect(view.categories.find((category) => category.id === 10)?.color).toBe("#E6194B");
    expect(view.categories.find((category) => category.id === 20)?.color).toBe(REVIEW_FALLBACK_COLOR);
    const missingMapping = deriveReviewAnnotationView(annotations, categories.slice(0, 1), new Set());
    expect(missingMapping.categories.find((category) => category.id === 20)?.color).toBe(REVIEW_FALLBACK_COLOR);
    expect(safeCategoryColor(null)).toBe(REVIEW_FALLBACK_COLOR);
    expect(safeCategoryColor("definitely invalid")).toBe(REVIEW_FALLBACK_COLOR);
  });

  it("hides all tracks when no filtered Submission behavior is active", () => {
    const overlay = deriveReviewOverlay([
      snapshot(1, 10, "攻击", 1, 2, [1], "攻击者", [1]),
    ], 3, null);
    expect(overlay.mouseIds).toEqual([]);
    expect(overlay.roleLabels).toEqual({});
    expect(overlay.activeAnnotationIds).toEqual([]);
  });

  it("unions overlapping participants and stably deduplicates behavior-prefixed roles", () => {
    const active = [
      snapshot(2, 20, "追逐", 1, 4, [2, 3], "追逐者", [2]),
      snapshot(1, 10, "攻击", 0, 3, [1, 2], "攻击者", [2]),
      snapshot(3, 10, "攻击", 0, 3, [2], "攻击者", [2]),
    ];
    const overlay = deriveReviewOverlay(active, 2, null);
    expect(overlay.mouseIds).toEqual([1, 2, 3]);
    expect(overlay.activeAnnotationIds).toEqual([1, 3, 2]);
    expect(overlay.roleLabels[2]).toBe("攻击：攻击者 / 追逐：追逐者");
  });

  it("uses only the focused annotation and drops focus after playback leaves its interval", () => {
    const active = [
      snapshot(1, 10, "攻击", 0, 3, [1, 2], "攻击者", [1]),
      snapshot(2, 20, "追逐", 1, 5, [2, 3], "追逐者", [3]),
    ];
    const focused = deriveReviewOverlay(active, 2, 2);
    expect(focused.focusedAnnotationId).toBe(2);
    expect(focused.mouseIds).toEqual([2, 3]);
    expect(focused.roleLabels).toEqual({ 3: "追逐：追逐者" });

    const afterBoundary = deriveReviewOverlay(active, 4, 1);
    expect(afterBoundary.focusedAnnotationId).toBeNull();
    expect(afterBoundary.mouseIds).toEqual([2, 3]);
  });

  it("derives active behavior only from the already filtered annotation collection", () => {
    const all = [
      snapshot(1, 10, "攻击", 0, 3, [1], "攻击者", [1]),
      snapshot(2, 20, "追逐", 0, 3, [2], "追逐者", [2]),
    ];
    const filtered = deriveReviewAnnotationView(all, categories, new Set([20])).annotations;
    expect(deriveReviewOverlay(filtered, 1, null).mouseIds).toEqual([2]);
  });
});

function snapshot(
  id: number,
  categoryId: number,
  categoryName: string,
  startTime: number,
  endTime: number,
  mouseIds: number[],
  roleName: string,
  roleMouseIds: number[],
): SubmissionAnnotationSnapshot {
  return {
    id,
    category_id: categoryId,
    category_name: categoryName,
    category_group: "社交行为",
    category_participant_mode: "role_based",
    confidence: "certain",
    start_frame: Math.round(startTime * 30),
    end_frame: Math.round(endTime * 30),
    start_time: startTime,
    end_time: endTime,
    mouse_ids: mouseIds,
    role_definitions: [{ key: "actor", name: roleName, min_count: 1, max_count: 1, role_sort_order: 0 }],
    participant_roles: { actor: roleMouseIds },
  } as SubmissionAnnotationSnapshot;
}
