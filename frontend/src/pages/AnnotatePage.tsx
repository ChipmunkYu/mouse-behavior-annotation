import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent, type Ref } from "react";
import { Link, useBlocker, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  createAnnotation,
  deleteAnnotation,
  exportAnnotations,
  listAnnotations,
  listCategories,
  listDetectionSuppressions,
  listProjects,
  listVideos,
  submitVideoForReview,
  updateAnnotation,
  checkIdentityEdit,
  commitIdentityEdit,
  createSuppression,
  getCorrectedTracks,
  revertIdentityEdit,
  revertSuppression,
} from "../api";
import { ApiError } from "../api/client";
import type {
  Annotation,
  AnnotationPatchInput,
  Category,
  Project,
  Video,
  CorrectedTrackSummary,
  DetectionImport,
  DetectionWithTrack,
  DetectionSuppression,
  IdentityEditResult,
} from "../api/types";
import { ROLE_LABELS, WORKFLOW_LABELS } from "../api/types";
import { Card, EmptyState, Loading, WorkflowBadge, statusLabel } from "../components/ui";
import { useConfirm } from "../components/ConfirmDialog";
import { MediaStatusPanel } from "../components/MediaStatusPanel";
import { MediaLoadProgress } from "../components/MediaLoadProgress";
import Timeline from "../components/Timeline";
import DetectionOverlay from "../components/DetectionOverlay";
import { ParticipantSummary } from "../components/ParticipantSummary";
import { clampFrame, formatDate, formatTime, formatTimeShort, frameToEndTime, frameToStartTime } from "../utils/format";
import { getAdjacentVideos, sortVideosForNavigation } from "../utils/videoNavigation";
import { getInitiallyUnlockedRoleKeys, isRoleAccessible } from "../utils/roleNavigation";
import { useMediaSource } from "../media";

type SaveState = "idle" | "saving" | "saved" | "error";
type Point = { frame: number };
type UndoEntry = { kind: "identity" | "suppression"; id: number; createdAt: number };
type IdentityEditFeedback = { text: string; key: number; routeKey: string };
type DraftField = "category" | "start" | "end" | "participants";
type DraftSnapshot = {
  activeCategory: Category | null;
  startPoint: Point | null;
  endPoint: Point | null;
  selectedMouseIds: number[];
  participantRoles: Record<string, number[]>;
  activeRoleKey: string | null;
  unlockedRoleKeys: Set<string>;
  roleMessage: string | null;
};

const CATEGORY_SHORTCUT_KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"] as const;

export function buildIdentityEditFeedback(
  operation: "split" | "merge",
  selectedTrackIds: number[],
  frame: number,
  result: Pick<IdentityEditResult, "new_display_track_id" | "retained_display_track_id">
): string {
  if (operation === "split") {
    return result.new_display_track_id != null
      ? `Split 完成：Track ${selectedTrackIds[0]} 从帧 ${frame} 起拆分为新 Track ${result.new_display_track_id}，已自动选中。`
      : "Split 已完成，但服务端未返回新 Track ID。";
  }
  return result.retained_display_track_id != null
    ? `Merge 完成：保留 Track ${result.retained_display_track_id}，已并入 ${selectedTrackIds.length - 1} 个 track，已自动选中。`
    : "Merge 已完成，但服务端未返回保留的 Track ID。";
}

export function identityEditFeedbackForRoute(feedback: IdentityEditFeedback | null, routeKey: string): IdentityEditFeedback | null {
  return feedback?.routeKey === routeKey ? feedback : null;
}

/**
 * 数字键只覆盖稳定显示顺序中的前 10 个启用类别。
 * 当前类别模型没有显式的群体行为快捷键字段，因此不按名称、分组或参与对象数量猜测。
 */
export function sortCategoriesForDisplay(categories: Category[]): Category[] {
  return [...categories].sort((a, b) => a.sort_order - b.sort_order || a.id - b.id);
}

export function buildCategoryShortcuts(categories: Category[]): Array<{ key: string; category: Category }> {
  return sortCategoriesForDisplay(categories)
    .filter((category) => category.is_active)
    .slice(0, CATEGORY_SHORTCUT_KEYS.length)
    .map((category, index) => ({ key: CATEGORY_SHORTCUT_KEYS[index], category }));
}

function verifyCategoryShortcuts(categories: Category[], shortcuts: Array<{ key: string; category: Category }>): void {
  const expected = sortCategoriesForDisplay(categories)
    .filter((category) => category.is_active)
    .slice(0, CATEGORY_SHORTCUT_KEYS.length);
  const valid = shortcuts.length === expected.length && shortcuts.every(({ key, category }, index) => (
    key === CATEGORY_SHORTCUT_KEYS[index] && category.id === expected[index]?.id
  ));
  if (!valid) {
    console.error("[快捷键映射校验失败] 类别数字键没有遵循稳定显示顺序", { categories, shortcuts });
    return;
  }
  console.info("[快捷键映射校验]", shortcuts.map(({ key, category }) => `${key}=${category.name}`).join("，"));
}

function digitShortcutFromEvent(event: KeyboardEvent): string | null {
  if (event.ctrlKey || event.altKey || event.metaKey || event.shiftKey) return null;
  const digit = event.code.startsWith("Digit")
    ? event.code.slice(5)
    : event.code.startsWith("Numpad")
      ? event.code.slice(6)
      : event.key;
  return CATEGORY_SHORTCUT_KEYS.includes(digit as (typeof CATEGORY_SHORTCUT_KEYS)[number]) ? digit : null;
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable || target.closest("[contenteditable]:not([contenteditable='false'])") != null;
}

function blurActiveButton(): void {
  if (document.activeElement instanceof HTMLButtonElement) document.activeElement.blur();
}

type AnnotateEscapeAction = "close-help" | "cancel-edit" | "exit-participant-navigation" | "exit-identity-navigation";

export function resolveAnnotateEscapeAction(state: {
  shortcutHelpOpen: boolean;
  editingAnnotationId: number | null;
  participantNavigationActive: boolean;
  identityNavigationActive: boolean;
  hasDraft: boolean;
}): AnnotateEscapeAction | null {
  if (state.shortcutHelpOpen) return "close-help";
  if (state.editingAnnotationId != null) return "cancel-edit";
  if (state.participantNavigationActive) return "exit-participant-navigation";
  if (state.identityNavigationActive) return "exit-identity-navigation";
  // 有草稿本身不是取消目标；重置草稿只能由显式按钮发起。
  return null;
}

function draftApiErrorMessages(error: unknown): string[] {
  if (!(error instanceof ApiError)) return [error instanceof Error ? error.message : "保存行为标注失败"];
  const messages: string[] = [];
  const raw = error.detail && typeof error.detail === "object" && "detail" in error.detail
    ? (error.detail as { detail?: unknown }).detail
    : error.detail;
  const visit = (value: unknown) => {
    if (typeof value === "string") {
      if (value.trim()) messages.push(value.trim());
      return;
    }
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (!value || typeof value !== "object") return;
    const row = value as Record<string, unknown>;
    const text = row.message ?? row.msg ?? row.reason;
    if (typeof text === "string") {
      const trackId = row.track_id ?? row.display_track_id ?? row.mouse_id;
      messages.push(trackId == null ? text : `Track ${trackId}：${text}`);
    }
    for (const key of ["errors", "invalid_tracks", "invalid_track_ids", "missing_tracks", "missing_track_ids", "conflicts"]) {
      if (key in row) visit(row[key]);
    }
  };
  visit(raw);
  if (messages.length === 0) messages.push(error.message);
  return [...new Set(messages)];
}

function inferDraftErrorFields(messages: string[]): Set<DraftField> {
  const fields = new Set<DraftField>();
  for (const message of messages) {
    if (/类别|category/i.test(message)) fields.add("category");
    if (/开始|start/i.test(message)) fields.add("start");
    if (/结束|end|单帧|区间/i.test(message)) fields.add("end");
    if (/track|mouse|参与|角色|检测/i.test(message)) fields.add("participants");
  }
  return fields;
}

/* ================= 行为类别面板（按 group 动态分组，绝不硬编码类别） ================= */
function groupCategories(categories: Category[]): [string, Category[]][] {
  const map = new Map<string, Category[]>();
  for (const c of categories) {
    const arr = map.get(c.group) ?? [];
    arr.push(c);
    map.set(c.group, arr);
  }
  return [...map.entries()];
}

function CategoryPanel({
  categories,
  activeCategory,
  shortcuts,
  onSelect,
  disabled,
}: {
  categories: Category[];
  activeCategory: Category | null;
  shortcuts: Map<number, string>;
  onSelect: (c: Category) => void;
  disabled: boolean;
}) {
  const groups = useMemo(() => groupCategories(categories), [categories]);

  if (categories.length === 0) {
    return (
      <Card title="行为类别">
        <EmptyState title="暂无类别" compact hint="项目尚未初始化行为类别" />
      </Card>
    );
  }

  return (
    <Card title={`行为类别（${categories.length}）`} className="category-panel">
      <div className="group-list">
        {groups.map(([group, list]) => (
          <div className="group" key={group}>
          <div className="group-title">{group}</div>
          <div className="category-grid">
            {list.map((c) => (
              <button
                key={c.id}
                type="button"
                className={activeCategory?.id === c.id ? "cat-btn active" : "cat-btn"}
                disabled={disabled}
                onClick={(e) => {
                  // 选择后清除旧按钮焦点，避免与后续列表导航高亮混淆。
                  e.currentTarget.blur();
                  onSelect(c);
                }}
                title={`${c.group} · ${c.name}`}
              >
                <span className="swatch" style={{ background: c.color ?? "var(--text-3)" }} />
                <span>{c.name}</span>
                {shortcuts.get(c.id) ? <span className="shortcut-key">[{shortcuts.get(c.id)}]</span> : null}
              </button>
            ))}
          </div>
        </div>
      ))}
      </div>
      {activeCategory ? (
        <div className="frame-preview" style={{ marginTop: 10 }}>
          当前类别：<b>{activeCategory.name}</b> · 按 S 设开始、D 设结束，Ctrl+Enter 保存
        </div>
      ) : null}
    </Card>
  );
}

/* ================= 标注区间列表 ================= */
function AnnotationRow({
  ann,
  categoryById,
  active,
  selected,
  busy,
  readOnly,
  onEdit,
  onDelete,
  onSelect,
  rowRef,
}: {
  ann: Annotation;
  categoryById: Map<number, Category>;
  active: boolean;
  selected: boolean;
  busy: boolean;
  readOnly: boolean;
  onEdit: () => void;
  onDelete: () => void;
  onSelect: () => void;
  rowRef?: Ref<HTMLDivElement>;
}) {
  const cat = categoryById.get(ann.category_id);
  return (
    <div ref={rowRef} className={`anno-row${active ? " active" : ""}${selected ? " selected" : ""}`} onClick={onSelect}>
      <div className="anno-row-top">
        <span className="anno-cat" title={cat?.group ?? ""}>
          <span className="swatch" style={{ background: cat?.color ?? "var(--text-3)" }} />
          <span className="name">{ann.category_name ?? cat?.name ?? `类别 #${ann.category_id}`}</span>
        </span>
        <span className="anno-times">
          <b>{formatTimeShort(ann.start_time)}</b> – <b>{formatTimeShort(ann.end_time)}</b>
        </span>
        <span className="anno-row-actions">
          <button type="button" className="btn-link" disabled={busy || readOnly} onClick={onEdit}>
            编辑
          </button>
          <button type="button" className="btn-link" disabled={busy || readOnly} onClick={onDelete} style={{ color: "var(--danger)" }}>
            删除 [Delete]
          </button>
        </span>
      </div>
      <div className="anno-row-meta">
        <span>
          帧 {ann.start_frame} → {ann.end_frame}
        </span>
        <span>·</span>
        <span>标注者 {ann.annotator ?? `#${ann.annotator_id}`}</span>
        <span>·</span>
        <span>
          可信度 {statusLabel(ann.confidence)} · 审核 {statusLabel(ann.review_status)}
        </span>
        <ParticipantSummary mode={cat?.participant_mode ?? "unordered"} roles={cat?.role_definitions ?? []} assignments={ann.participant_roles} mouseIds={ann.mouse_ids} compact />
        {ann.participant_status === "needs_participants" ? <span className="mouse-status warning">角色待补全</span> : null}
        {ann.mouse_id_status === "needs_mouse_ids" ? <span className="mouse-status warning">{cat?.participant_mode === "role_based" ? "Track 已失效，需要重新分配" : "Track 已失效，需要重新选择"}</span> : null}
      </div>
    </div>
  );
}

function AnnotationEditForm({
  ann,
  categories,
  fps,
  frameCount,
  onCancel,
  onSave,
  onCategoryChange,
}: {
  ann: Annotation;
  categories: Category[];
  fps: number | null | undefined;
  frameCount: number | null | undefined;
  onCancel: () => void;
  onSave: (patch: AnnotationPatchInput) => Promise<void>;
  onCategoryChange: (category: Category) => Promise<boolean>;
}) {
  const [categoryId, setCategoryId] = useState(ann.category_id);
  const [startFrame, setStartFrame] = useState(String(ann.start_frame));
  const [endFrame, setEndFrame] = useState(String(ann.end_frame));
  const [localError, setLocalError] = useState<string | null>(null);
  const [mouseIds, setMouseIds] = useState(ann.mouse_ids.join(", "));
  const selectedCategory = categories.find((c) => c.id === categoryId);
  const [saving, setSaving] = useState(false);

  const groups = useMemo(() => groupCategories(categories), [categories]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const s = Number(startFrame);
    const en = Number(endFrame);
    const resolvedFps = fps && fps > 0 ? fps : null;
    if (!Number.isInteger(s) || !Number.isInteger(en)) {
      setLocalError("开始帧和结束帧必须是整数");
      return;
    }
    if (s < 0 || en < 0) {
      setLocalError("帧索引不能为负");
      return;
    }
    if (frameCount && s >= frameCount) {
      setLocalError(`开始帧不能超过最后一帧 ${frameCount - 1}`);
      return;
    }
    if (en <= s) {
      setLocalError("结束帧必须大于开始帧，单帧行为不能保存");
      return;
    }
    if (frameCount && en >= frameCount) {
      setLocalError(`结束帧不能超过最后一帧 ${frameCount - 1}`);
      return;
    }
    if (!resolvedFps) {
      setLocalError("视频 FPS 无效，无法由帧派生时间");
      return;
    }
    setSaving(true);
    setLocalError(null);
    try {
      await onSave({
        category_id: categoryId,
        start_time: frameToStartTime(s, resolvedFps),
        end_time: frameToEndTime(en, resolvedFps),
        start_frame: s,
        end_frame: en,
        ...(selectedCategory?.participant_mode === "unordered" ? { mouse_ids: [...new Set(mouseIds.split(/[,，\s]+/).filter(Boolean).map(Number))].filter(Number.isInteger).sort((a, b) => a - b) } : { participant_roles: ann.participant_roles }),
        detection_import_revision: ann.detection_import_revision,
        identity_revision: ann.identity_revision,
      });
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : "保存失败");
      setSaving(false);
    }
  }

  const sNum = Number(startFrame);
  const eNum = Number(endFrame);
  const resolvedFps = fps && fps > 0 ? fps : null;

  return (
    <form className="anno-edit" onSubmit={submit}>
      <div className="field">
        <label htmlFor={`edit-cat-${ann.id}`}>类别</label>
        <select
          id={`edit-cat-${ann.id}`}
          className="select"
          value={categoryId}
          onChange={(e) => { const next = categories.find((c) => c.id === Number(e.target.value)); if (next) void onCategoryChange(next).then((ok) => { if (ok) setCategoryId(next.id); }); }}
        >
          {groups.map(([group, list]) => (
            <optgroup key={group} label={group}>
              {list.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </div>
      <div className="field-row">
        <div className="field">
          <label htmlFor={`edit-start-${ann.id}`}>开始帧</label>
          <input
            id={`edit-start-${ann.id}`}
            className="input"
            type="number"
            min="0"
            max={frameCount ? frameCount - 1 : undefined}
            step="1"
            value={startFrame}
            onChange={(e) => setStartFrame(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor={`edit-end-${ann.id}`}>结束帧（inclusive）</label>
          <input
            id={`edit-end-${ann.id}`}
            className="input"
            type="number"
            min="0"
            max={frameCount ? frameCount - 1 : undefined}
            step="1"
            value={endFrame}
            onChange={(e) => setEndFrame(e.target.value)}
          />
        </div>
      </div>
      <div className="frame-preview">
        派生时间（只读）：{resolvedFps && Number.isInteger(sNum) ? formatTime(frameToStartTime(sNum, resolvedFps)) : "—"} →{" "}
        {resolvedFps && Number.isInteger(eNum) ? formatTime(frameToEndTime(eNum, resolvedFps)) : "—"}
      </div>
      {selectedCategory?.participant_mode === "unordered" ? <div className="field"><label htmlFor={`edit-mice-${ann.id}`}>参与对象（填写 track ID，以逗号分隔）</label><input id={`edit-mice-${ann.id}`} className="input mono" value={mouseIds} onChange={(e) => setMouseIds(e.target.value)} /></div> : <div className="field-hint">角色分配请使用上方参与对象角色槽位；保存时会提交完整角色分配。</div>}
      <div className="form-error">{localError ?? ""}</div>
      <div className="actions">
        <button type="button" className="btn btn-sm" onClick={onCancel} disabled={saving}>
          取消 [Esc]
        </button>
        <button type="submit" className="btn btn-sm btn-primary" disabled={saving}>
          {saving ? "保存中…" : "保存"}
        </button>
      </div>
    </form>
  );
}

function AnnotationList({
  annotations,
  categories,
  categoryById,
  fps,
  frameCount,
  currentTime,
  readOnly,
  selectedId,
  editingId,
  onSelect,
  onEditingChange,
  onEditSave,
  onDelete,
  onEditCategoryChange,
}: {
  annotations: Annotation[];
  categories: Category[];
  categoryById: Map<number, Category>;
  fps: number | null | undefined;
  frameCount: number | null | undefined;
  currentTime: number;
  readOnly: boolean;
  selectedId: number | null;
  editingId: number | null;
  onSelect: (id: number) => void;
  onEditingChange: (id: number | null) => void;
  onEditSave: (id: number, patch: AnnotationPatchInput) => Promise<void>;
  onDelete: (ann: Annotation) => Promise<void>;
  onEditCategoryChange: (category: Category) => Promise<boolean>;
}) {
  const [busyId, setBusyId] = useState<number | null>(null);
  const activeId = annotations.find(
    (a) => currentTime >= a.start_time && currentTime <= a.end_time
  )?.id;

  const activeRowRef = useRef<HTMLDivElement | null>(null);

  // 播放/跳转时，若当前行为标注位于折叠区之外，将其轻微滚入视野
  useEffect(() => {
    if (activeId == null) return;
    activeRowRef.current?.scrollIntoView({ block: "nearest" });
  }, [activeId]);

  async function handleDelete(ann: Annotation) {
    setBusyId(ann.id);
    try {
      await onDelete(ann);
      onEditingChange(null);
    } catch {
      // 错误提示已由父级处理
    } finally {
      setBusyId(null);
    }
  }

  return (
    <Card title={`标注区间（${annotations.length}）`}>
      {annotations.length === 0 ? (
        <EmptyState compact title="暂无行为标注" hint="播放视频并填写行为草稿，再点击“保存此行为”" />
      ) : (
        <div className="anno-list-body">
          {annotations.map((a) =>
            editingId === a.id ? (
              <AnnotationEditForm
                key={a.id}
                ann={a}
                categories={categories}
                fps={fps}
                frameCount={frameCount}
                onCancel={() => onEditingChange(null)}
                onSave={async (patch) => {
                  await onEditSave(a.id, patch);
                  onEditingChange(null);
                }}
                onCategoryChange={onEditCategoryChange}
              />
            ) : (
              <AnnotationRow
                key={a.id}
                ann={a}
                categoryById={categoryById}
                active={activeId === a.id}
                selected={selectedId === a.id}
                busy={busyId === a.id}
                readOnly={readOnly}
                onEdit={() => { if (!readOnly) { onSelect(a.id); onEditingChange(a.id); } }}
                onDelete={() => void handleDelete(a)}
                onSelect={() => onSelect(a.id)}
                rowRef={activeId === a.id ? activeRowRef : undefined}
              />
            )
          )}
        </div>
      )}
    </Card>
  );
}

/* ================= 保存状态提示 ================= */
function SaveStatus({ state, message }: { state: SaveState; message?: string | null }) {
  if (state === "idle") return null;
  const label =
    state === "saving" ? "保存中…" : state === "saved" ? "已保存" : "保存失败";
  return (
    <span className={`save-pill ${state}`} title={message ?? undefined} role="status" aria-live="polite">
      {label}
    </span>
  );
}

/* ================= 提交审核控件 =================
 * draft / rejected → 「提交审核」；submitted → 「等待审核」；approved → 「已通过」
 */
function SubmitReviewControl({
  status,
  hasAnnotations,
  blockedReason,
  submitting,
  onSubmit,
}: {
  status: string;
  hasAnnotations: boolean;
  blockedReason: string | null;
  submitting: boolean;
  onSubmit: () => void;
}) {
  if (status === "submitted") {
    return (
      <span className="submit-pill pending" role="status" title="该视频已在审核队列中">
        ⏳ 等待审核
      </span>
    );
  }
  if (status === "approved") {
    return (
      <span className="submit-pill ok" role="status" title="该视频已审核通过">
        ✓ 已通过
      </span>
    );
  }
  return (
    <button
      type="button"
      className="btn btn-sm btn-primary"
      disabled={!hasAnnotations || blockedReason != null || submitting}
      title={blockedReason ?? (hasAnnotations ? "提交审核后，审核人将按当前行为标注版本审核" : "至少需要一条行为标注才能提交审核")}
      onClick={onSubmit}
    >
      {submitting ? "提交中…" : "提交审核"}
    </button>
  );
}

function MouseIdsPanel({ tracks, selected, category, disabled, navigationActive, focusIndex, onFocusIndex, onExitNavigation, onToggle }: {
  tracks: CorrectedTrackSummary[];
  selected: number[];
  category: Category | null;
  disabled: boolean;
  navigationActive: boolean;
  focusIndex: number;
  onFocusIndex: (index: number) => void;
  onExitNavigation: () => void;
  onToggle: (id: number) => void;
}) {
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const min = category?.mouse_count_min ?? 1;
  const max = category?.mouse_count_max ?? null;
  const valid = selected.length >= min && (max == null || selected.length <= max);
  const rule = max == null ? `至少 ${min} 个对象` : min === max ? `恰好 ${min} 个对象` : `${min}–${max} 个对象`;
  useEffect(() => {
    if (navigationActive) itemRefs.current[focusIndex]?.scrollIntoView({ block: "nearest" });
  }, [focusIndex, navigationActive]);
  return <Card title="参与对象" className={`mouse-ids-panel${navigationActive ? " keyboard-nav" : ""}`} extra={<span className={valid ? "mouse-count valid" : "mouse-count"}>{selected.length} / {rule}</span>}>
    {navigationActive ? <div className="participant-nav-status" role="status"><span>键盘选择中：↑/↓ 移动，Enter 选择，T 退出</span><button type="button" className="btn-link" onClick={onExitNavigation}>退出 [T / Esc]</button></div> : null}
    <div className="selected-mice">{selected.length ? selected.map((id) => <button key={id} className="mouse-chip selected" onClick={() => onToggle(id)}>track ID {id} ×</button>) : <span>点击视频框或下方 track ID 选择参与对象</span>}</div>
    <div className="mouse-id-list">{tracks.map((track, index) => <button ref={(node) => { itemRefs.current[index] = node; }} data-participant-item key={track.display_track_id} disabled={disabled} className={`${selected.includes(track.display_track_id) ? "mouse-id-item selected" : "mouse-id-item"}${navigationActive && focusIndex === index ? " keyboard-focused" : ""}`} onClick={() => { onFocusIndex(index); onToggle(track.display_track_id); }}><b>track ID {track.display_track_id}</b><span>{track.visible_in_current_frame ? "当前可见" : `${track.first_frame ?? "?"}–${track.last_frame ?? "?"}`}</span></button>)}</div>
    {!valid && category ? <div className="mouse-rule-warning">“{category.name}”需要{rule}，当前选择不符合规则。</div> : null}
  </Card>;
}

function RoleSlotsPanel({ category, assignments, pendingIds, activeKey, unlocked, tracks, disabled, message, onActivate, onTrack, onRemove, onRemovePending }: {
  category: Category; assignments: Record<string, number[]>; pendingIds: number[]; activeKey: string | null; unlocked: Set<string>; tracks: CorrectedTrackSummary[]; disabled: boolean; message: string | null;
  onActivate: (key: string) => void; onTrack: (id: number) => void; onRemove: (key: string, id: number) => void; onRemovePending: (id: number) => void;
}) {
  const roles = [...category.role_definitions].sort((a, b) => a.role_sort_order - b.role_sort_order);
  const trackRole = new Map<number, string>(); roles.forEach((r) => (assignments[r.key] ?? []).forEach((id) => trackRole.set(id, r.name)));
  return <Card title="参与对象角色" className="role-slots-panel" extra={<span className="role-slot-legend">按角色分配 · 不会自动切换</span>}>
    {pendingIds.length ? <div className="pending-role-tracks" role="status"><b>待分配：</b>先选择角色槽位，再点击 Track。{pendingIds.map((id) => <button type="button" key={id} className="role-track-chip" onClick={() => onRemovePending(id)}>Track {id}<span aria-hidden="true"> ×</span><span className="visually-hidden">从待分配中移除</span></button>)}</div> : null}
    <div className="role-slots" role="list" aria-label="参与对象角色槽位">{roles.map((role) => { const ids = assignments[role.key] ?? []; const complete = ids.length >= role.min_count && (role.max_count == null || ids.length <= role.max_count); const accessible = isRoleAccessible(roles, assignments, unlocked, role.key); return <button type="button" role="listitem" key={role.key} disabled={!accessible} title={accessible ? `切换到“${role.name}”` : "请先完成所有前序角色的最少数量"} className={`role-slot${activeKey === role.key ? " active" : ""}${complete ? " complete" : " incomplete"}${!accessible ? " locked" : ""}`} onClick={() => onActivate(role.key)} aria-pressed={activeKey === role.key} aria-label={`${role.name}，${complete ? "已完成" : "未完成"}，已选 ${ids.length}`}><span className="role-slot-index">{accessible ? (complete ? "✓" : "!") : "🔒"}</span><span className="role-slot-main"><b>{role.name}</b><small>{ids.length} / {role.max_count == null ? `至少 ${role.min_count}` : role.min_count === role.max_count ? `${role.min_count}` : `${role.min_count}–${role.max_count}`} · {complete ? "已完成" : accessible ? "可点击切换" : "前序完成后可进入"}</small></span>{activeKey === role.key ? <span className="role-active-label">当前</span> : null}</button>; })}</div>
    <div className="role-slot-chips">{roles.map((role) => (assignments[role.key] ?? []).map((id) => <button type="button" key={`${role.key}-${id}`} className="role-track-chip" onClick={() => onRemove(role.key, id)}><b>{role.name}</b> · Track {id}<span aria-hidden="true"> ×</span><span className="visually-hidden">移除</span></button>))}{Object.values(assignments).every((ids) => ids.length === 0) ? <span className="muted">先选择角色槽位，再点击视频框或下方 Track。</span> : null}</div>
    <div className="mouse-id-list">{tracks.map((track) => { const assigned = trackRole.get(track.display_track_id); const pending = pendingIds.includes(track.display_track_id); return <button type="button" key={track.display_track_id} disabled={disabled || !activeKey} className={`mouse-id-item${assigned || pending ? " selected" : ""}`} onClick={() => onTrack(track.display_track_id)}><b>Track {track.display_track_id}</b><span>{assigned ? `已分配：${assigned}` : pending ? "待分配 · 点击加入当前角色" : track.visible_in_current_frame ? "当前可见 · 点击加入" : `${track.first_frame ?? "?"}–${track.last_frame ?? "?"}`}</span></button>; })}</div>
    {message ? <div className="mouse-rule-warning" role="status">{message}</div> : null}
  </Card>;
}

function IdentityPanel({ tracks, selected, frame, search, showAll, busy, suppressions, canRevertSuppression, canRevertIdentity, canUndoLatest, undoBoundary, navigationActive, focusIndex, onFocusIndex, onExitNavigation, onSearch, onShowAll, onToggle, onSplit, onMerge, onSuppressTrack, onUndoLatest, onRevertSuppression, onRevertIdentity }: {
  tracks: CorrectedTrackSummary[]; selected: number[]; frame: number; search: string; showAll: boolean; busy: boolean; suppressions: DetectionSuppression[]; canRevertSuppression: boolean; canRevertIdentity: boolean;
  canUndoLatest: boolean; undoBoundary: string; navigationActive: boolean; focusIndex: number;
  onFocusIndex: (index: number) => void; onExitNavigation: () => void;
  onSearch: (s: string) => void; onShowAll: (v: boolean) => void; onToggle: (id: number) => void; onSplit: () => void; onMerge: () => void; onSuppressTrack: () => void; onUndoLatest: () => void; onRevertSuppression: (id?: number) => void; onRevertIdentity: () => void;
}) {
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);
  useEffect(() => {
    if (navigationActive) itemRefs.current[focusIndex]?.scrollIntoView({ block: "nearest" });
  }, [focusIndex, navigationActive]);
  return <Card title="track 修正" className={`identity-panel${navigationActive ? " keyboard-nav" : ""}`} extra={<span className="identity-frame mono">帧 {frame}</span>}>
    {navigationActive ? <div className="participant-nav-status" role="status"><span>键盘导航中：↑/↓ 移动，Enter 选择，T 退出</span><button type="button" className="btn-link" onClick={onExitNavigation}>退出 [T / Esc]</button></div> : null}
    <div className="identity-filter"><div className="segmented"><button className={!showAll ? "active" : ""} onClick={() => onShowAll(false)}>当前帧可见</button><button className={showAll ? "active" : ""} onClick={() => onShowAll(true)}>全部 track ID</button></div><input className="input" placeholder="搜索 track ID" aria-label="搜索 track ID" value={search} onChange={(e) => onSearch(e.target.value)} /></div>
    <div className="identity-track-list">{tracks.map((t, index) => <button ref={(node) => { itemRefs.current[index] = node; }} key={t.display_track_id} className={`${selected.includes(t.display_track_id) ? "identity-track selected" : "identity-track"}${navigationActive && focusIndex === index ? " keyboard-focused" : ""}`} onClick={() => { onFocusIndex(index); onToggle(t.display_track_id); }}><span className="track-id mono">track ID {t.display_track_id}</span><span>{t.visible_in_current_frame ? "● 当前可见" : `${t.first_frame ?? "?"} → ${t.last_frame ?? "?"}`}</span><span className="flex-spacer" /><span>{t.detection_count} 框</span></button>)}</div>
    <div className="identity-actions"><button className="btn btn-primary" disabled={busy || selected.length !== 1} onClick={onSplit}>从当前帧 Split</button><button className="btn btn-primary" disabled={busy || selected.length < 2} onClick={onMerge}>Merge 所选</button></div>
    <div className="identity-danger-zone"><button className="btn btn-sm btn-danger" disabled={busy || selected.length !== 1} onClick={onSuppressTrack}>忽略整个 track [Delete]</button><button className="btn btn-sm" disabled={busy || !canUndoLatest} onClick={onUndoLatest}>撤销上一次 track 修正 [Ctrl+Z]</button><button className="btn btn-sm" disabled={busy || !canRevertSuppression} onClick={() => onRevertSuppression()}>撤销上一次忽略</button><button className="btn btn-sm" disabled={busy || !canRevertIdentity} onClick={onRevertIdentity}>撤销上一次 Split / Merge</button></div>
    <div className="frame-preview">{undoBoundary}</div>
    {suppressions.length ? <div className="identity-track-list">{suppressions.map((s) => <div key={s.id} className="identity-track"><span>忽略记录 #{s.id}</span><span>{s.frozen_detection_count} 个检测</span><span className="flex-spacer" /><button className="btn btn-sm" disabled={busy} onClick={() => onRevertSuppression(s.id)}>撤销</button></div>)}</div> : null}
  </Card>;
}

function ShortcutHelp({ mode, categoryShortcuts, onClose }: {
  mode: "behavior" | "identity";
  categoryShortcuts: Array<{ key: string; category: Category }>;
  onClose: () => void;
}) {
  return <div className="modal-overlay shortcut-help-overlay" onClick={onClose}>
    <div className="modal shortcut-help" role="dialog" aria-modal="true" aria-labelledby="shortcut-help-title" onClick={(e) => e.stopPropagation()}>
      <div className="modal-title" id="shortcut-help-title">键盘快捷键</div>
      <div className="shortcut-help-grid">
        <kbd>Space</kbd><span>仅播放 / 暂停视频</span>
        <kbd>Tab</kbd><span>切换行为标注 / track 修正模式</span>
        <kbd>Shift+Tab</kbd><span>已消费，不执行操作</span>
        <kbd>T</kbd><span>进入 / 退出当前模式的 track 列表键盘导航</span>
        <kbd>Enter</kbd><span>导航中选择当前高亮项；非危险确认弹窗中执行确认</span>
        <kbd>← / →</kbd><span>前后 1 帧</span>
        <kbd>Shift+← / Shift+→</kbd><span>前后 10 帧</span>
        <kbd>《</kbd><span>上一个视频（物理键 Shift+Comma）</span>
        <kbd>》</kbd><span>下一个视频（物理键 Shift+Period）</span>
        <kbd>?</kbd><span>打开 / 关闭本帮助</span>
        <kbd>Esc</kbd><span>关闭帮助、取消编辑、退出列表导航或取消确认</span>
        {mode === "behavior" ? <>
          <kbd>S / D</kbd><span>设置开始 / 设置结束（不保存）</span>
          <kbd>Ctrl+Enter</kbd><span>保存此行为；与页面主按钮使用相同校验与确认</span>
          <kbd>1–9 / 0</kbd><span>{categoryShortcuts.length ? `按面板顺序选择前 ${categoryShortcuts.length} 个启用行为类别` : "当前没有可映射的启用行为类别"}</span>
          <kbd>↑ / ↓</kbd><span>参与对象列表导航时移动高亮</span>
          <kbd>Delete</kbd><span>为当前选中的行为标注打开删除确认框</span>
        </> : <>
          <kbd>↑ / ↓</kbd><span>track 列表导航时移动高亮</span>
          <kbd>Delete</kbd><span>为单一选中的整个 track 打开忽略确认框</span>
          <kbd>Ctrl+Z</kbd><span>仅撤销当前页面会话内最近一次可追踪的 track 修正；刷新后历史不完整</span>
        </>}
      </div>
      <div className="modal-actions"><button type="button" className="btn" onClick={onClose}>关闭 [? / Esc]</button></div>
    </div>
  </div>;
}

/* ================= 标注工作台主页面 ================= */
export default function AnnotatePage() {
  const { projectId, videoId } = useParams<{ projectId: string; videoId: string }>();
  const pid = Number(projectId);
  const vid = Number(videoId);
  const navigate = useNavigate();

  const videoRef = useRef<HTMLVideoElement>(null);
  const loadAllRequestRef = useRef(0);
  const routeKeyRef = useRef(`${pid}:${vid}`);
  routeKeyRef.current = `${pid}:${vid}`;

  const [project, setProject] = useState<Project | null>(null);
  const [video, setVideo] = useState<Video | null>(null);
  const [projectVideos, setProjectVideos] = useState<Video[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [loading, setLoading] = useState(true);

  const [elementDuration, setElementDuration] = useState(0);

  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [activeCategory, setActiveCategory] = useState<Category | null>(null);
  const [startPoint, setStartPoint] = useState<Point | null>(null);
  const [endPoint, setEndPoint] = useState<Point | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [workspaceMode, setWorkspaceMode] = useState<"behavior" | "identity">("behavior");
  const [selectedMouseIds, setSelectedMouseIds] = useState<number[]>([]);
  const [identitySelectedMouseIds, setIdentitySelectedMouseIds] = useState<number[]>([]);
  const [participantRoles, setParticipantRoles] = useState<Record<string, number[]>>({});
  const [activeRoleKey, setActiveRoleKey] = useState<string | null>(null);
  const [unlockedRoleKeys, setUnlockedRoleKeys] = useState<Set<string>>(new Set());
  const [roleMessage, setRoleMessage] = useState<string | null>(null);
  const [tracks, setTracks] = useState<CorrectedTrackSummary[]>([]);
  const [currentFrame, setCurrentFrame] = useState(0);
  const [detectionImport, setDetectionImport] = useState<DetectionImport | null>(null);
  const [identityRevision, setIdentityRevision] = useState(0);
  const [identitySearch, setIdentitySearch] = useState("");
  const [showAllTracks, setShowAllTracks] = useState(false);
  const [identityBusy, setIdentityBusy] = useState(false);
  const [identityEditFeedback, setIdentityEditFeedback] = useState<IdentityEditFeedback | null>(null);
  const [overlayRefresh, setOverlayRefresh] = useState(0);
  const [lastSuppressionId, setLastSuppressionId] = useState<number | null>(null);
  const [activeSuppressions, setActiveSuppressions] = useState<DetectionSuppression[]>([]);
  const [lastIdentityEditId, setLastIdentityEditId] = useState<number | null>(null);
  const [undoHistory, setUndoHistory] = useState<UndoEntry[]>([]);
  const [participantNavigationActive, setParticipantNavigationActive] = useState(false);
  const [participantFocusIndex, setParticipantFocusIndex] = useState(0);
  const [identityNavigationActive, setIdentityNavigationActive] = useState(false);
  const [identityFocusIndex, setIdentityFocusIndex] = useState(0);
  const [selectedAnnotationId, setSelectedAnnotationId] = useState<number | null>(null);
  const [editingAnnotationId, setEditingAnnotationId] = useState<number | null>(null);
  const [shortcutHelpOpen, setShortcutHelpOpen] = useState(false);
  const [draftErrors, setDraftErrors] = useState<string[]>([]);
  const [draftErrorFields, setDraftErrorFields] = useState<Set<DraftField>>(new Set());
  const draftErrorRef = useRef<HTMLDivElement>(null);
  const categorySectionRef = useRef<HTMLDivElement>(null);
  const participantSectionRef = useRef<HTMLDivElement>(null);
  const startButtonRef = useRef<HTMLButtonElement>(null);
  const endButtonRef = useRef<HTMLButtonElement>(null);
  const draftBeforeEditRef = useRef<DraftSnapshot | null>(null);

  const [submitting, setSubmitting] = useState(false);
  const [annotationMutationBusy, setAnnotationMutationBusy] = useState(false);
  const [navigationPending, setNavigationPending] = useState(false);
  const navigationPendingRef = useRef(false);
  const allowNavigationRef = useRef(false);
  const [confirmDialog, confirm] = useConfirm();

  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [hint, setHint] = useState("Tab 切换模式；T 进入 track 列表导航；Space 播放；Ctrl+Enter 保存");

  useEffect(() => {
    setParticipantNavigationActive(false);
    setIdentityNavigationActive(false);
  }, [workspaceMode]);

  // 从片段库跳回标注位置：?t=<秒> 定位播放头
  const [searchParams] = useSearchParams();
  const seekParamRaw = searchParams.get("t");
  const seekParam = Number(seekParamRaw);
  const hasSeekTarget = seekParamRaw != null && Number.isFinite(seekParam);
  const pendingSeekRef = useRef<number | null>(null);
  const handleMediaReady = useCallback((reason: "initial" | "retry-restored", element: HTMLVideoElement) => {
    setElementDuration(element.duration);
    if (reason !== "initial" || pendingSeekRef.current == null) return;
    element.currentTime = Math.min(pendingSeekRef.current, element.duration);
    setCurrentTime(element.currentTime);
    pendingSeekRef.current = null;
  }, []);
  const media = useMediaSource({ videoId: vid, surface: "annotate", videoRef, onReady: handleMediaReady });

  // 同一组件承载相邻视频路由；路由变化时先彻底移除旧视频的交互与临时状态。
  useLayoutEffect(() => {
    loadAllRequestRef.current += 1;
    navigationPendingRef.current = false;
    allowNavigationRef.current = false;
    const element = videoRef.current;
    if (element) {
      element.pause();
      element.currentTime = 0;
    }
    pendingSeekRef.current = null;
    setProject(null);
    setVideo(null);
    setProjectVideos([]);
    setCategories([]);
    setAnnotations([]);
    setLoading(true);
    setElementDuration(0);
    setCurrentTime(0);
    setPlaying(false);
    setActiveCategory(null);
    setStartPoint(null);
    setEndPoint(null);
    setSaveState("idle");
    setWorkspaceMode("behavior");
    setSelectedMouseIds([]);
    setIdentitySelectedMouseIds([]);
    setParticipantRoles({}); setActiveRoleKey(null); setUnlockedRoleKeys(new Set()); setRoleMessage(null);
    setTracks([]);
    setCurrentFrame(0);
    setDetectionImport(null);
    setIdentityRevision(0);
    setIdentitySearch("");
    setShowAllTracks(false);
    setIdentityBusy(false);
    setIdentityEditFeedback(null);
    setOverlayRefresh(0);
    setLastSuppressionId(null);
    setActiveSuppressions([]);
    setLastIdentityEditId(null);
    setUndoHistory([]);
    setParticipantNavigationActive(false);
    setParticipantFocusIndex(0);
    setIdentityNavigationActive(false);
    setIdentityFocusIndex(0);
    setSelectedAnnotationId(null);
    setEditingAnnotationId(null);
    setShortcutHelpOpen(false);
    setDraftErrors([]);
    setDraftErrorFields(new Set());
    draftBeforeEditRef.current = null;
    setSubmitting(false);
    setAnnotationMutationBusy(false);
    setNavigationPending(false);
    setErrorMsg(null);
    setHint("Tab 切换模式；T 进入 track 列表导航；Space 播放；Ctrl+Enter 保存");
  }, [pid, vid]);

  // 供键盘监听读取最新值（避免闭包过期）
  const latest = useRef({ startPoint, endPoint, activeCategory, video });
  latest.current = { startPoint, endPoint, activeCategory, video };

  const categoryById = useMemo(
    () => new Map(categories.map((c) => [c.id, c] as const)),
    [categories]
  );
  const invalidTrackCounts = useMemo(() => annotations.reduce((counts, annotation) => {
    if (annotation.mouse_id_status !== "needs_mouse_ids") return counts;
    if (categoryById.get(annotation.category_id)?.participant_mode === "role_based") counts.roleBased += 1;
    else counts.unordered += 1;
    return counts;
  }, { roleBased: 0, unordered: 0 }), [annotations, categoryById]);

  const displayCategories = useMemo(() => sortCategoriesForDisplay(categories), [categories]);
  const categoryShortcuts = useMemo(() => buildCategoryShortcuts(displayCategories), [displayCategories]);
  const categoryShortcutById = useMemo(
    () => new Map(categoryShortcuts.map(({ key, category }) => [category.id, key] as const)),
    [categoryShortcuts]
  );
  const sortedProjectVideos = useMemo(() => sortVideosForNavigation(projectVideos), [projectVideos]);
  const { previous: previousVideo, next: nextVideo } = useMemo(
    () => getAdjacentVideos(sortedProjectVideos, vid),
    [sortedProjectVideos, vid]
  );
  const currentVideoInList = useMemo(
    () => sortedProjectVideos.some((item) => item.id === vid),
    [sortedProjectVideos, vid]
  );
  const navigationBusy = saveState === "saving" || annotationMutationBusy || submitting || identityBusy || navigationPending;

  useEffect(() => {
    if (import.meta.env.DEV && displayCategories.length > 0) {
      verifyCategoryShortcuts(displayCategories, categoryShortcuts);
    }
  }, [categoryShortcuts, displayCategories]);

  const toggleMouseId = useCallback((id: number) => {
    setSelectedMouseIds((prev) => prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id].sort((a, b) => a - b));
  }, []);
  const toggleIdentityMouseId = useCallback((id: number) => {
    setIdentitySelectedMouseIds((prev) => prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id].sort((a, b) => a - b));
  }, []);

  const roleDefinitions = useMemo(() => activeCategory?.participant_mode === "role_based" ? [...activeCategory.role_definitions].sort((a, b) => a.role_sort_order - b.role_sort_order) : [], [activeCategory]);
  const roleSelectedIds = useMemo(() => [...new Set(Object.values(participantRoles).flat())].sort((a, b) => a - b), [participantRoles]);
  const behaviorSelectedIds = useMemo(() => [...new Set([...selectedMouseIds, ...roleSelectedIds])].sort((a, b) => a - b), [roleSelectedIds, selectedMouseIds]);
  const overlaySelectedIds = workspaceMode === "identity" ? identitySelectedMouseIds : behaviorSelectedIds;
  const trackRoleLabels = useMemo(() => { const map: Record<number, string> = {}; roleDefinitions.forEach((role) => (participantRoles[role.key] ?? []).forEach((id) => { map[id] = role.name; })); return map; }, [participantRoles, roleDefinitions]);

  const activateRole = useCallback((key: string) => {
    const index = roleDefinitions.findIndex((r) => r.key === key);
    if (index < 0) return;
    const allowed = isRoleAccessible(roleDefinitions, participantRoles, unlockedRoleKeys, key);
    if (!allowed) { setRoleMessage("请先补全前序角色的最少参与对象，再进入这个角色。"); return; }
    setUnlockedRoleKeys((keys) => new Set([...keys, key])); setActiveRoleKey(key); setRoleMessage(null);
  }, [participantRoles, roleDefinitions, unlockedRoleKeys]);

  const toggleRoleTrack = useCallback(async (id: number) => {
    const role = roleDefinitions.find((r) => r.key === activeRoleKey);
    if (!role) { setRoleMessage("请先选择一个角色槽位。"); return; }
    const currentKey = roleDefinitions.find((r) => (participantRoles[r.key] ?? []).includes(id))?.key;
    if (currentKey === role.key) { setParticipantRoles((prev) => ({ ...prev, [role.key]: (prev[role.key] ?? []).filter((x) => x !== id) })); setSelectedMouseIds((ids) => [...new Set([...ids, id])].sort((a, b) => a - b)); setRoleMessage(`${role.name}已移除 Track ${id}，已放回待分配。`); return; }
    if ((participantRoles[role.key] ?? []).length >= (role.max_count ?? Number.POSITIVE_INFINITY)) { setRoleMessage(`${role.name}已达到最多 ${role.max_count} 个参与对象，请先移除后再添加。`); return; }
    if (currentKey) {
      const oldRole = roleDefinitions.find((r) => r.key === currentKey)!;
      const ok = await confirm({ title: `移至“${role.name}”？`, message: <>Track {id} 当前属于“<b>{oldRole.name}</b>”。确认后会从原角色移除，并一次性加入当前角色。</>, confirmLabel: "移至当前角色" });
      if (!ok) { setRoleMessage(`Track ${id} 仍保留在“${oldRole.name}”。`); return; }
    }
    setParticipantRoles((prev) => { const next: Record<string, number[]> = {}; roleDefinitions.forEach((r) => { next[r.key] = (prev[r.key] ?? []).filter((x) => x !== id); }); next[role.key] = [...next[role.key], id].sort((a, b) => a - b); return next; });
    setSelectedMouseIds((ids) => ids.filter((trackId) => trackId !== id));
    setRoleMessage(`Track ${id} 已分配给“${role.name}”。`);
  }, [activeRoleKey, confirm, participantRoles, roleDefinitions]);

  const selectCategory = useCallback(async (category: Category) => {
    // 鼠标点击与数字键复用本函数，全局导航不依赖旧按钮焦点。
    blurActiveButton();
    if (activeCategory?.id === category.id) {
      setHint(`当前类别仍为“${category.name}”`);
      return;
    }
    const carriedIds = [...new Set([...selectedMouseIds, ...roleSelectedIds])].sort((a, b) => a - b);
    setActiveCategory(category); setRoleMessage(null);
    if (category.participant_mode === "role_based") {
      const roles = [...category.role_definitions].sort((a, b) => a.role_sort_order - b.role_sort_order);
      const nextRoleKeys = new Set(roles.map((role) => role.key));
      const compatible = activeCategory?.participant_mode === "role_based"
        ? Object.fromEntries(roles.map((role) => [role.key, [...(participantRoles[role.key] ?? [])]]))
        : Object.fromEntries(roles.map((role) => [role.key, []]));
      const retainedRoleIds = new Set(Object.values(compatible).flat());
      const pending = activeCategory?.participant_mode === "role_based"
        ? [...new Set([...selectedMouseIds, ...Object.entries(participantRoles).filter(([key]) => !nextRoleKeys.has(key)).flatMap(([, ids]) => ids)])].filter((id) => !retainedRoleIds.has(id)).sort((a, b) => a - b)
        : carriedIds;
      setSelectedMouseIds(pending);
      setParticipantRoles(compatible); setActiveRoleKey(roles[0]?.key ?? null); setUnlockedRoleKeys(getInitiallyUnlockedRoleKeys(roles, compatible));
      setParticipantNavigationActive(false);
      setHint(retainedRoleIds.size ? `已选择类别“${category.name}”；已保留兼容角色分配` : `已选择类别“${category.name}”；请在角色槽位中分配参与对象`);
      return;
    } else { setSelectedMouseIds(carriedIds); setParticipantRoles({}); setActiveRoleKey(null); setUnlockedRoleKeys(new Set()); }
    setErrorMsg(null);
    if (category.mouse_count_max === 0) {
      setParticipantNavigationActive(false);
      setHint(`已选择类别“${category.name}”；该类别无需选择参与对象`);
      return;
    }
    // detectionImport 由 current 接口返回；非空即表示当前存在 active 导入。
    if (!detectionImport) {
      setParticipantNavigationActive(false);
      setHint(`已选择类别“${category.name}”；没有可用检测结果，无法选择参与对象`);
      return;
    }
    if (tracks.length === 0) {
      setParticipantNavigationActive(false);
      setHint(`已选择类别“${category.name}”；当前没有可选择的参与对象`);
      return;
    }
    setParticipantNavigationActive(false);
    setParticipantFocusIndex(0);
    setHint(`已选择类别“${category.name}”；按 T 进入参与对象键盘选择，用 ↑/↓ 移动、Enter 选择、T 退出`);
  }, [activeCategory, detectionImport, participantRoles, roleSelectedIds, selectedMouseIds, tracks]);

  useEffect(() => {
    if (!participantNavigationActive) return;
    if (tracks.length === 0) {
      setParticipantNavigationActive(false);
      setHint("参与对象列表已为空，已退出键盘选择");
      return;
    }
    setParticipantFocusIndex((index) => Math.min(index, tracks.length - 1));
  }, [participantNavigationActive, tracks]);

  useEffect(() => {
    if (!identityNavigationActive) return;
    if (tracks.length === 0) {
      setIdentityNavigationActive(false);
      setHint("track 列表已为空，已退出键盘导航");
      return;
    }
    setIdentityFocusIndex((index) => Math.min(index, tracks.length - 1));
  }, [identityNavigationActive, tracks]);

  const mouseIdsValid = useCallback((category: Category | null, ids: number[]) => {
    if (!category) return false;
    return ids.length >= category.mouse_count_min && (category.mouse_count_max == null || ids.length <= category.mouse_count_max);
  }, []);

  const handleFrameData = useCallback((data: { frame: number; detections: DetectionWithTrack[]; detectionImport: DetectionImport | null }) => {
    setCurrentFrame(data.frame);
    setDetectionImport(data.detectionImport);
  }, []);

  useEffect(() => {
    if (!detectionImport) { setTracks([]); return; }
    let alive = true;
    getCorrectedTracks(pid, vid, { current_frame: currentFrame, search: identitySearch || undefined, page_size: 200 })
      .then((result) => {
        if (!alive) return;
        const items = showAllTracks || identitySearch
          ? result.items
          : result.items.filter((t) => t.visible_in_current_frame || overlaySelectedIds.includes(t.display_track_id));
        setTracks(items);
      })
      .catch((err: unknown) => { if (alive) setErrorMsg(err instanceof Error ? err.message : "加载 track ID 失败"); });
    return () => { alive = false; };
  }, [pid, vid, detectionImport, currentFrame, identitySearch, showAllTracks, identityRevision, overlaySelectedIds]);

  // 优先使用浏览器实际解析的媒体时长（elementDuration）作为时间轴基准，
  // DB 元数据时长仅作回退：避免元数据 duration 与真实播放时长不一致时时间轴错位。
  const timelineDuration =
    elementDuration > 0 ? elementDuration : video?.duration && video.duration > 0 ? video.duration : null;
  const videoReady = media.status === "ready";
  const effectiveFps = detectionImport?.fps && detectionImport.fps > 0 ? detectionImport.fps : video?.fps && video.fps > 0 ? video.fps : null;
  const authoritativeFrameCount = effectiveFps && timelineDuration
    ? Math.max(1, Math.ceil(timelineDuration * effectiveFps))
    : null;
  const startDisplayTime = startPoint && effectiveFps ? frameToStartTime(startPoint.frame, effectiveFps) : null;
  const endDisplayTime = endPoint && effectiveFps ? frameToEndTime(endPoint.frame, effectiveFps) : null;
  const hasRoleAssignments = roleSelectedIds.length > 0;
  const hasDraft = activeCategory != null || startPoint != null || endPoint != null || selectedMouseIds.length > 0 || hasRoleAssignments;
  const draftIntervalInvalid = startPoint != null && endPoint != null && endPoint.frame <= startPoint.frame;

  const routeBlocker = useBlocker(({ currentLocation, nextLocation }) => (
    hasDraft
    && !allowNavigationRef.current
    && `${currentLocation.pathname}${currentLocation.search}` !== `${nextLocation.pathname}${nextLocation.search}`
  ));
  const routeConfirmingRef = useRef(false);
  useEffect(() => {
    if (routeBlocker.state !== "blocked" || routeConfirmingRef.current) return;
    routeConfirmingRef.current = true;
    void confirm({
      title: "离开当前视频？",
      message: "当前有未保存行为草稿。离开后草稿将被放弃，且不会带入其他视频。",
      confirmLabel: "放弃草稿并离开",
      danger: true,
    }).then((ok) => {
      if (ok) {
        allowNavigationRef.current = true;
        routeBlocker.proceed();
      } else {
        routeBlocker.reset();
      }
    }).finally(() => { routeConfirmingRef.current = false; });
  }, [confirm, routeBlocker]);

  useEffect(() => {
    if (!hasDraft) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [hasDraft]);

  useEffect(() => {
    setDraftErrors([]);
    setDraftErrorFields(new Set());
    setSaveState((state) => state === "error" ? "idle" : state);
  }, [activeCategory, endPoint, participantRoles, selectedMouseIds, startPoint]);

  function changeEditingAnnotation(id: number | null) {
    if (id != null) {
      if (editingAnnotationId == null) {
        draftBeforeEditRef.current = {
          activeCategory,
          startPoint,
          endPoint,
          selectedMouseIds: [...selectedMouseIds],
          participantRoles: Object.fromEntries(Object.entries(participantRoles).map(([key, ids]) => [key, [...ids]])),
          activeRoleKey,
          unlockedRoleKeys: new Set(unlockedRoleKeys),
          roleMessage,
        };
      }
      setStartPoint(null);
      setEndPoint(null);
      setDraftErrors([]);
      setDraftErrorFields(new Set());
      setEditingAnnotationId(id);
      return;
    }
    setEditingAnnotationId(null);
    const snapshot = draftBeforeEditRef.current;
    draftBeforeEditRef.current = null;
    if (!snapshot) return;
    setActiveCategory(snapshot.activeCategory);
    setStartPoint(snapshot.startPoint);
    setEndPoint(snapshot.endPoint);
    setSelectedMouseIds(snapshot.selectedMouseIds);
    setParticipantRoles(snapshot.participantRoles);
    setActiveRoleKey(snapshot.activeRoleKey);
    setUnlockedRoleKeys(snapshot.unlockedRoleKeys);
    setRoleMessage(snapshot.roleMessage);
    setDraftErrors([]);
    setDraftErrorFields(new Set());
  }

  /* ---------- 数据加载 ---------- */
  const loadAnnotations = useCallback(async () => {
    const routeKey = `${pid}:${vid}`;
    try {
      const loadedAnnotations = await listAnnotations(pid, vid);
      if (routeKeyRef.current === routeKey) setAnnotations(loadedAnnotations);
    } catch (err) {
      if (routeKeyRef.current === routeKey) setErrorMsg(err instanceof Error ? err.message : "加载标注失败");
    }
  }, [pid, vid]);

  useEffect(() => {
    if (selectedAnnotationId != null && !annotations.some((annotation) => annotation.id === selectedAnnotationId)) {
      setSelectedAnnotationId(null);
    }
    if (editingAnnotationId != null && !annotations.some((annotation) => annotation.id === editingAnnotationId)) {
      changeEditingAnnotation(null);
    }
  }, [annotations, editingAnnotationId, selectedAnnotationId]);

  useEffect(() => {
    if (editingAnnotationId == null) return;
    const annotation = annotations.find((a) => a.id === editingAnnotationId);
    const category = annotation ? categoryById.get(annotation.category_id) : null;
    if (!annotation || !category) return;
    setActiveCategory(category);
    if (category.participant_mode === "role_based") {
      const roles = [...category.role_definitions].sort((a, b) => a.role_sort_order - b.role_sort_order);
      const complete = Object.fromEntries(roles.map((r) => [r.key, [...(annotation.participant_roles[r.key] ?? [])]]));
      setParticipantRoles(complete); setSelectedMouseIds([]); setActiveRoleKey(roles[0]?.key ?? null);
      setUnlockedRoleKeys(getInitiallyUnlockedRoleKeys(roles, complete));
      setRoleMessage("已恢复这条标注的角色分配，可在槽位中继续调整。");
    } else { setSelectedMouseIds(annotation.mouse_ids); setParticipantRoles({}); setActiveRoleKey(null); }
  }, [annotations, categoryById, editingAnnotationId]);

  const syncSuppressions = useCallback(async () => {
    const routeKey = `${pid}:${vid}`;
    try {
      const suppressions = await listDetectionSuppressions(pid, vid);
      if (routeKeyRef.current !== routeKey) return;
      setActiveSuppressions(suppressions);
      setLastSuppressionId(suppressions[0]?.id ?? null);
    } catch (err) {
      if (routeKeyRef.current === routeKey) setErrorMsg(err instanceof Error ? `操作已完成，但忽略记录同步失败：${err.message}` : "操作已完成，但忽略记录同步失败");
    }
  }, [pid, vid]);

  const loadAll = useCallback(async () => {
    const requestId = ++loadAllRequestRef.current;
    const routeKey = `${pid}:${vid}`;
    setLoading(true);
    try {
      const [projs, vids, cats, anns, suppressions] = await Promise.all([
        listProjects(),
        listVideos(pid),
        listCategories(pid),
        listAnnotations(pid, vid),
        listDetectionSuppressions(pid, vid),
      ]);
      if (loadAllRequestRef.current !== requestId || routeKeyRef.current !== routeKey) return;
      const loadedVideo = vids.find((v) => v.id === vid) ?? null;
      setProject(projs.find((p) => p.id === pid) ?? null);
      setVideo(loadedVideo);
      setProjectVideos(vids);
      setIdentityRevision(loadedVideo?.identity_revision ?? 0);
      setCategories(cats);
      setAnnotations(anns);
      setActiveSuppressions(suppressions);
      setLastSuppressionId(suppressions[0]?.id ?? null);
      setLastIdentityEditId(null);
      setUndoHistory([]);
      setSelectedAnnotationId(null);
      setEditingAnnotationId(null);
      setParticipantNavigationActive(false);
      setErrorMsg(null);
    } catch (err) {
      if (loadAllRequestRef.current === requestId && routeKeyRef.current === routeKey) {
        setErrorMsg(err instanceof Error ? err.message : "加载数据失败");
      }
    } finally {
      if (loadAllRequestRef.current === requestId && routeKeyRef.current === routeKey) setLoading(false);
    }
  }, [pid, vid]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  /* ---------- 视频元数据刷新（标注变更后工作流状态可能回到 draft） ---------- */
  const refreshVideo = useCallback(async () => {
    const routeKey = `${pid}:${vid}`;
    try {
      const vids = await listVideos(pid);
      if (routeKeyRef.current !== routeKey) return;
      setProjectVideos(vids);
      const v = vids.find((item) => item.id === vid) ?? null;
      if (v) {
        setVideo(v);
        setIdentityRevision(v.identity_revision ?? 0);
      }
    } catch {
      // 元数据刷新失败不阻断标注流程，沿用当前 video 状态
    }
  }, [pid, vid]);

  /* ---------- 工作流守卫 ----------
   * 对已提交 / 已通过 / 已退回的视频执行创建 / 编辑 / 删除标注前，
   * 明确告知后果（退回草稿、审核失效、已有片段删除）；取消则不发请求。
   */
  const guardMutation = useCallback(
    async (action: string): Promise<boolean> => {
      const status = video?.workflow_status ?? "draft";
      if (status === "draft" || !status) return true;
      const label = WORKFLOW_LABELS[status] ?? status;
      return confirm({
        title: `确认${action}？`,
        message: (
          <>
            该视频当前为「<b>{label}</b>」（行为标注版本 v{video?.annotation_revision ?? 1}）。
            修改行为标注将使其退回<b>草稿</b>，已有审核结果将<b>失效</b>，已有视频片段（如有）将被删除，需要重新提交审核。
          </>
        ),
        confirmLabel: `仍要${action}`,
        danger: true,
      });
    },
    [video, confirm]
  );

  /* ---------- 提交审核 ---------- */
  const handleSubmitReview = useCallback(async () => {
    if (!video) return;
    if (annotations.length === 0) {
      setErrorMsg("至少需要一条行为标注才能提交审核");
      return;
    }
    const roleInvalid = annotations.filter((a) => a.participant_status === "needs_participants");
    const trackInvalid = invalidTrackCounts.roleBased + invalidTrackCounts.unordered;
    if (!detectionImport || roleInvalid.length > 0 || trackInvalid > 0) {
      if (!detectionImport) setErrorMsg("缺少有效 YOLO 检测数据，不能提交审核");
      else setErrorMsg([
        roleInvalid.length ? `${roleInvalid.length} 条角色待补全` : "",
        invalidTrackCounts.roleBased ? `${invalidTrackCounts.roleBased} 条 Track 已失效，需要重新分配` : "",
        invalidTrackCounts.unordered ? `${invalidTrackCounts.unordered} 条 Track 已失效，需要重新选择` : "",
      ].filter(Boolean).join("；"));
      return;
    }
    const ok = await confirm({
      title: "提交审核？",
      message: (
        <>
          提交后该视频将进入审核队列，审核通过前标注将被锁定（修改需经确认并退回草稿）。
          <br />
          当前共 <b>{annotations.length}</b> 条行为标注，行为标注版本 <b>v{video.annotation_revision ?? 1}</b>。
        </>
      ),
      confirmLabel: "提交审核",
    });
    if (!ok) return;
    setSubmitting(true);
    setErrorMsg(null);
    try {
      const updated = await submitVideoForReview(pid, vid);
      setVideo(updated);
      setHint("已提交审核，等待审核人处理");
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "提交审核失败");
    } finally {
      setSubmitting(false);
    }
  }, [video, annotations, pid, vid, confirm, detectionImport, invalidTrackCounts]);

  /* ---------- 从片段库带参跳转：定位到目标时间 ---------- */
  useEffect(() => {
    if (!hasSeekTarget) return;
    pendingSeekRef.current = seekParam;
  }, [hasSeekTarget, seekParam]);

  /* ---------- 保存状态自动复位 ---------- */
  useEffect(() => {
    if (saveState !== "saved") return;
    const t = window.setTimeout(() => setSaveState("idle"), 3000);
    return () => window.clearTimeout(t);
  }, [saveState]);

  /* ---------- 播放控制 ---------- */
  function togglePlay() {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) void v.play();
    else v.pause();
  }

  function step(dir: 1 | -1, frames = 1) {
    const v = videoRef.current;
    if (!v) return;
    const fps = latest.current.video?.fps && latest.current.video.fps > 0 ? latest.current.video.fps : 30;
    const dt = frames / fps;
    const next = Math.min(Math.max(0, v.currentTime + dir * dt), v.duration || Number.MAX_VALUE);
    v.currentTime = next;
  }

  function seekTo(t: number) {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = Math.min(Math.max(0, t), v.duration || t);
  }

  /* ---------- 标注操作 ---------- */
  function markStart() {
    if (!videoRef.current) return;
    const frame = clampFrame(currentFrame, authoritativeFrameCount);
    setStartPoint({ frame });
    setErrorMsg(null);
    setHint(`开始已设置：帧 ${frame}${effectiveFps ? `（${formatTime(frameToStartTime(frame, effectiveFps))}）` : ""}`);
  }

  function markEnd() {
    if (!videoRef.current) return;
    const frame = clampFrame(currentFrame, authoritativeFrameCount);
    setEndPoint({ frame });
    setErrorMsg(null);
    setHint(`结束已设置：帧 ${frame}${effectiveFps ? `（半开边界 ${formatTime(frameToEndTime(frame, effectiveFps))}）` : ""}；保存时统一校验`);
  }

  async function saveDraft() {
    if (saveState === "saving" || annotationMutationBusy) return;
    const cat = latest.current.activeCategory;
    const sp = latest.current.startPoint;
    const ep = latest.current.endPoint;
    const errors: string[] = [];
    const errorFields = new Set<DraftField>();
    let firstTarget: HTMLElement | null = null;
    if (!cat) {
      errors.push("请选择行为类别");
      errorFields.add("category");
      firstTarget ??= categorySectionRef.current;
    }
    if (!sp) {
      errors.push("请设置开始点");
      errorFields.add("start");
      firstTarget ??= startButtonRef.current;
    }
    if (!ep) {
      errors.push("请设置结束点");
      errorFields.add("end");
      firstTarget ??= endButtonRef.current;
    }
    if (!effectiveFps) errors.push("视频 FPS 无效，无法由帧派生规范时间");
    if (sp && (sp.frame < 0 || (authoritativeFrameCount != null && sp.frame >= authoritativeFrameCount))) { errors.push("开始帧超出视频范围"); errorFields.add("start"); firstTarget ??= startButtonRef.current; }
    if (ep && (ep.frame < 0 || (authoritativeFrameCount != null && ep.frame >= authoritativeFrameCount))) { errors.push("结束帧超出视频范围"); errorFields.add("end"); firstTarget ??= endButtonRef.current; }
    if (sp && ep && ep.frame <= sp.frame) { errors.push("结束帧必须大于开始帧，单帧行为不能保存"); errorFields.add("end"); firstTarget ??= endButtonRef.current; }
    if (!detectionImport) errors.push("缺少有效 YOLO 检测数据");

    let participantIds: number[] = [];
    if (cat?.participant_mode === "role_based") {
      const seen = new Set<number>();
      for (const role of cat.role_definitions) {
        const ids = participantRoles[role.key] ?? [];
        if (ids.length < role.min_count || (role.max_count != null && ids.length > role.max_count)) {
          errors.push(`角色“${role.name}”需要${role.max_count == null ? `至少 ${role.min_count}` : role.min_count === role.max_count ? `${role.min_count}` : `${role.min_count}–${role.max_count}`}个参与对象`);
          errorFields.add("participants");
          firstTarget ??= participantSectionRef.current;
        }
        for (const id of ids) {
          if (seen.has(id)) { errors.push(`Track ${id} 不能跨角色重复分配`); errorFields.add("participants"); }
          seen.add(id);
        }
      }
      participantIds = [...seen];
      if (selectedMouseIds.length) {
        errors.push(`还有 ${selectedMouseIds.length} 个 Track 待分配到角色`);
        errorFields.add("participants");
        firstTarget ??= participantSectionRef.current;
      }
    } else if (cat) {
      participantIds = selectedMouseIds;
      if (!mouseIdsValid(cat, participantIds)) {
        const max = cat.mouse_count_max;
        errors.push(`“${cat.name}”需要${max === cat.mouse_count_min ? `恰好 ${cat.mouse_count_min}` : max == null ? `至少 ${cat.mouse_count_min}` : `${cat.mouse_count_min}–${max}`}个参与对象，当前为 ${participantIds.length} 个`);
        errorFields.add("participants");
        firstTarget ??= participantSectionRef.current;
      }
    }
    if (sp && ep && ep.frame > sp.frame) {
      for (const id of participantIds) {
        const track = tracks.find((item) => item.display_track_id === id);
        if (track && (track.first_frame == null || track.last_frame == null || track.last_frame < sp.frame || track.first_frame > ep.frame)) {
          errors.push(`Track ${id} 在所选帧区间内没有有效检测`);
          errorFields.add("participants");
          firstTarget ??= participantSectionRef.current;
        }
      }
    }
    if (errors.length) {
      setDraftErrors([...new Set(errors)]);
      setDraftErrorFields(errorFields);
      setSaveState("error");
      window.requestAnimationFrame(() => (firstTarget ?? draftErrorRef.current)?.focus());
      return;
    }
    if (!cat || !sp || !ep || !effectiveFps) return;
    // 已提交 / 已通过 / 已退回的视频新增标注前需确认（会退回草稿、审核失效）
    if (!(await guardMutation("新增行为标注"))) {
      setHint("已取消保存，未保存行为草稿保持不变");
      return;
    }
    setSaveState("saving");
    setAnnotationMutationBusy(true);
    try {
      await createAnnotation(pid, vid, {
        category_id: cat.id,
        start_time: frameToStartTime(sp.frame, effectiveFps),
        end_time: frameToEndTime(ep.frame, effectiveFps),
        start_frame: sp.frame,
        end_frame: ep.frame,
        confidence: "certain",
        ...(cat.participant_mode === "role_based" ? { participant_roles: Object.fromEntries(cat.role_definitions.map((role) => [role.key, [...(participantRoles[role.key] ?? [])].sort((a, b) => a - b)])) } : detectionImport ? {
          mouse_ids: selectedMouseIds,
          detection_import_revision: detectionImport.revision,
          identity_revision: identityRevision,
        } : {}),
        ...(cat.participant_mode === "role_based" && detectionImport ? { detection_import_revision: detectionImport.revision, identity_revision: identityRevision } : {}),
      });
      setSaveState("saved");
      setStartPoint(null);
      setEndPoint(null);
      setSelectedMouseIds([]);
      if (cat.participant_mode === "role_based") {
        const roles = [...cat.role_definitions].sort((a, b) => a.role_sort_order - b.role_sort_order);
        const empty = Object.fromEntries(roles.map((role) => [role.key, []]));
        setParticipantRoles(empty);
        setActiveRoleKey(roles[0]?.key ?? null);
        setUnlockedRoleKeys(getInitiallyUnlockedRoleKeys(roles, empty));
      } else {
        setParticipantRoles({});
      }
      setParticipantNavigationActive(false);
      setDraftErrors([]);
      setDraftErrorFields(new Set());
      setErrorMsg(null);
      const wasLocked = (video?.workflow_status ?? "draft") !== "draft";
      setHint(
        wasLocked
          ? `行为已保存：${cat.name}，帧 ${sp.frame}→${ep.frame}（视频已退回草稿，请重新提交审核）`
          : `行为已保存：${cat.name}，帧 ${sp.frame}→${ep.frame}`
      );
      await loadAnnotations();
      if (wasLocked) await refreshVideo();
    } catch (err) {
      setSaveState("error");
      const messages = draftApiErrorMessages(err);
      setDraftErrors(messages);
      setDraftErrorFields(inferDraftErrorFields(messages));
      setErrorMsg(null);
      window.requestAnimationFrame(() => draftErrorRef.current?.focus());
    } finally {
      setAnnotationMutationBusy(false);
    }
  }

  async function runIdentityEdit(operation: "split" | "merge") {
    if (!detectionImport) return;
    const operationRouteKey = `${pid}:${vid}`;
    const request = {
      operation,
      track_ids: identitySelectedMouseIds,
      frame: operation === "split" ? currentFrame : undefined,
      base_identity_revision: identityRevision,
      base_detection_import_revision: detectionImport.revision,
    };
    setIdentityBusy(true); setErrorMsg(null);
    try {
      const check = await checkIdentityEdit(pid, vid, request);
      if (check.conflicts?.length) { setErrorMsg(`${check.message ?? "角色分配冲突"}：${check.conflicts.map((c) => `标注 #${c.annotation_id} · 帧 ${c.start_frame}–${c.end_frame} / ${c.start_time}–${c.end_time} 秒 · ${c.role_name ?? c.role_key} · Track ${c.track_id}`).join("；")}`); return; }
      if (check.conflict_frames?.length) { setErrorMsg(`Merge 冲突：帧 ${check.conflict_frames.slice(0, 12).join("、")} 同一 track ID 将对应多个框，请先忽略误检框`); return; }
      const ok = await confirm({
        title: operation === "split" ? `Split track ID ${identitySelectedMouseIds[0]}？` : `Merge ${identitySelectedMouseIds.length} 个 track？`,
        message: <>{operation === "split" ? <>从帧 <b>{currentFrame}</b> 起生成新 track ID，前后分别有 {check.detections_before} / {check.detections_after} 个检测。</> : <>保留 track ID <b>{check.retained_display_track_id}</b>，影响 {check.affected_detection_count} 个检测。</>}<br />受影响行为标注：<b>{check.affected_annotation_count}</b> 条。</>,
        confirmLabel: operation === "split" ? "确认 Split" : "确认 Merge",
      });
      if (!ok) return;
      const result = await commitIdentityEdit(pid, vid, request);
      setIdentityRevision(result.identity_revision);
      setVideo((v) => v ? { ...v, identity_revision: result.identity_revision } : v);
      setIdentitySelectedMouseIds(result.new_display_track_id != null ? [result.new_display_track_id] : result.retained_display_track_id != null ? [result.retained_display_track_id] : []);
      if (result.edit_id) {
        setLastIdentityEditId(result.edit_id);
        setUndoHistory((history) => [...history, { kind: "identity", id: result.edit_id!, createdAt: Date.now() }]);
      } else {
        setHint("track 修正已完成，但服务端未返回可撤销 ID；本次操作不能通过 Ctrl+Z 撤销");
      }
      const feedbackText = buildIdentityEditFeedback(operation, identitySelectedMouseIds, currentFrame, result);
      if (routeKeyRef.current === operationRouteKey) {
        setIdentityEditFeedback({ text: feedbackText, key: Date.now(), routeKey: operationRouteKey });
      }
      setOverlayRefresh((x) => x + 1);
      await loadAnnotations();
      if (result.edit_id) setHint("track 修正已提交；受影响行为标注和审核状态已刷新");
    } catch (err) { setErrorMsg(err instanceof Error ? err.message : "track 修正失败"); }
    finally { setIdentityBusy(false); }
  }

  async function suppressTrack() {
    if (!detectionImport || identitySelectedMouseIds.length !== 1) return;
    const selectedId = identitySelectedMouseIds[0];
    const ok = await confirm({ title: `忽略整个 track（track ID ${selectedId}）？`, message: <>此操作会将所选内容标记为误检并忽略；不会删除原始检测数据。忽略的数据不再参与显示、校验和导出，且可撤销。</>, confirmLabel: "标记为误检并忽略", danger: true });
    if (!ok) return;
    setIdentityBusy(true);
    try {
      const result = await createSuppression(pid, vid, { scope: "corrected_track", track_id: selectedId, base_identity_revision: identityRevision, base_detection_import_revision: detectionImport.revision });
      setIdentityRevision(result.identity_revision); setVideo((v) => v ? { ...v, identity_revision: result.identity_revision } : v); setLastSuppressionId(result.suppression_id ?? null); setIdentitySelectedMouseIds([]); setOverlayRefresh((x) => x + 1); await loadAnnotations();
      if (result.suppression_id != null) {
        setUndoHistory((history) => [...history, { kind: "suppression", id: result.suppression_id!, createdAt: Date.now() }]);
      } else {
        setHint("track 已忽略，但服务端未返回可撤销 ID；本次操作不能通过 Ctrl+Z 撤销");
      }
      await syncSuppressions();
    } catch (err) { setErrorMsg(err instanceof Error ? err.message : "忽略检测失败"); }
    finally { setIdentityBusy(false); }
  }

  async function revertLastSuppression(suppressionId = lastSuppressionId ?? undefined): Promise<boolean> {
    if (!detectionImport || suppressionId == null) return false;
    setIdentityBusy(true);
    try {
      const result = await revertSuppression(pid, vid, suppressionId, { base_identity_revision: identityRevision, base_detection_import_revision: detectionImport.revision });
      setIdentityRevision(result.identity_revision); setVideo((v) => v ? { ...v, identity_revision: result.identity_revision } : v); setOverlayRefresh((x) => x + 1); await loadAnnotations();
      setActiveSuppressions((current) => current.filter((s) => s.id !== suppressionId));
      if (lastSuppressionId === suppressionId) setLastSuppressionId(null);
      await syncSuppressions();
      setUndoHistory((history) => history.filter((entry) => !(entry.kind === "suppression" && entry.id === suppressionId)));
      return true;
    } catch (err) { setErrorMsg(err instanceof Error ? err.message : "撤销失败"); return false; }
    finally { setIdentityBusy(false); }
  }

  async function revertLastIdentity(editId = lastIdentityEditId ?? undefined): Promise<boolean> {
    if (!detectionImport || editId == null) return false;
    setIdentityBusy(true);
    try {
      const result = await revertIdentityEdit(pid, vid, editId, { base_identity_revision: identityRevision, base_detection_import_revision: detectionImport.revision });
      setIdentityRevision(result.identity_revision); setVideo((v) => v ? { ...v, identity_revision: result.identity_revision } : v); if (lastIdentityEditId === editId) setLastIdentityEditId(null); setOverlayRefresh((x) => x + 1); await loadAnnotations();
      setUndoHistory((history) => history.filter((entry) => !(entry.kind === "identity" && entry.id === editId)));
      return true;
    } catch (err) { setErrorMsg(err instanceof Error ? err.message : "撤销 Split / Merge 失败"); return false; }
    finally { setIdentityBusy(false); }
  }

  async function undoLatestTrackEdit() {
    if (identityBusy) return;
    const latestEntry = undoHistory[undoHistory.length - 1];
    if (!latestEntry) {
      setHint("当前页面会话没有可统一撤销的 track 修正；刷新前的 Split / Merge 历史无法恢复");
      return;
    }
    const ok = latestEntry.kind === "identity"
      ? await revertLastIdentity(latestEntry.id)
      : await revertLastSuppression(latestEntry.id);
    if (ok) setHint("已撤销当前页面会话中最近一次 track 修正");
  }

  /* ---------- 列表编辑 / 删除 / 导出 ---------- */
  async function handleEditSave(id: number, patch: AnnotationPatchInput) {
    if (!(await guardMutation("编辑行为标注"))) return;
    setSaveState("saving");
    setAnnotationMutationBusy(true);
    try {
      const target = categoryById.get(patch.category_id ?? annotations.find((a) => a.id === id)?.category_id ?? -1);
      const authorityPatch: AnnotationPatchInput = { ...patch };
      if (target?.participant_mode === "role_based") {
        delete authorityPatch.mouse_ids;
        authorityPatch.participant_roles = Object.fromEntries(target.role_definitions.map((role) => [role.key, [...(participantRoles[role.key] ?? [])].sort((a, b) => a - b)]));
      } else { delete authorityPatch.participant_roles; }
      await updateAnnotation(pid, vid, id, authorityPatch);
      setSaveState("saved");
      const wasLocked = (video?.workflow_status ?? "draft") !== "draft";
      setHint(wasLocked ? "行为标注已更新，视频已退回草稿，请重新提交审核" : "行为标注已更新");
      await loadAnnotations();
      if (wasLocked) await refreshVideo();
    } catch (err) {
      setSaveState("error");
      setErrorMsg(err instanceof Error ? err.message : "保存行为标注失败");
      throw err;
    } finally {
      setAnnotationMutationBusy(false);
    }
  }

  async function handleEditCategoryChange(category: Category): Promise<boolean> {
    if (activeCategory?.id === category.id) return true;
    if (roleSelectedIds.length > 0 && !await confirm({ title: "切换类别并清空角色分配？", message: "这条标注已有参与对象角色分配。切换类别后将清空现有分配。", confirmLabel: "清空并切换", danger: true })) return false;
    setActiveCategory(category); setSelectedMouseIds([]); setRoleMessage(null);
    if (category.participant_mode === "role_based") { const roles = [...category.role_definitions].sort((a, b) => a.role_sort_order - b.role_sort_order); const empty = Object.fromEntries(roles.map((r) => [r.key, []])); setParticipantRoles(empty); setActiveRoleKey(roles[0]?.key ?? null); setUnlockedRoleKeys(getInitiallyUnlockedRoleKeys(roles, empty)); }
    else { setParticipantRoles({}); setActiveRoleKey(null); setUnlockedRoleKeys(new Set()); }
    return true;
  }

  async function handleDelete(ann: Annotation) {
    const item = `${ann.category_name ?? `#${ann.category_id}`}（${formatTimeShort(ann.start_time)}–${formatTimeShort(ann.end_time)}）`;
    const status = video?.workflow_status ?? "draft";
    const locked = status !== "draft" && status !== "";
    const ok = await confirm({
      title: "删除行为标注？",
      message: locked ? (
        <>
          将删除行为标注「{item}」。该视频当前为「<b>{WORKFLOW_LABELS[status] ?? "未知状态"}</b>」状态：
          删除后视频将退回<b>草稿</b>，已有审核结果失效，已有视频片段（如有）将被删除。
        </>
      ) : (
        <>将删除行为标注「{item}」，此操作不可撤销。</>
      ),
      confirmLabel: "删除",
      danger: true,
    });
    if (!ok) return;
    setSaveState("saving");
    setAnnotationMutationBusy(true);
    try {
      await deleteAnnotation(pid, vid, ann.id);
      setSaveState("saved");
      setHint(locked ? "行为标注已删除，视频已退回草稿，请重新提交审核" : "行为标注已删除");
      await loadAnnotations();
      if (locked) await refreshVideo();
    } catch (err) {
      setSaveState("error");
      setErrorMsg(err instanceof Error ? err.message : "删除行为标注失败");
    } finally {
      setAnnotationMutationBusy(false);
    }
  }

  async function handleExport() {
    setErrorMsg(null);
    try {
      const events = await exportAnnotations(pid, vid);
      const blob = new Blob([JSON.stringify(events, null, 2)], { type: "application/json;charset=utf-8" });
      const base = video?.filename?.replace(/\.[^/.]+$/, "") ?? `video_${vid}`;
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = `${base}_annotations.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      // 延迟回收 object URL，确保下载请求已完成携带 blob（部分浏览器同步 revoke 会中断下载）
      window.setTimeout(() => URL.revokeObjectURL(link.href), 1000);
      setHint(`已导出 ${events.length} 条行为事件 JSON`);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "导出失败");
    }
  }

  function clearDraft(keepCategory = false) {
    const retainedCategory = keepCategory ? activeCategory : null;
    setStartPoint(null);
    setEndPoint(null);
    setSelectedMouseIds([]);
    setDraftErrors([]);
    setDraftErrorFields(new Set());
    setRoleMessage(null);
    setParticipantNavigationActive(false);
    setActiveCategory(retainedCategory);
    if (retainedCategory?.participant_mode === "role_based") {
      const roles = [...retainedCategory.role_definitions].sort((a, b) => a.role_sort_order - b.role_sort_order);
      const empty = Object.fromEntries(roles.map((role) => [role.key, []]));
      setParticipantRoles(empty);
      setActiveRoleKey(roles[0]?.key ?? null);
      setUnlockedRoleKeys(getInitiallyUnlockedRoleKeys(roles, empty));
    } else {
      setParticipantRoles({});
      setActiveRoleKey(null);
      setUnlockedRoleKeys(new Set());
    }
  }

  async function requestResetDraft() {
    const ok = await confirm({
      title: "重置标注？",
      message: "将清空当前未保存行为的类别、开始、结束和全部参与对象。此操作不会修改已经保存的行为标注。",
      confirmLabel: "重置标注",
      danger: true,
    });
    if (!ok) return;
    clearDraft(false);
    setHint("已重置当前未保存行为标注");
  }

  const handleVideoNavigation = useCallback(async (direction: "previous" | "next") => {
    if (saveState === "saving" || annotationMutationBusy || submitting || identityBusy) {
      setHint("当前操作尚未完成，请稍后再切换视频");
      return;
    }
    if (navigationPendingRef.current) {
      setHint("正在确认或切换视频，请稍候");
      return;
    }
    const target = direction === "previous" ? previousVideo : nextVideo;
    if (!target) {
      setHint(currentVideoInList
        ? direction === "previous" ? "已到项目视频列表开头" : "已到项目视频列表末尾"
        : "当前视频不在项目视频列表中，无法切换");
      return;
    }
    navigationPendingRef.current = true;
    setNavigationPending(true);
    let didNavigate = false;
    try {
      if (hasDraft || editingAnnotationId != null) {
        const ok = await confirm({
          title: `切换到${direction === "previous" ? "上一个" : "下一个"}视频？`,
          message: <>
            {hasDraft && editingAnnotationId != null
              ? "当前有未保存行为草稿和正在编辑的行为标注，切换后这些内容将丢失。"
              : hasDraft
                ? "当前有未保存行为草稿，切换后草稿将被放弃。"
                : "当前有正在编辑的行为标注，切换后未保存的编辑可能丢失。"}
          </>,
          confirmLabel: "仍要切换",
        });
        if (!ok) return;
      }
      didNavigate = true;
      allowNavigationRef.current = true;
      navigate(`/projects/${pid}/annotate/${target.id}`);
    } finally {
      if (!didNavigate && routeKeyRef.current === `${pid}:${vid}`) {
        navigationPendingRef.current = false;
        setNavigationPending(false);
      }
    }
  }, [annotationMutationBusy, confirm, currentVideoInList, editingAnnotationId, hasDraft, identityBusy, navigate, nextVideo, pid, previousVideo, saveState, submitting, vid]);

  /* ---------- 键盘快捷键（输入聚焦时不触发） ----------
   * 处理函数通过 ref 持有最新闭包：effect 仅注册一次，但每次渲染都更新 ref，
   * 避免路由参数变化（组件未卸载）时 S/D 等快捷键继续作用于旧的 pid/vid。
   */
  const keyHandlerRef = useRef<(e: KeyboardEvent) => void>(() => {});
  keyHandlerRef.current = (e: KeyboardEvent) => {
    const confirmOpen = document.querySelector(".modal-overlay:not(.shortcut-help-overlay)") != null;
    if (confirmOpen) return;

    if (e.key === "Escape") {
      const action = resolveAnnotateEscapeAction({ shortcutHelpOpen, editingAnnotationId, participantNavigationActive, identityNavigationActive, hasDraft });
      if (action == null) return;
      e.preventDefault();
      if (e.repeat) return;
      if (action === "close-help") setShortcutHelpOpen(false);
      else if (action === "cancel-edit") { changeEditingAnnotation(null); setHint("已取消编辑行为标注"); }
      else if (action === "exit-participant-navigation") { setParticipantNavigationActive(false); blurActiveButton(); setHint("已退出参与对象键盘选择；已选参与对象保持不变"); }
      else { setIdentityNavigationActive(false); setHint("已退出 track 列表键盘导航；已选 track 保持不变"); }
      return;
    }

    if (shortcutHelpOpen) {
      if (e.key === "?") {
        e.preventDefault();
        if (!e.repeat) setShortcutHelpOpen(false);
      } else if ((e.key === "Tab" && !e.ctrlKey && !e.altKey && !e.metaKey) || e.code === "Space" || e.code === "Enter") {
        e.preventDefault();
      }
      return;
    }

    if (e.key === "Tab" && !e.ctrlKey && !e.altKey && !e.metaKey) {
      e.preventDefault();
      if (e.repeat) return;
      if (e.shiftKey) return;
      if (workspaceMode === "behavior") {
        if (!detectionImport) {
          setHint("track 修正需要有效的检测导入，当前仍停留在行为标注模式");
          return;
        }
        setWorkspaceMode("identity");
        setHint("已切换到 track 修正模式");
      } else {
        setWorkspaceMode("behavior");
        setHint("已切换到行为标注模式");
      }
      return;
    }

    if (isEditableTarget(e.target)) {
      return;
    }
    const isVideoNavigationShortcut = e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey
      && (e.code === "Comma" || e.code === "Period");
    if (editingAnnotationId != null && !isVideoNavigationShortcut) {
      return;
    }
    if (e.repeat) {
      const navigationActive = participantNavigationActive || identityNavigationActive;
      const managedNavigationKey = navigationActive && (e.code === "ArrowUp" || e.code === "ArrowDown" || e.code === "Enter" || e.code === "Delete");
      const managedPageKey = e.code === "Space" || e.code === "ArrowLeft" || e.code === "ArrowRight" || e.code === "KeyT" || (e.code === "Enter" && e.ctrlKey) || isVideoNavigationShortcut;
      if (managedNavigationKey || managedPageKey) e.preventDefault();
      return;
    }

    if (e.key === "?") {
      e.preventDefault();
      setShortcutHelpOpen(true);
      return;
    }

    if (e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey && (e.code === "Comma" || e.code === "Period")) {
      e.preventDefault();
      void handleVideoNavigation(e.code === "Comma" ? "previous" : "next");
      return;
    }

    if (e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey && e.code === "Enter" && workspaceMode === "behavior") {
      e.preventDefault();
      void saveDraft();
      return;
    }

    if (!e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey && e.code === "KeyT") {
      e.preventDefault();
      if (workspaceMode === "behavior") {
        if (participantNavigationActive) {
          setParticipantNavigationActive(false);
          setHint("已退出参与对象键盘选择；已选参与对象保持不变");
          return;
        }
        if (!activeCategory) {
          setHint("请先选择行为类别，再按 T 进入参与对象键盘选择");
          return;
        }
        if (activeCategory.participant_mode === "role_based") {
          setHint("当前类别使用角色槽位分配参与对象，请使用角色槽位和 Track 按钮");
          return;
        }
        if (activeCategory.mouse_count_max === 0) {
          setHint("当前类别无需选择参与对象");
          return;
        }
        if (!detectionImport) {
          setHint("没有可用检测结果，无法进入参与对象键盘选择");
          return;
        }
        if (tracks.length === 0) {
          setHint("当前没有可选择的参与对象");
          return;
        }
        setParticipantFocusIndex(0);
        setParticipantNavigationActive(true);
        setHint("参与对象键盘选择中：↑/↓ 移动，Enter 选择，T 或 Esc 退出");
      } else {
        if (identityNavigationActive) {
          setIdentityNavigationActive(false);
          setHint("已退出 track 列表键盘导航；已选 track 保持不变");
          return;
        }
        if (!detectionImport) {
          setHint("track 修正需要有效的检测导入");
          return;
        }
        if (tracks.length === 0) {
          setHint("当前没有可导航的 track");
          return;
        }
        setIdentityFocusIndex(0);
        setIdentityNavigationActive(true);
        setHint("track 列表键盘导航中：↑/↓ 移动，Enter 选择，T 或 Esc 退出");
      }
      return;
    }

    if (participantNavigationActive && workspaceMode === "behavior") {
      if (e.code === "ArrowUp" || e.code === "ArrowDown") {
        e.preventDefault();
        if (tracks.length > 0) {
          const delta = e.code === "ArrowUp" ? -1 : 1;
          setParticipantFocusIndex((index) => Math.max(0, Math.min(tracks.length - 1, index + delta)));
        }
        return;
      }
      if (!e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey && e.code === "Enter") {
        e.preventDefault();
        const track = tracks[participantFocusIndex];
        if (track) toggleMouseId(track.display_track_id);
        return;
      }
      if (e.code === "Delete") {
        e.preventDefault();
        return;
      }
    }

    if (identityNavigationActive && workspaceMode === "identity") {
      if (e.code === "ArrowUp" || e.code === "ArrowDown") {
        e.preventDefault();
        if (tracks.length > 0) {
          const delta = e.code === "ArrowUp" ? -1 : 1;
          setIdentityFocusIndex((index) => Math.max(0, Math.min(tracks.length - 1, index + delta)));
        }
        return;
      }
      if (!e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey && e.code === "Enter") {
        e.preventDefault();
        const track = tracks[identityFocusIndex];
        if (track) toggleIdentityMouseId(track.display_track_id);
        return;
      }
    }

    if (e.code === "Space") {
      e.preventDefault();
      togglePlay();
      return;
    }

    const categoryKey = digitShortcutFromEvent(e);
    if (categoryKey && workspaceMode === "behavior") {
      e.preventDefault();
      const shortcut = categoryShortcuts.find((item) => item.key === categoryKey);
      if (!videoReady) {
        setHint("视频尚未就绪，暂不能用数字键选择类别");
      } else if (shortcut) {
        selectCategory(shortcut.category);
      } else {
        setHint(`数字键 ${categoryKey} 当前没有映射行为类别`);
      }
      return;
    }

    if (e.code === "Delete") {
      e.preventDefault();
      if (workspaceMode === "behavior") {
        const annotation = annotations.find((item) => item.id === selectedAnnotationId);
        if (!annotation) {
          setHint("请先点击选择一条行为标注，再按 Delete");
          return;
        }
        void handleDelete(annotation);
      } else if (!detectionImport || identitySelectedMouseIds.length !== 1 || identityBusy) {
        setHint("忽略整个 track 需要恰好选择 1 个有效 track ID");
      } else {
        void suppressTrack();
      }
      return;
    }

    if (e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey && e.code === "KeyZ") {
      if (workspaceMode === "identity") {
        e.preventDefault();
        void undoLatestTrackEdit();
      }
      return;
    }

    if (!e.ctrlKey && !e.altKey && !e.metaKey) {
      if (e.code === "ArrowLeft" || e.code === "ArrowRight") {
        e.preventDefault();
        step(e.code === "ArrowLeft" ? -1 : 1, e.shiftKey ? 10 : 1);
        return;
      }
      if (!e.shiftKey && workspaceMode === "behavior" && e.code === "KeyS") {
        e.preventDefault();
        markStart();
        return;
      }
      if (!e.shiftKey && workspaceMode === "behavior" && e.code === "KeyD") {
        e.preventDefault();
        void markEnd();
        return;
      }
    }

  };

  useEffect(() => {
    const handler = (e: KeyboardEvent) => keyHandlerRef.current(e);
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  const previousVideoTitle = previousVideo
    ? `上一个视频：${previousVideo.filename}（快捷键：上一个《 / Shift+Comma）${navigationBusy ? "；当前操作完成后可切换" : ""}`
    : currentVideoInList
      ? "已到项目视频列表开头，没有上一个视频"
      : "当前视频不在项目视频列表中，无法定位上一个视频";
  const nextVideoTitle = nextVideo
    ? `下一个视频：${nextVideo.filename}（快捷键：下一个》 / Shift+Period）${navigationBusy ? "；当前操作完成后可切换" : ""}`
    : currentVideoInList
      ? "已到项目视频列表末尾，没有下一个视频"
      : "当前视频不在项目视频列表中，无法定位下一个视频";
  const submitBlockedReason = useMemo(() => {
    if (annotations.length === 0) return "至少需要一条行为标注才能提交审核";
    if (!detectionImport) return "缺少有效 YOLO 检测数据，不能提交审核";
    const roleInvalid = annotations.filter((annotation) => annotation.participant_status === "needs_participants").length;
    const trackInvalid = invalidTrackCounts.roleBased + invalidTrackCounts.unordered;
    if (!roleInvalid && !trackInvalid) return null;
    return [
      roleInvalid ? `${roleInvalid} 条角色待补全` : "",
      invalidTrackCounts.roleBased ? `${invalidTrackCounts.roleBased} 条 Track 已失效，需要重新分配` : "",
      invalidTrackCounts.unordered ? `${invalidTrackCounts.unordered} 条 Track 已失效，需要重新选择` : "",
    ].filter(Boolean).join("；");
  }, [annotations, detectionImport, invalidTrackCounts]);
  const visibleIdentityEditFeedback = identityEditFeedbackForRoute(identityEditFeedback, `${pid}:${vid}`);

  return (
    <div className="annotate-page">
      <div className="annotate-header">
        <Link to={`/projects/${pid}/videos`} className="btn btn-sm btn-ghost" title="返回视频库">
          ← 视频库
        </Link>
        <h1>
          <Link to={`/projects/${pid}/videos`} className="crumb-link" title={project?.name ?? undefined}>
            {project?.name ?? `项目 #${pid}`}
          </Link>
          <span className="crumb-sep">/</span>
          <span className="crumb-current" title={video?.filename ?? undefined}>
            {video?.filename ?? `视频 #${vid}`}
          </span>
        </h1>
        <nav className="annotate-video-nav" aria-label="视频导航">
          <button
            type="button"
            className="btn btn-sm annotate-video-nav-previous"
            disabled={navigationBusy || previousVideo == null}
            title={previousVideoTitle}
            onClick={() => void handleVideoNavigation("previous")}
          >
            <span className="annotate-video-nav-arrow" aria-hidden="true">‹</span>
            <span>上一个视频</span>
            <kbd className="annotate-video-nav-key" aria-hidden="true">⇧ ,</kbd>
          </button>
          <button
            type="button"
            className="btn btn-sm annotate-video-nav-next"
            disabled={navigationBusy || nextVideo == null}
            title={nextVideoTitle}
            onClick={() => void handleVideoNavigation("next")}
          >
            <span>下一个视频</span>
            <kbd className="annotate-video-nav-key" aria-hidden="true">⇧ .</kbd>
            <span className="annotate-video-nav-arrow" aria-hidden="true">›</span>
          </button>
        </nav>
        {video ? (
          <div className="workflow-chip" title="视频工作流状态与行为标注版本">
            <WorkflowBadge value={video.workflow_status} revision={video.annotation_revision} />
            {video.workflow_status === "submitted" && video.submitted_at ? (
              <span className="workflow-meta">提交于 {formatDate(video.submitted_at)}</span>
            ) : null}
            {video.workflow_status === "approved" && video.approved_at ? (
              <span className="workflow-meta">通过于 {formatDate(video.approved_at)}</span>
            ) : null}
            {video.workflow_status === "rejected" ? (
              <span className="workflow-meta">请修改后重新提交</span>
            ) : null}
          </div>
        ) : null}
        <div className="actions">
          <button type="button" className="btn btn-sm" onClick={() => setShortcutHelpOpen(true)}>
            快捷键 [?]
          </button>
          <SubmitReviewControl
            status={video?.workflow_status ?? "draft"}
            hasAnnotations={annotations.length > 0}
            blockedReason={submitBlockedReason}
            submitting={submitting}
            onSubmit={() => void handleSubmitReview()}
          />
          <button type="button" className="btn btn-sm" onClick={() => void loadAnnotations()}>
            刷新行为标注
          </button>
          <button type="button" className="btn btn-sm" onClick={() => void handleExport()}>
            导出 JSON
          </button>
        </div>
      </div>

      {errorMsg ? <div className="error-box" role="alert">⚠ {errorMsg}</div> : null}
      {invalidTrackCounts.roleBased > 0 ? <div className="mouse-warning-banner" role="status">⚠ 有 {invalidTrackCounts.roleBased} 条行为标注的 Track 已失效，需要重新分配；完成前不能提交审核。</div> : null}
      {invalidTrackCounts.unordered > 0 ? <div className="mouse-warning-banner" role="status">⚠ 有 {invalidTrackCounts.unordered} 条行为标注的 Track 已失效，需要重新选择；完成前不能提交审核。</div> : null}
      {annotations.some((a) => a.participant_status === "needs_participants") ? <div className="mouse-warning-banner role-warning" role="status">⚠ 有 {annotations.filter((a) => a.participant_status === "needs_participants").length} 条行为标注角色待补全；草稿可继续保存，补全前不能提交审核。</div> : null}
      {confirmDialog}
      {shortcutHelpOpen ? <ShortcutHelp mode={workspaceMode} categoryShortcuts={categoryShortcuts} onClose={() => setShortcutHelpOpen(false)} /> : null}

      <div className="annotate-body">
        <section className="annotate-main">
          <div className="card player-card">
            <div className="video-wrap">
              <video
                ref={videoRef}
                className={videoReady && !loading ? "" : "media-player-pending"}
                onClick={togglePlay}
                title="点击播放 / 暂停 [Space]"
                onTimeUpdate={(e) => setCurrentTime(e.currentTarget.currentTime)}
                onPlay={() => setPlaying(true)}
                onPause={() => setPlaying(false)}
                playsInline
                preload="metadata"
              />
              {videoReady && !loading ? <DetectionOverlay
                    projectId={pid}
                    videoId={vid}
                    video={videoRef.current}
                    currentTime={currentTime}
                    fallbackFps={video?.fps}
                    selectedIds={overlaySelectedIds}
                    trackRoleLabels={trackRoleLabels}
                    interactive
                    onToggleTrack={(id) => {
                      if (workspaceMode === "identity") toggleIdentityMouseId(id);
                      else if (activeCategory?.participant_mode === "role_based") void toggleRoleTrack(id);
                      else toggleMouseId(id);
                    }}
                    onFrameData={handleFrameData}
                    refreshKey={overlayRefresh}
              /> : null}
              {loading ? <div className="media-status-overlay"><Loading text="加载标注数据…" /></div> : <MediaLoadProgress state={media} onCancel={media.cancel} />}
              {!loading && (media.status === "pending" || media.status === "failed" || media.status === "cancelled") ? <div className="media-status-overlay"><EmptyState compact title={media.status === "pending" ? "播放资源处理中" : media.status === "cancelled" ? "下载已取消" : "视频下载失败"} hint={media.message} /><button type="button" className="btn btn-sm" onClick={media.reload}>{media.status === "cancelled" ? "重新下载" : "重试"}</button></div> : null}
            </div>
            {!loading && videoReady ? (
              <>

                <div className="player-controls">
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={(e) => {
                      e.currentTarget.blur();
                      togglePlay();
                    }}
                  >
                    {playing ? "⏸ 暂停 [Space]" : "▶ 播放 [Space]"}
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={(e) => {
                      e.currentTarget.blur();
                      step(-1);
                    }}
                  >
                    ⟨ 退一帧 [←]
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={(e) => {
                      e.currentTarget.blur();
                      step(1);
                    }}
                  >
                    进一帧 [→] ⟩
                  </button>
                  <span className="time-display">
                    <b>{formatTime(currentTime)}</b> / {timelineDuration ? formatTime(timelineDuration) : "?"}
                  </span>
                  <span className="flex-spacer" />
                  <div className="workspace-tabs" role="tablist" aria-label="标注工作模式">
                    <button id="behavior-tab" type="button" role="tab" aria-selected={workspaceMode === "behavior"} aria-controls="behavior-panel" tabIndex={workspaceMode === "behavior" ? 0 : -1} className={workspaceMode === "behavior" ? "active" : ""} onClick={() => setWorkspaceMode("behavior")} onKeyDown={(e) => { if (e.key === "ArrowRight" && detectionImport) { e.preventDefault(); e.stopPropagation(); setWorkspaceMode("identity"); document.getElementById("identity-tab")?.focus(); } }}>行为标注</button>
                    <button id="identity-tab" type="button" role="tab" aria-selected={workspaceMode === "identity"} aria-controls="identity-panel" tabIndex={workspaceMode === "identity" ? 0 : -1} className={workspaceMode === "identity" ? "active" : ""} disabled={!detectionImport} onClick={() => setWorkspaceMode("identity")} onKeyDown={(e) => { if (e.key === "ArrowLeft") { e.preventDefault(); e.stopPropagation(); setWorkspaceMode("behavior"); document.getElementById("behavior-tab")?.focus(); } }}>track 修正</button>
                  </div>
                  <button
                    ref={startButtonRef}
                    type="button"
                    className={`${startPoint ? "btn btn-sm btn-point armed" : "btn btn-sm btn-point"}${draftErrorFields.has("start") ? " draft-field-error" : ""}`}
                    disabled={workspaceMode === "identity"}
                    aria-invalid={draftErrorFields.has("start") || undefined}
                    aria-describedby={draftErrorFields.has("start") ? "draft-error-summary" : undefined}
                    onClick={(e) => {
                      e.currentTarget.blur();
                      markStart();
                    }}
                    title="将当前显示帧设置为开始点"
                  >
                    设开始 [S]
                  </button>
                  <button
                    ref={endButtonRef}
                    type="button"
                    className={`btn btn-sm btn-point${draftErrorFields.has("end") ? " draft-field-error" : ""}`}
                    disabled={workspaceMode === "identity"}
                    aria-invalid={draftErrorFields.has("end") || undefined}
                    aria-describedby={draftErrorFields.has("end") ? "draft-error-summary" : undefined}
                    onClick={(e) => {
                      e.currentTarget.blur();
                      void markEnd();
                    }}
                    title="将当前显示帧设置为结束点（不保存）"
                  >
                    设结束 [D]
                  </button>
                  <button type="button" className="btn btn-sm btn-primary" disabled={workspaceMode === "identity" || saveState === "saving" || annotationMutationBusy} onClick={() => void saveDraft()}>
                    {saveState === "saving" ? "保存中…" : "保存此行为 [Ctrl+Enter]"}
                  </button>
                  <button type="button" className="btn btn-sm" disabled={workspaceMode === "identity" || !hasDraft || saveState === "saving" || annotationMutationBusy} onClick={() => void requestResetDraft()}>
                    重置标注
                  </button>
                </div>

                <div className="draft-summary" style={{ padding: "0 10px 8px" }}>
                  <strong>未保存行为</strong>
                  <span className={activeCategory ? "" : "pending"}>· 类别 {activeCategory ? "✓" : "—"}</span>
                  <span className={startPoint ? "" : "pending"}>· 开始 {startPoint ? "✓" : "—"}</span>
                  <span className={endPoint ? "" : "pending"}>· 结束 {endPoint ? "✓" : "—"}</span>
                  <span>· 参与对象 {behaviorSelectedIds.length}</span>
                  {draftIntervalInvalid ? <span className="invalid">· 区间待修正</span> : null}
                  <span className="visually-hidden" role="status" aria-live="polite" aria-atomic="true">
                    {`未保存行为。类别${activeCategory ? `：${activeCategory.name}` : "未设置"}；开始${startPoint ? `：帧 ${startPoint.frame}${startDisplayTime == null ? "" : `，${formatTime(startDisplayTime)}`}` : "未设置"}；结束${endPoint ? `：帧 ${endPoint.frame}${endDisplayTime == null ? "" : `，${formatTime(endDisplayTime)}`}` : "未设置"}；参与对象 ${behaviorSelectedIds.length} 个${draftIntervalInvalid ? "；区间待修正" : ""}。`}
                  </span>
                </div>
                {draftErrors.length ? <div id="draft-error-summary" ref={draftErrorRef} className="draft-errors" role="alert" tabIndex={-1}><strong>此行为尚不能保存：</strong><ul>{draftErrors.map((message) => <li key={message}>{message}</li>)}</ul></div> : null}

                {timelineDuration && timelineDuration > 0 ? (
                  <div style={{ padding: "0 10px 10px" }}>
                    <Timeline
                      duration={timelineDuration}
                      currentTime={currentTime}
                      annotations={annotations}
                      categoryById={categoryById}
                      draftStartTime={startDisplayTime}
                      draftEndTime={endDisplayTime}
                      draftStartFrame={startPoint?.frame}
                      draftEndFrame={endPoint?.frame}
                      draftColor={activeCategory?.color}
                      onSeek={seekTo}
                    />
                  </div>
                ) : (
                  <div className="frame-preview" style={{ padding: "0 10px 10px", color: "var(--text-3)" }}>
                    暂无时长信息，时间轴不可用（可在视频库中补充 duration 元数据）
                  </div>
                )}
              </>
            ) : null}
          </div>

          <div className="statusbar">
            <span>
              时间 <b className="mono">{formatTime(currentTime)}</b>
            </span>
            <span>
              开始{" "}
              <b className="mono">
                {startDisplayTime != null ? `${formatTime(startDisplayTime)}（帧 ${startPoint?.frame}）` : startPoint ? `帧 ${startPoint.frame}` : "未设置"}
              </b>
            </span>
            <span>
              结束{" "}
              <b className="mono">
                {endDisplayTime != null ? `${formatTime(endDisplayTime)}（帧 ${endPoint?.frame} inclusive）` : endPoint ? `帧 ${endPoint.frame}` : "未设置"}
              </b>
            </span>
            <span className="flex-spacer" />
            <span className="hint">{hint}</span>
            <SaveStatus state={saveState} message={errorMsg} />
          </div>
        </section>

        <aside className="annotate-side">
          {video?.workflow_status === "approved" ? (
            <Card title="视频片段生成" className="media-card">
              {/* 已通过：只读展示片段生成状态（无生成 / 重试按钮），轮询在有任务进行时自动继续 */}
              <MediaStatusPanel
                projectId={pid}
                videoId={vid}
                workflowStatus="approved"
                retryable={false}
              />
            </Card>
          ) : null}
          {workspaceMode === "behavior" ? <div id="behavior-panel" className="workspace-panel" role="tabpanel" aria-labelledby="behavior-tab">
            <div ref={categorySectionRef} tabIndex={-1} className={draftErrorFields.has("category") ? "draft-field-error" : undefined} aria-invalid={draftErrorFields.has("category") || undefined} aria-describedby={draftErrorFields.has("category") ? "draft-error-summary" : undefined}>
              <CategoryPanel categories={displayCategories} activeCategory={activeCategory} shortcuts={categoryShortcutById} onSelect={selectCategory} disabled={!videoReady} />
            </div>
            <div ref={participantSectionRef} tabIndex={-1} className={draftErrorFields.has("participants") ? "draft-field-error" : undefined} aria-invalid={draftErrorFields.has("participants") || undefined} aria-describedby={draftErrorFields.has("participants") ? "draft-error-summary" : undefined}>
              {activeCategory?.participant_mode === "role_based" ? <RoleSlotsPanel category={activeCategory} assignments={participantRoles} pendingIds={selectedMouseIds} activeKey={activeRoleKey} unlocked={unlockedRoleKeys} tracks={tracks} disabled={!detectionImport} message={roleMessage} onActivate={activateRole} onTrack={(id) => void toggleRoleTrack(id)} onRemove={(key, id) => { setParticipantRoles((prev) => ({ ...prev, [key]: (prev[key] ?? []).filter((x) => x !== id) })); setSelectedMouseIds((ids) => [...new Set([...ids, id])].sort((a, b) => a - b)); setRoleMessage(`已移除 Track ${id}，已放回待分配。`); }} onRemovePending={(id) => setSelectedMouseIds((ids) => ids.filter((trackId) => trackId !== id))} /> : <MouseIdsPanel tracks={tracks} selected={selectedMouseIds} category={activeCategory} disabled={!detectionImport} navigationActive={participantNavigationActive} focusIndex={participantFocusIndex} onFocusIndex={setParticipantFocusIndex} onExitNavigation={() => { setParticipantNavigationActive(false); blurActiveButton(); setHint("已退出参与对象键盘选择；已选参与对象保持不变"); }} onToggle={toggleMouseId} />}
            </div>
          </div> : <div id="identity-panel" className="workspace-panel" role="tabpanel" aria-labelledby="identity-tab">{visibleIdentityEditFeedback ? <div key={visibleIdentityEditFeedback.key} className="identity-edit-feedback" role="status" aria-live="polite"><span className="feedback-text">{visibleIdentityEditFeedback.text}</span><button type="button" className="identity-edit-feedback-close" aria-label="关闭" onClick={() => setIdentityEditFeedback(null)}>×</button></div> : null}<IdentityPanel tracks={tracks} selected={identitySelectedMouseIds} frame={currentFrame} search={identitySearch} showAll={showAllTracks} busy={identityBusy} suppressions={activeSuppressions} canRevertSuppression={lastSuppressionId != null} canRevertIdentity={lastIdentityEditId != null} canUndoLatest={undoHistory.length > 0} undoBoundary={undoHistory.length ? `当前页面会话可统一撤销 ${undoHistory.length} 步；按实际操作时间撤销最近一步。` : "当前页面会话没有可统一撤销的记录；刷新前的 Split / Merge 历史无法恢复。"} navigationActive={identityNavigationActive} focusIndex={identityFocusIndex} onFocusIndex={setIdentityFocusIndex} onExitNavigation={() => { setIdentityNavigationActive(false); setHint("已退出 track 列表键盘导航；已选 track 保持不变"); }} onSearch={setIdentitySearch} onShowAll={setShowAllTracks} onToggle={toggleIdentityMouseId} onSplit={() => void runIdentityEdit("split")} onMerge={() => void runIdentityEdit("merge")} onSuppressTrack={() => void suppressTrack()} onUndoLatest={() => void undoLatestTrackEdit()} onRevertSuppression={(id) => void revertLastSuppression(id)} onRevertIdentity={() => void revertLastIdentity()} /></div>}
          <AnnotationList
            annotations={annotations}
            categories={displayCategories}
            categoryById={categoryById}
            fps={effectiveFps}
            frameCount={authoritativeFrameCount}
            currentTime={currentTime}
            readOnly={workspaceMode === "identity"}
            selectedId={selectedAnnotationId}
            editingId={editingAnnotationId}
            onSelect={setSelectedAnnotationId}
            onEditingChange={changeEditingAnnotation}
            onEditSave={handleEditSave}
            onDelete={handleDelete}
            onEditCategoryChange={handleEditCategoryChange}
          />
          {project ? (
            <div className="frame-preview" style={{ color: "var(--text-3)" }}>
              项目 {project.name} · 我的角色：{ROLE_LABELS[project.role] ?? "未知角色"}
            </div>
          ) : null}
        </aside>
      </div>
    </div>
  );
}
