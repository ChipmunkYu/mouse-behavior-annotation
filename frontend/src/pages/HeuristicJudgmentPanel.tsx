import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useBlocker, useSearchParams } from "react-router-dom";
import { commitIdentityEdit, createVideoReview, getBehaviorReviewState, getCorrectedTracks, getIdentityEditHistory, getJudgment, listCategories, putBehaviorDecision, putJudgment, revertIdentityEdit, submitVideoForReview, withdrawVideo } from "../api";
import { ApiError } from "../api/client";
import type { Category, CorrectedTrackSummary, IdentityEditCheckRequest, IdentityEditCheckResponse, Judgment, JudgmentSelectionInput, JudgmentVersions, Project, Video } from "../api/types";
import { Card, EmptyState, Loading, WorkflowBadge } from "../components/ui";
import { useConfirm } from "../components/ConfirmDialog";
import DetectionOverlay, { type OverlayFrameData } from "../components/DetectionOverlay";
import Timeline from "../components/Timeline";
import { MouseIdsPanel, RoleSlotsPanel, IdentityPanel, tracksForOverlayFrame } from "../components/AnnotationTrackPanels";
import { ParticipantSummary } from "../components/ParticipantSummary";
import { useRoleAssignment } from "../components/useRoleAssignment";
import { getInitiallyUnlockedRoleKeys } from "../utils/roleNavigation";
import { identityEditPreview } from "../utils/identityEditPreview";
import { MediaLoadProgress } from "../components/MediaLoadProgress";
import { useMediaSource } from "../media";
import { formatTime } from "../utils/format";

export function judgmentVersions(value: Judgment): JudgmentVersions {
  return {
    expected_annotation_revision: value.annotation_revision,
    base_detection_import_revision: value.detection_import_revision,
    base_identity_revision: value.identity_revision,
    expected_submission_id: value.submission_id,
    expected_decision_revision: value.submission_id == null ? null : value.decision_revision,
  };
}

export function judgmentSelection(category: Category, mouseIds: number[], roles: Record<string, number[]>): JudgmentSelectionInput {
  return category.participant_mode === "role_based"
    ? { category_id: category.id, participant_roles: Object.fromEntries(category.role_definitions.map((role) => [role.key, roles[role.key] ?? []])) }
    : { category_id: category.id, mouse_ids: [...mouseIds].sort((a, b) => a - b), participant_roles: {} };
}

export function judgmentSelectionError(category: Category | undefined, mouseIds: number[], roles: Record<string, number[]>): string | null {
  if (!category?.is_active) return "请人工选择本项目启用的类别";
  const ids = category.participant_mode === "role_based" ? [...new Set(Object.values(roles).flat())] : mouseIds;
  if (ids.length < category.mouse_count_min || (category.mouse_count_max != null && ids.length > category.mouse_count_max)) return `参与鼠数量不符合类别要求（${category.mouse_count_min}–${category.mouse_count_max ?? "不限"}）`;
  if (category.participant_mode === "role_based") {
    const assigned = category.role_definitions.flatMap((role) => roles[role.key] ?? []);
    if (mouseIds.some((id) => !assigned.includes(id))) return "还有 Track 待分配到角色，请分配或移除待分配项";
    if (new Set(assigned).size !== assigned.length) return "不同角色不能使用同一只鼠";
    for (const role of category.role_definitions) {
      const count = (roles[role.key] ?? []).length;
      if (count < role.min_count || (role.max_count != null && count > role.max_count)) return `请完整选择「${role.name}」（${role.min_count}–${role.max_count ?? "不限"}）`;
    }
  }
  return null;
}

type JudgmentDraft = { categoryId: number | null; mouseIds: number[]; roles: Record<string, number[]> };
const labelKey = (label: string) => label.trim().toLowerCase().replace(/[\s_-]+/g, "_");
// Explicit expert aliases, including the production Project 2 display names; never category IDs or role keys.
const behaviorAliases = [
  ["locomotion", "moving", "移动"], ["static", "stationary", "静止"], ["grooming", "理毛"],
  ["rearing", "climb_wall", "站立攀爬", "爬墙"], ["approach", "接近"], ["together", "一起"],
  ["chasing", "chase", "追逐"], ["attack", "attack_review", "攻击", "攻击行为"],
  ["snout_head_contact", "nose_head_contact", "nose_head", "鼻头接触"],
  ["snout_rear_contact", "nose_tail_contact", "nose_tail", "鼻尾接触"],
  ["huddling", "huddle", "扎堆", "扎堆行为"], ["social_clustering", "社会聚集"],
  ["dispersal", "dispersal_review", "群体分散"], ["group_locomotion", "群体同步运动"],
  ["isolation", "孤立", "孤立行为"],
];
const directionalRoles: Record<string, [string[], string[]]> = {
  approach: [["approaching", "主动接近"], ["approached", "被接近"]],
  chasing: [["chasing", "追逐"], ["chased", "被追逐"]],
  attack: [["attacker", "攻击"], ["attacked", "被攻击"]],
  snout_head_contact: [["snout_contacting", "鼻部探测方"], ["head_contacted", "头部被接触方"]],
  snout_rear_contact: [["snout_contacting", "鼻部探测方"], ["rear_contacted", "后躯被接触方"]],
};

/** Only an unsaved initial draft may consume input; every ID is checked against current corrected tracks. */
export function initialJudgmentDraft(value: Judgment, categories: Category[], tracks: CorrectedTrackSummary[]): JudgmentDraft {
  if (value.selection) return { categoryId: value.selection.category_id, mouseIds: value.selection.mouse_ids, roles: value.selection.participant_roles };
  const empty: JudgmentDraft = { categoryId: null, mouseIds: [], roles: {} };
  if (value.workflow_status !== "draft" || value.judgment_status !== "pending") return empty;
  const labels = [value.input.behavior, value.input.canonical_behavior].filter((label): label is string => typeof label === "string" && !!label.trim());
  const aliases = labels.map((label) => behaviorAliases.find((group) => group.includes(labelKey(label))));
  if (!aliases.length || aliases.some((group) => !group || group !== aliases[0])) return empty;
  const matches = categories.filter((category) => category.is_active && aliases[0]!.includes(labelKey(category.name)));
  if (matches.length !== 1) return empty;
  const category = matches[0];
  const valid = new Set(tracks.map((track) => track.display_track_id));
  const legalId = (id: unknown): id is number => typeof id === "number" && Number.isSafeInteger(id) && id >= 0 && valid.has(id);
  const legalIds = (ids: unknown): number[] => Array.isArray(ids) ? [...new Set(ids.filter(legalId))] : [];
  const mouseIds = new Set(legalIds(value.input.mouse_ids));
  const roles: Record<string, number[]> = {};
  const names = directionalRoles[aliases[0]![0]];
  const semanticRole = (label: string) => names?.findIndex((group) => group.includes(labelKey(label)));
  const roleFor = (label: string) => {
    const index = labelKey(label) === "actor" ? 0 : labelKey(label) === "target" ? 1 : semanticRole(label);
    const found = category.role_definitions.filter((role) => role.key === label || (index != null && index >= 0 && semanticRole(role.name) === index));
    return found.length === 1 ? found[0].key : null;
  };
  let conflict = false;
  const assign = (key: string | null, ids: number[]) => {
    if (!key || !ids.length) return;
    if (roles[key] && (roles[key].length !== ids.length || roles[key].some((id) => !ids.includes(id)))) conflict = true;
    roles[key] = ids;
  };
  for (const [label, id] of [["actor", value.input.actor_id], ["target", value.input.target_id]] as const) {
    if (legalId(id)) { mouseIds.add(id); if (category.participant_mode === "role_based") assign(roleFor(label), [id]); }
  }
  for (const participant of Array.isArray(value.input.participants) ? value.input.participants : []) {
    if (!participant || typeof participant !== "object" || Array.isArray(participant)) continue;
    const entry = participant as Record<string, unknown>;
    const ids = legalIds(entry.track_ids);
    ids.forEach((id) => mouseIds.add(id));
    if (category.participant_mode !== "role_based" || !ids.length) continue;
    const labels = [entry.role_key, entry.role_name].filter((label) => label != null);
    const keys = labels.map((label) => typeof label === "string" ? roleFor(label) : null);
    if (keys.length && keys.some((key) => !key || key !== keys[0])) { conflict = true; continue; }
    assign(keys[0] ?? null, ids);
  }
  const assigned = Object.values(roles).flat();
  if (new Set(assigned).size !== assigned.length || category.role_definitions.some((role) => role.max_count != null && (roles[role.key]?.length ?? 0) > role.max_count)) conflict = true;
  return { categoryId: category.id, mouseIds: [...mouseIds].sort((a, b) => a - b), roles: conflict ? {} : roles };
}

type Props = {
  projectId: number;
  video: Video;
  project: Project | null;
  review?: boolean;
  disabled?: boolean;
  refreshKey?: string;
  onChanged?: () => Promise<void>;
  onEditingChange?: (editing: boolean) => void;
};

function toggle(ids: number[], id: number): number[] { return ids.includes(id) ? ids.filter((value) => value !== id) : [...ids, id]; }

async function allTracks(pid: number, videoId: number): Promise<CorrectedTrackSummary[]> {
  const items: CorrectedTrackSummary[] = [];
  let page = 1;
  let pages = 1;
  do {
    const result = await getCorrectedTracks(pid, videoId, { page, page_size: 200 });
    items.push(...result.items);
    pages = result.pages;
    page += 1;
  } while (page <= pages);
  return items;
}

function ParticipantChoices({ category, tracks, mouseIds, roles, disabled, onMouseIds, onRoles, onTrackHandler }: {
  category: Category; tracks: CorrectedTrackSummary[]; mouseIds: number[]; roles: Record<string, number[]>; disabled: boolean;
  onMouseIds: (ids: number[]) => void; onRoles: (roles: Record<string, number[]>) => void;
  onTrackHandler?: { current: ((id: number) => void) | null };
}) {
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [unlocked, setUnlocked] = useState<Set<string>>(new Set());
  const [message, setMessage] = useState<string | null>(null);
  const [dialog, confirm] = useConfirm();
  const definitions = useMemo(() => [...category.role_definitions].sort((a, b) => a.role_sort_order - b.role_sort_order), [category]);
  useEffect(() => { setActiveKey(definitions[0]?.key ?? null); setUnlocked(getInitiallyUnlockedRoleKeys(definitions, roles)); setMessage(null); }, [category.id]);
  const { activateRole, toggleRoleTrack, removeRoleTrack } = useRoleAssignment({
    roleDefinitions: definitions, participantRoles: roles, activeRoleKey: activeKey, unlockedRoleKeys: unlocked,
    setParticipantRoles: (value) => onRoles(typeof value === "function" ? value(roles) : value),
    setSelectedMouseIds: (value) => onMouseIds(typeof value === "function" ? value(mouseIds) : value),
    setActiveRoleKey: setActiveKey, setUnlockedRoleKeys: setUnlocked, setRoleMessage: setMessage, confirm,
    beforeToggle: () => !disabled,
  });
  const onToggle = (id: number) => { if (!disabled) category.participant_mode === "role_based" ? void toggleRoleTrack(id) : onMouseIds(toggle(mouseIds, id)); };
  useEffect(() => { if (onTrackHandler) onTrackHandler.current = onToggle; return () => { if (onTrackHandler) onTrackHandler.current = null; }; });
  return <>{dialog}{category.participant_mode === "role_based"
    ? <RoleSlotsPanel category={category} assignments={roles} pendingIds={mouseIds.filter((id) => !Object.values(roles).flat().includes(id))} activeKey={activeKey} unlocked={unlocked} tracks={tracks} disabled={disabled} message={message} onActivate={activateRole} onTrack={onToggle} onRemove={removeRoleTrack} onRemovePending={(id) => onMouseIds(mouseIds.filter((value) => value !== id))} />
    : <MouseIdsPanel category={category} tracks={tracks} selected={mouseIds} disabled={disabled} navigationActive={false} focusIndex={0} onFocusIndex={() => {}} onExitNavigation={() => {}} onToggle={onToggle} />}</>;
}

/** 人工与启发式工作页不同时挂载，旧增删、边界、crop、抑制和快捷键不会进入此面板。 */
export default function HeuristicJudgmentPanel({ projectId: pid, video, project, review = false, disabled = false, refreshKey = "", onChanged, onEditingChange }: Props) {
  const [judgment, setJudgment] = useState<Judgment | null>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  const [trackSummaries, setTrackSummaries] = useState<CorrectedTrackSummary[]>([]);
  const [frameData, setFrameData] = useState<OverlayFrameData | null>(null);
  const [identitySearch, setIdentitySearch] = useState("");
  const [showAllTracks, setShowAllTracks] = useState(false);
  const [workspaceMode, setWorkspaceMode] = useState<"behavior" | "identity">("behavior");
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [mouseIds, setMouseIds] = useState<number[]>([]);
  const [roles, setRoles] = useState<Record<string, number[]>>({});
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reviewFeedback, setReviewFeedback] = useState("");
  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [duration, setDuration] = useState(0);
  const [identityIds, setIdentityIds] = useState<number[]>([]);
  const [preview, setPreview] = useState<{ request: IdentityEditCheckRequest; result: IdentityEditCheckResponse } | null>(null);
  const [confirmedIds, setConfirmedIds] = useState<number[]>([]);
  const [confirmedRoles, setConfirmedRoles] = useState<Record<string, number[]>>({});
  const [explicitConfirmation, setExplicitConfirmation] = useState(false);
  const [lastEditId, setLastEditId] = useState<number | null>(null);
  const [dataVersion, setDataVersion] = useState(0);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [videoElement, setVideoElement] = useState<HTMLVideoElement | null>(null);
  const attachVideo = useCallback((element: HTMLVideoElement | null) => { (videoRef as { current: HTMLVideoElement | null }).current = element; setVideoElement(element); }, []);
  const participantTrackHandler = useRef<((id: number) => void) | null>(null);
  const [searchParams] = useSearchParams();
  const initialSeekDone = useRef(false);
  const mounted = useRef(true);
  const loadGeneration = useRef(0);
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  const actionLock = useRef(false);
  const retainedDraft = useRef<JudgmentDraft | null>(null);
  const initialized = useRef(false);
  const [confirmDialog, confirm] = useConfirm();
  const blocker = useBlocker(dirty && !busy);
  const media = useMediaSource({ videoId: videoElement ? video.id : null, surface: review ? "review" : "annotate", videoRef });
  useEffect(() => {
    if (!judgment || media.status !== "ready" || !videoRef.current || initialSeekDone.current) return;
    const requested = searchParams.get("t");
    const time = requested != null && Number.isFinite(Number(requested)) ? Number(requested) : judgment.input.time_range.start;
    videoRef.current.currentTime = Math.max(0, Math.min(videoRef.current.duration || time, time));
    setCurrentTime(videoRef.current.currentTime); initialSeekDone.current = true;
  }, [judgment, media.status, searchParams]);
  const category = categories.find((item) => item.id === categoryId);
  const terminal = judgment?.workflow_status === "approved" || judgment?.workflow_status === "unusable";
  const submitted = judgment?.workflow_status === "submitted";
  const contextReady = loadedKey === refreshKey;
  const canEdit = Boolean(!disabled && contextReady && judgment && project && !terminal && (submitted ? review && project.can_review : ["draft", "rejected"].includes(judgment.workflow_status)));
  const selectionError = judgmentSelectionError(category, mouseIds, roles);
  const frame = Math.max(0, Math.floor(currentTime * (video.fps || 30)));
  const timelineDuration = duration > 0 ? duration : video.duration && video.duration > 0 ? video.duration : null;

  function togglePlay() {
    const element = videoRef.current;
    if (!element || media.status !== "ready") return;
    if (element.paused) void element.play().catch(() => setError("播放失败，请重试。"));
    else element.pause();
  }

  // 与 AnnotatePage 相同：逐帧和时间轴跳转不改变播放/暂停状态。
  function step(direction: -1 | 1, frames = 1) {
    const element = videoRef.current;
    if (!element) return;
    const fps = video.fps && video.fps > 0 ? video.fps : 30;
    element.currentTime = Math.min(Math.max(0, element.currentTime + direction * frames / fps), element.duration || Number.MAX_VALUE);
  }

  function seekTo(time: number) {
    const element = videoRef.current;
    if (element) element.currentTime = Math.min(Math.max(0, time), element.duration || time);
  }

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (media.status !== "ready" || document.querySelector(".modal-overlay") || (event.target instanceof HTMLElement && event.target.closest("input, textarea, select, button, [contenteditable=true]"))) return;
      if (!["Space", "ArrowLeft", "ArrowRight"].includes(event.code)) return;
      event.preventDefault();
      if (event.repeat) return;
      if (event.code === "Space") togglePlay();
      else step(event.code === "ArrowLeft" ? -1 : 1, event.shiftKey ? 10 : 1);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [media.status, video.fps]);

  const applyJudgment = useCallback((value: Judgment, draft?: JudgmentDraft) => {
    retainedDraft.current = null;
    setJudgment(value); setCategoryId(draft ? draft.categoryId : value.selection?.category_id ?? null);
    setMouseIds(draft ? draft.mouseIds : value.selection?.mouse_ids ?? []); setRoles(draft ? draft.roles : value.selection?.participant_roles ?? {});
    setDirty(false); setPreview(null); setExplicitConfirmation(false);
  }, []);

  const load = useCallback(async () => {
    const generation = ++loadGeneration.current;
    const [value, cats, tracks, history] = await Promise.all([
      getJudgment(pid, video.id), listCategories(pid), allTracks(pid, video.id), getIdentityEditHistory(pid, video.id),
    ]);
    if (!mounted.current || generation !== loadGeneration.current) return;
    const draft = retainedDraft.current;
    const initial = !initialized.current ? initialJudgmentDraft(value, cats, tracks) : undefined;
    initialized.current = true;
    applyJudgment(value, initial); setCategories(cats);
    if (draft && (["draft", "rejected"].includes(value.workflow_status) || (review && value.workflow_status === "submitted"))) {
      retainedDraft.current = draft; setCategoryId(draft.categoryId); setMouseIds(draft.mouseIds); setRoles(draft.roles); setDirty(true);
    }
    setTrackSummaries(tracks); setLoadedKey(refreshKey);
    setLastEditId(history[0] && ["merge", "split"].includes(history[0].operation) ? history[0].id : null);
    setDataVersion((version) => version + 1);
    return value;
  }, [pid, video.id, applyJudgment, refreshKey, review]);

  useEffect(() => {
    mounted.current = true;
    void load().catch((error: unknown) => { if (mounted.current) setError(error instanceof Error ? error.message : "判断加载失败"); });
    return () => { mounted.current = false; loadGeneration.current += 1; };
  }, [load]);

  useEffect(() => { onEditingChange?.(busy || dirty || preview != null || !contextReady); }, [busy, dirty, preview, contextReady, onEditingChange]);
  useEffect(() => () => onEditingChange?.(false), [onEditingChange]);
  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);

  async function run(action: () => Promise<void>) {
    if (actionLock.current) return;
    actionLock.current = true; setBusy(true); setError(null); setNotice(null);
    try { await action(); }
    catch (error) {
      if (!mounted.current) return;
      setError(error instanceof Error ? error.message : "操作失败");
      if (error instanceof ApiError && error.status === 409) {
        // Refresh CAS without silently throwing away a user's participant edits.
        try {
          retainedDraft.current = { categoryId, mouseIds, roles };
          await load(); await onChanged?.(); setError(retainedDraft.current ? "状态已变化，已刷新；未保存的选择已保留，请核对后重试，未自动重试。" : "状态已变化，已刷新；当前结果只读，未自动重试。");
        }
        catch { setError("状态已变化，刷新失败；请刷新页面后再操作。"); setJudgment(null); }
      }
    } finally { actionLock.current = false; if (mounted.current) setBusy(false); }
  }

  async function saved(value: Judgment, message: string) {
    if (!mounted.current) return;
    applyJudgment(value); setNotice(message);
    await onChanged?.();
  }

  function changed(patch: Partial<JudgmentDraft>) {
    retainedDraft.current = { ...(retainedDraft.current ?? { categoryId, mouseIds, roles }), ...patch };
    setDirty(true); setPreview(null);
  }

  async function saveUsable() {
    if (!judgment || !category || selectionError || !canEdit) return;
    await run(async () => saved(await putJudgment(pid, video.id, { judgment_status: "usable", ...judgmentSelection(category, mouseIds, roles), ...judgmentVersions(judgment) }), submitted ? "判断已更新；已生成新快照，请重新裁决。" : "已保存为可用，可提交复核。"));
  }

  async function pass() {
    if (!judgment || !category || selectionError || !canEdit || submitted || review || preview) return;
    await run(async () => {
      const value = await putJudgment(pid, video.id, { judgment_status: "usable", ...judgmentSelection(category, mouseIds, roles), ...judgmentVersions(judgment) });
      if (!mounted.current) return;
      applyJudgment(value);
      await submitVideoForReview(pid, video.id, judgmentVersions(value));
      await load(); await onChanged?.(); setNotice("已提交复核，尚未最终通过。");
    });
  }

  async function markUnusable() {
    if (!judgment || !canEdit || submitted) return;
    await run(async () => {
      if (!await confirm({ title: "确认不可用？", message: "此事件将终止，不进入复核、不导出，且不可恢复。原文件与判断证据会保留，不会删除。", confirmLabel: "标记不可用", danger: true })) return;
      await saved(await putJudgment(pid, video.id, { judgment_status: "unusable", ...judgmentVersions(judgment) }), "已标记不可用，证据已保留。");
    });
  }

  async function decideReview(result: "approved" | "rejected" | "unusable") {
    if (!review || !submitted || !canEdit || !project?.can_review || !judgment || dirty || preview) return;
    const feedback = reviewFeedback.trim();
    if (result === "rejected" && !feedback) { setError("退回时请填写原因"); return; }
    await run(async () => {
      if (!await confirm({ title: result === "approved" ? "确认最终通过？" : result === "unusable" ? "确认不可用？" : "确认退回判断？", message: result === "approved" ? "该启发式结果将最终通过并永久只读，进入可导出范围。" : result === "unusable" ? "该启发式结果将永久不可用，不进入导出，不可恢复；原文件与判断证据保留。" : <>将退回判断，原因：{feedback}</>, confirmLabel: result === "approved" ? "最终通过" : result === "unusable" ? "标记不可用" : "退回判断", danger: result !== "approved" })) return;
      if (!mounted.current) return;
      const state = await getBehaviorReviewState(pid, video.id);
      if (!mounted.current) return;
      if (state.submission_status !== "submitted" || state.submission_id !== judgment.submission_id || state.decision_revision !== judgment.decision_revision) throw new ApiError(409, "复核快照已变化，请重新核对");
      if (state.annotations.length !== 1) throw new Error("启发式单事件快照必须恰有一条行为，未提交裁决，请刷新或联系管理员。");
      const decided = result === "unusable" ? state : await putBehaviorDecision(pid, video.id, state.submission_id!, state.annotations[0].id, { status: result, feedback: result === "rejected" ? feedback : null, expected_decision_revision: state.decision_revision });
      if (!mounted.current) return;
      setJudgment((current) => current ? { ...current, decision_revision: decided.decision_revision } : current);
      try {
        await createVideoReview(pid, video.id, { result, comment: feedback || null, expected_submission_id: state.submission_id!, expected_decision_revision: decided.decision_revision });
      } catch (error) {
        if (error instanceof ApiError && error.status === 409) throw error;
        throw new Error(`${result === "unusable" ? "不可用结果尚未确认" : "单条裁决已保存，最终结果尚未确认"}，请刷新核对后重试；${error instanceof Error ? error.message : "发布失败"}`);
      }
      await load(); await onChanged?.(); setReviewFeedback("");
      setNotice(result === "approved" ? "复核已最终通过。" : result === "unusable" ? "已标记不可用，不进入导出。" : "已退回判断，原因已保存。");
    });
  }

  async function previewIdentity(operation: "merge" | "split") {
    if (!judgment || !canEdit || dirty) return;
    const request: IdentityEditCheckRequest = { operation, track_ids: identityIds, ...(operation === "split" ? { frame } : {}), ...judgmentVersions(judgment) };
    await run(async () => {
      const result = await identityEditPreview(pid, video.id, request);
      setPreview({ request, result }); setExplicitConfirmation(false);
      setConfirmedIds(result.proposed_selection?.mouse_ids ?? judgment.selection?.mouse_ids ?? []);
      setConfirmedRoles(result.proposed_selection?.participant_roles ?? judgment.selection?.participant_roles ?? {});
    });
  }

  const trackIds = useMemo(() => trackSummaries.map((track) => track.display_track_id), [trackSummaries]);
  const tracks = tracksForOverlayFrame(trackSummaries, frameData ?? { status: "loading", detections: [] }, [], true, "");
  const identityTracks = tracksForOverlayFrame(tracks.filter((track) => !identitySearch || String(track.display_track_id).includes(identitySearch)), frameData ?? { status: "loading", detections: [] }, identityIds, showAllTracks, identitySearch);
  const identityOptions = useMemo(() => [...new Set([...trackIds, ...(preview?.result.new_display_track_id != null ? [preview.result.new_display_track_id] : [])])]
    .filter((id) => preview?.request.operation !== "merge" || !preview.request.track_ids.includes(id) || id === preview.result.retained_display_track_id)
    .sort((a, b) => a - b), [trackIds, preview]);

  if (!judgment) return <Card>{error ? <div className="error-box" role="alert">{error}<button className="btn btn-sm" onClick={() => void run(async () => { await load(); })}>重试</button></div> : <Loading text="加载启发式结果…" />}</Card>;
  const selectedIds = category?.participant_mode === "role_based" ? [...new Set(Object.values(roles).flat())] : mouseIds;
  const roleLabels = Object.fromEntries(category?.role_definitions.flatMap((role) => (roles[role.key] ?? []).map((id) => [id, role.name])) ?? []);
  const readOnlyMessage = terminal ? "已进入终态：只读，不可重新打开或恢复。" : submitted ? "已提交复核；需先撤回再修改，复核者也可在复核页直接修改。" : "当前没有编辑权限。";
  const tabPrefix = `judgment-${review ? "review" : "annotate"}-${video.id}`;
  const toggleIdentityTrack = (id: number) => { if (canEdit && !busy && !dirty && !preview) setIdentityIds((ids) => toggle(ids, id)); };

  return <div className="judgment-workspace">
    {confirmDialog}
    {blocker.state === "blocked" ? <div className="error-box" role="alert">判断尚未保存。<button className="btn btn-sm" onClick={() => blocker.reset()}>继续编辑</button><button className="btn btn-danger btn-sm" onClick={() => blocker.proceed()}>放弃并离开</button></div> : null}
    {error ? <div className="error-box" role="alert">{error}</div> : null}
    {notice ? <div className="ok-box" role="status">{notice}</div> : null}
    <div className="annotate-body judgment-body">
    <section className="annotate-main judgment-main" aria-label="视频主区">
    <Card className="player-card" title={review ? "启发式结果复核" : "启发式结果判断"} extra={<WorkflowBadge value={judgment.workflow_status} revision={judgment.annotation_revision} />}>
      <div className="video-wrap judgment-player">
        <video ref={attachVideo} playsInline preload="metadata" className={media.status === "ready" ? "" : "media-player-pending"} title="点击播放 / 暂停 [Space]" onClick={togglePlay} onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} onDurationChange={(event) => setDuration(Number.isFinite(event.currentTarget.duration) ? event.currentTarget.duration : 0)} onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)} />
        {media.status === "ready" && videoElement ? <DetectionOverlay projectId={pid} videoId={video.id} video={videoElement} showOnlySelected={false} interactive onToggleTrack={(id) => { if (canEdit && !busy && preview == null && (workspaceMode === "identity" ? !dirty : !!category)) { if (workspaceMode === "identity") toggleIdentityTrack(id); else participantTrackHandler.current?.(id); } }} onFrameData={setFrameData} currentTime={currentTime} fallbackFps={video.fps} selectedIds={workspaceMode === "identity" ? identityIds : selectedIds} trackRoleLabels={roleLabels} refreshKey={dataVersion} /> : null}
        <MediaLoadProgress state={media} onCancel={media.cancel} />
        {media.status === "pending" || media.status === "failed" || media.status === "cancelled" ? <div className="media-status-overlay"><EmptyState compact title="播放资源暂不可用" hint={media.message} /><button className="btn btn-sm" onClick={media.reload}>重试</button></div> : null}
      </div>
      <div className="player-controls judgment-player-controls">
        <div className="judgment-frame-controls">
          <button type="button" className="btn btn-sm" disabled={media.status !== "ready"} onClick={(event) => { event.currentTarget.blur(); togglePlay(); }}>{playing ? "⏸ 暂停 [Space]" : "▶ 播放 [Space]"}</button>
          <button type="button" className="btn btn-sm" disabled={media.status !== "ready"} onClick={(event) => { event.currentTarget.blur(); step(-1); }}>⟨ 退一帧 [←]</button>
          <button type="button" className="btn btn-sm" disabled={media.status !== "ready"} onClick={(event) => { event.currentTarget.blur(); step(1); }}>进一帧 [→] ⟩</button>
          <span className="mono">{formatTime(currentTime)} · 帧 {frame}</span>
        </div>
        {review || canEdit ? <div className="judgment-disposition-actions" aria-label={review ? "启发式复核决策" : "启发式判断处置"}>
          {review ? <><button className="btn btn-primary" disabled={!canEdit || !submitted || !project?.can_review || busy || dirty || preview != null} onClick={() => void decideReview("approved")}>通过</button><button className="btn btn-danger" disabled={!canEdit || !submitted || !project?.can_review || busy || dirty || preview != null} onClick={() => void decideReview("unusable")}>不可用</button><button className="btn" disabled={!canEdit || !submitted || !project?.can_review || busy || dirty || preview != null || !reviewFeedback.trim()} onClick={() => void decideReview("rejected")}>退回</button></> : !submitted ? <><button className="btn btn-primary" disabled={busy || !!selectionError || preview != null} onClick={() => void pass()}>通过</button><button className="btn btn-danger" disabled={busy || preview != null} onClick={() => void markUnusable()}>不可用</button></> : null}
        </div> : null}
      </div>
      {timelineDuration && timelineDuration > 0 ? <div style={{ padding: "0 10px 10px" }}><Timeline duration={timelineDuration} currentTime={currentTime} annotations={[]} categoryById={new Map(categories.map((item) => [item.id, item]))} draftStartTime={judgment.input.time_range.start} draftEndTime={judgment.input.time_range.end} draftStartFrame={judgment.input.frame_range.start} draftEndFrame={judgment.input.frame_range.end} draftColor={category?.color} onSeek={seekTo} /></div> : <div className="frame-preview" style={{ padding: "0 10px 10px", color: "var(--text-3)" }}>暂无时长信息，时间轴不可用（可在视频库中补充 duration 元数据）</div>}
      {review && submitted ? <div className="field review-comment-field"><label htmlFor={`heuristic-review-feedback-${video.id}`}>退回原因（退回时必填）</label><textarea id={`heuristic-review-feedback-${video.id}`} className="textarea" rows={2} value={reviewFeedback} disabled={!canEdit || busy} placeholder="填写本次复核意见；退回必须说明原因" onChange={(event) => setReviewFeedback(event.target.value)} /></div> : null}
      <div className="judgment-boundary" role="note">事件起止只读：帧 {judgment.input.frame_range.start}–{judgment.input.frame_range.end} · {formatTime(judgment.input.time_range.start)}–{formatTime(judgment.input.time_range.end)}。</div>
    </Card>
    </section>
    <aside className="annotate-side judgment-side" aria-label="判断工作栏">
      <div className="judgment-side-header">
      <div className="workspace-tabs" role="tablist" aria-label="判断工作模式">
        {([ ["behavior", "行为信息"], ["identity", "Track 修正"] ] as const).map(([mode, label]) => <button key={mode} type="button" id={`${tabPrefix}-${mode}-tab`} role="tab" aria-selected={workspaceMode === mode} aria-controls={`${tabPrefix}-${mode}-panel`} tabIndex={workspaceMode === mode ? 0 : -1} className={workspaceMode === mode ? "active" : ""} disabled={busy || preview != null} onClick={() => setWorkspaceMode(mode)} onKeyDown={(event) => {
          if (busy || preview != null || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === "Home" ? "behavior" : event.key === "End" ? "identity" : workspaceMode === "behavior" ? "identity" : "behavior";
          setWorkspaceMode(next); document.getElementById(`${tabPrefix}-${next}-tab`)?.focus();
        }}>{label}</button>)}
      </div>
      <div className="judgment-summary" role="status" aria-label="当前行为摘要" aria-atomic="true">
        <div>行为类别：<span className="badge badge-muted">{category?.name ?? "未选择"}</span></div>
        <ParticipantSummary mode={category?.participant_mode ?? "unordered"} roles={category?.role_definitions ?? []} assignments={roles} mouseIds={mouseIds} emptyLabel="未选择" />
        {category?.participant_mode === "role_based" && mouseIds.some((id) => !selectedIds.includes(id)) ? <span className="participant-summary">待分配：{mouseIds.filter((id) => !selectedIds.includes(id)).map((id) => `Track ${id}`).join("、")}</span> : null}
      </div>
      </div>
      {workspaceMode === "behavior" ? <div id={`${tabPrefix}-behavior-panel`} className="workspace-panel" role="tabpanel" aria-labelledby={`${tabPrefix}-behavior-tab`}>
      <fieldset disabled={!canEdit || busy || preview != null} className="judgment-fields"><legend>人工确认</legend>
        <label className="field" htmlFor={`judgment-category-${video.id}`}>项目类别<select id={`judgment-category-${video.id}`} className="select" value={categoryId ?? ""} onChange={(event) => { const categoryId = event.target.value ? Number(event.target.value) : null; setCategoryId(categoryId); setMouseIds([]); setRoles({}); changed({ categoryId, mouseIds: [], roles: {} }); }}><option value="">请选择项目类别</option>{categories.filter((item) => item.is_active || item.id === categoryId).map((item) => <option key={item.id} value={item.id} disabled={!item.is_active}>{item.group} / {item.name}{item.is_active ? "" : "（已停用）"}</option>)}</select></label>
        {category ? <ParticipantChoices key={`${category.id}:${dataVersion}`} category={category} tracks={tracks} onTrackHandler={participantTrackHandler} mouseIds={mouseIds} roles={roles} disabled={!canEdit || busy || preview != null} onMouseIds={(ids) => { setMouseIds(ids); changed({ mouseIds: ids }); }} onRoles={(value) => { setRoles(value); changed({ roles: value }); }} /> : null}
      </fieldset>
      {canEdit ? <><div className="field-hint" role="status">{busy ? "保存中…" : selectionError ?? (dirty ? "有未保存的修改" : "类别及参与关系已选择")}</div>{review && submitted ? <button className="btn btn-primary" disabled={busy || !!selectionError || preview != null} onClick={() => void saveUsable()}>保存修改并刷新快照</button> : null}{dirty ? <div className="judgment-actions">{!submitted ? <button className="btn" disabled={busy || !!selectionError || preview != null} onClick={() => void saveUsable()}>保存修改</button> : null}<button className="btn" disabled={busy} onClick={() => applyJudgment(judgment)}>放弃未保存修改</button></div> : null}</> : <p className="field-hint">{readOnlyMessage}</p>}
      {!review && submitted ? <div className="judgment-actions"><button className="btn" disabled={busy} onClick={() => void (async () => { if (!await confirm({ title: "撤回本次提交？", message: "未完成期间可以撤回后重新判断；旧快照与决定会保留。", confirmLabel: "撤回提交" })) return; await run(async () => { await withdrawVideo(pid, video.id, judgmentVersions(judgment)); await load(); await onChanged?.(); }); })()}>撤回提交</button>{project?.can_review ? <Link className="btn" to={`/projects/${pid}/review?video=${video.id}`}>进入复核</Link> : null}</div> : null}
      </div> : <div id={`${tabPrefix}-identity-panel`} className="workspace-panel" role="tabpanel" aria-labelledby={`${tabPrefix}-identity-tab`}>
    {canEdit ? <>
      <IdentityPanel restricted tracks={identityTracks} selected={identityIds} frame={frame} search={identitySearch} showAll={showAllTracks} busy={busy || dirty || preview != null} canRevertIdentity={lastEditId != null} undoBoundary={dirty ? "请先在行为信息中保存参与关系，再修正 ID。" : "只影响当前片段；预览后确认执行。"} onSearch={setIdentitySearch} onShowAll={setShowAllTracks} onToggle={toggleIdentityTrack} onSplit={() => void previewIdentity("split")} onMerge={() => void previewIdentity("merge")} onRevertIdentity={() => void run(async () => { if (!canEdit || lastEditId == null) return; await revertIdentityEdit(pid, video.id, lastEditId, judgmentVersions(judgment)); await load(); await onChanged?.(); setNotice("已撤销最近 ID 修正。"); })} />
      {preview ? <div className="judgment-identity-preview"><b>{preview.result.operation === "split" ? `将从帧 ${preview.request.frame} 拆为 Track ${preview.result.new_display_track_id ?? "待分配"}` : `将合并为 Track ${preview.result.retained_display_track_id ?? "待确认"}`}</b><p>{preview.result.message}</p>{preview.result.participant_confirmation_required ? <><p>参与关系有歧义，请明确操作后的参与鼠与角色（类别不变）。</p>{category ? <ParticipantChoices key={`preview:${category.id}`} category={category} tracks={identityOptions.map((id) => tracks.find((track) => track.display_track_id === id) ?? { display_track_id: id, detection_count: 0 })} mouseIds={confirmedIds} roles={confirmedRoles} disabled={busy} onMouseIds={(ids) => { setConfirmedIds(ids); setExplicitConfirmation(false); }} onRoles={(value) => { setConfirmedRoles(value); setExplicitConfirmation(false); }} /> : null}<label><input type="checkbox" checked={explicitConfirmation} disabled={busy || !!judgmentSelectionError(category, confirmedIds, confirmedRoles)} onChange={(event) => setExplicitConfirmation(event.target.checked)} />我已核对并确认操作后的参与关系</label></> : null}<div className="judgment-actions"><button className="btn btn-primary" disabled={busy || (preview.result.participant_confirmation_required && (!explicitConfirmation || !category))} onClick={() => void run(async () => { await commitIdentityEdit(pid, video.id, { ...preview.request, ...(preview.result.participant_confirmation_required && category ? { confirmed_selection: judgmentSelection(category, confirmedIds, confirmedRoles) } : {}) }); setIdentityIds([]); await load(); await onChanged?.(); setNotice(submitted ? "ID 已修正，新快照已生成，请重新裁决。" : "ID 与参与关系已同步更新。"); })}>确认执行</button><button className="btn" disabled={busy} onClick={() => setPreview(null)}>取消预览</button></div></div> : null}
    </> : <p className="field-hint">{readOnlyMessage}</p>}
      </div>}
    </aside>
    </div>
  </div>;
}
