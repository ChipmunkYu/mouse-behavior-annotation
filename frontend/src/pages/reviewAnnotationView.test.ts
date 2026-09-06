import { describe, expect, it } from "vitest";
import type { Category, SubmissionAnnotationSnapshot } from "../api/types";
import {
  REVIEW_FALLBACK_COLOR,
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
});
