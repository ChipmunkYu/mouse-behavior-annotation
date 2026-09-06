import type { Category, SubmissionAnnotationSnapshot } from "../api/types";

export const REVIEW_FALLBACK_COLOR = "var(--text-3)";

export type ReviewCategorySummary = Category & { count: number };
export interface ReviewOverlayState {
  activeAnnotationIds: number[];
  focusedAnnotationId: number | null;
  mouseIds: number[];
  roleLabels: Record<number, string>;
}

export function safeCategoryColor(color: string | null | undefined): string {
  const value = color?.trim();
  if (!value) return REVIEW_FALLBACK_COLOR;
  if (typeof CSS !== "undefined" && typeof CSS.supports === "function") {
    return CSS.supports("color", value) ? value : REVIEW_FALLBACK_COLOR;
  }
  return /^#[0-9a-f]{6}$/i.test(value) ? value : REVIEW_FALLBACK_COLOR;
}

export function deriveReviewAnnotationView(
  annotations: SubmissionAnnotationSnapshot[],
  currentCategories: Category[],
  selectedCategoryIds: ReadonlySet<number>,
): { annotations: SubmissionAnnotationSnapshot[]; categories: ReviewCategorySummary[] } {
  const currentById = new Map(currentCategories.map((category) => [category.id, category]));
  const firstByCategory = new Map<number, { annotation: SubmissionAnnotationSnapshot; index: number; count: number }>();

  annotations.forEach((annotation, index) => {
    const existing = firstByCategory.get(annotation.category_id);
    if (existing) existing.count += 1;
    else firstByCategory.set(annotation.category_id, { annotation, index, count: 1 });
  });

  const categories = [...firstByCategory.values()]
    .map(({ annotation, index, count }) => {
      const current = currentById.get(annotation.category_id);
      return {
        id: annotation.category_id,
        project_id: current?.project_id ?? 0,
        name: annotation.category_name ?? `类别 #${annotation.category_id}`,
        group: annotation.category_group ?? "历史类别",
        color: safeCategoryColor(current?.color),
        sort_order: current?.sort_order ?? Number.MAX_SAFE_INTEGER,
        is_active: current?.is_active ?? true,
        mouse_count_min: current?.mouse_count_min ?? 1,
        mouse_count_max: current?.mouse_count_max ?? null,
        participant_mode: annotation.category_participant_mode,
        role_definitions: annotation.role_definitions,
        count,
        _firstIndex: index,
      };
    })
    .sort((a, b) => a.sort_order - b.sort_order || a._firstIndex - b._firstIndex)
    .map(({ _firstIndex: _, ...category }) => category);

  return {
    annotations: selectedCategoryIds.size === 0
      ? annotations
      : annotations.filter((annotation) => selectedCategoryIds.has(annotation.category_id)),
    categories,
  };
}

export function toggleReviewCategory(
  selectedCategoryIds: ReadonlySet<number>,
  categoryId: number | null,
): Set<number> {
  if (categoryId == null) return new Set();
  const next = new Set(selectedCategoryIds);
  if (next.has(categoryId)) next.delete(categoryId);
  else next.add(categoryId);
  return next;
}

export function deriveReviewOverlay(
  annotations: SubmissionAnnotationSnapshot[],
  currentTime: number,
  focusedAnnotationId: number | null,
): ReviewOverlayState {
  const active = annotations
    .filter((annotation) => currentTime >= annotation.start_time && currentTime <= annotation.end_time)
    .sort((a, b) => a.start_time - b.start_time || a.id - b.id);
  const focused = focusedAnnotationId == null
    ? undefined
    : active.find((annotation) => annotation.id === focusedAnnotationId);
  const visible = focused ? [focused] : active;
  const mouseIds = [...new Set(visible.flatMap((annotation) => annotation.mouse_ids))].sort((a, b) => a - b);
  const labels = new Map<number, Set<string>>();

  for (const annotation of visible) {
    if (annotation.category_participant_mode !== "role_based") continue;
    const behavior = annotation.category_name ?? `类别 #${annotation.category_id}`;
    for (const role of [...annotation.role_definitions].sort((a, b) => a.role_sort_order - b.role_sort_order || a.key.localeCompare(b.key))) {
      for (const trackId of annotation.participant_roles[role.key] ?? []) {
        const trackLabels = labels.get(trackId) ?? new Set<string>();
        trackLabels.add(`${behavior}：${role.name}`);
        labels.set(trackId, trackLabels);
      }
    }
  }

  return {
    activeAnnotationIds: active.map((annotation) => annotation.id),
    focusedAnnotationId: focused?.id ?? null,
    mouseIds,
    roleLabels: Object.fromEntries([...labels].map(([trackId, values]) => [trackId, [...values].join(" / ")])),
  };
}
