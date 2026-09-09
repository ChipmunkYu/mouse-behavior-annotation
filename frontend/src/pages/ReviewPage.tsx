/**
 * 审核工作台 /projects/:projectId/review：
 * - 队列列表（包含待审核 Submission 快照，但不预加载视频流）
 * - 选中视频的共享播放器 + 时间轴 + 只读标注列表
 * - 审核历史、意见输入、通过 / 退回（均有确认）
 * - 审核页只负责裁决；通过后的视频片段由后台继续生成
 * - 键盘可用：Space 播放/暂停、←/→ 步进一帧（输入框聚焦时不触发）
 * - 仅后端返回 can_review=true 的成员可访问
 */
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  createVideoReview,
  getBehaviorReviewState,
  listCategories,
  listProjects,
  listReviewQueue,
  listVideos,
  listVideoReviews,
  putBehaviorDecision,
  reopenBehaviorReview,
} from "../api";
import { ApiError, apiErrorDetail } from "../api/client";
import type { BehaviorReviewAnnotation, BehaviorReviewState, BehaviorReviewStatus, Category, Project, Review, SubmissionAnnotationSnapshot, Video } from "../api/types";
import { ROLE_LABELS, WORKFLOW_LABELS } from "../api/types";
import { Card, EmptyState, Loading, StatusBadge, WorkflowBadge } from "../components/ui";
import { useConfirm } from "../components/ConfirmDialog";
import { MediaLoadProgress } from "../components/MediaLoadProgress";
import Timeline from "../components/Timeline";
import DetectionOverlay from "../components/DetectionOverlay";
import { ParticipantSummary } from "../components/ParticipantSummary";
import { formatDate, formatTime, formatTimeShort } from "../utils/format";
import { useMediaSource } from "../media";
import { deriveReviewAnnotationView, deriveReviewOverlay, toggleReviewCategory } from "./reviewAnnotationView";

/* ================= 只读标注列表（审核视角，无编辑/删除） ================= */
function ReadOnlyAnnotationList({
  annotations,
  categoryById,
  activeAnnotationIds,
  focusedAnnotationId,
  filtered,
  onActivate,
  feedbackDrafts,
  failedFeedbackId,
  busyId,
  decisionOpen,
  revokeOpen,
  submissionStatus,
  onFeedbackChange,
  onDecision,
}: {
  annotations: BehaviorReviewAnnotation[];
  categoryById: Map<number, Category>;
  activeAnnotationIds: ReadonlySet<number>;
  focusedAnnotationId: number | null;
  filtered: boolean;
  onActivate: (annotation: SubmissionAnnotationSnapshot) => void;
  feedbackDrafts: Record<number, string>;
  failedFeedbackId: number | null;
  busyId: number | null;
  decisionOpen: boolean;
  revokeOpen: boolean;
  submissionStatus: string | null | undefined;
  onFeedbackChange: (id: number, value: string) => void;
  onDecision: (annotation: BehaviorReviewAnnotation, status: BehaviorReviewStatus) => void;
}) {
  if (annotations.length === 0) {
    return (
      <EmptyState compact title={filtered ? "当前筛选下暂无行为标注" : "暂无行为标注"} hint={filtered ? "调整类别筛选或选择“全部”" : "该视频尚未添加行为标注，无法通过"} />
    );
  }
  return (
    <div className="anno-list-body">
      {annotations.map((a) => {
        const cat = categoryById.get(a.category_id);
        const focused = focusedAnnotationId === a.id;
        const current = activeAnnotationIds.has(a.id);
        const feedback = feedbackDrafts[a.id] ?? "";
        const rejectionReady = feedback.trim().length > 0;
        const restriction = behaviorDecisionRestriction(submissionStatus);
        return (
          <div
            key={a.id}
            data-review-annotation-id={a.id}
            className={`anno-row review-anno-button${current ? " is-current" : ""}${focused ? " active" : ""}`}
            onClick={(event) => { if (event.target === event.currentTarget) onActivate(a); }}
          >
            <button type="button" className="review-annotation-focus" aria-current={focused ? "true" : undefined} aria-expanded={focused} aria-controls={focused ? `behavior-editor-${a.id}` : undefined} aria-label={`${a.category_name ?? `类别 ${a.category_id}`}，${formatTimeShort(a.start_time)} 至 ${formatTimeShort(a.end_time)}，参与对象 ${a.mouse_ids.join("、") || "无"}`} onClick={() => onActivate(a)}>
              <div className="anno-row-top">
              <span className="anno-cat" title={cat?.group ?? ""}>
                <span className="swatch" style={{ background: cat?.color ?? "var(--text-3)" }} />
                <span className="name">{a.category_name ?? `类别 #${a.category_id}`}</span>
              </span>
              <span className="anno-times">
                <b>{formatTimeShort(a.start_time)}</b> – <b>{formatTimeShort(a.end_time)}</b>
              </span>
              <span className="anno-row-actions">
                {focused ? <span className="review-focus-badge">聚焦中</span> : null}
                <StatusBadge value={a.decision.status} tone={a.decision.status === "approved" ? "ok" : a.decision.status === "rejected" ? "danger" : undefined} />
              </span>
              </div>
              <div className="anno-row-meta">
              <span>帧 {a.start_frame} → {a.end_frame}</span>
              <span>·</span>
              {a.category_group ? <span>{a.category_group}</span> : null}
              <ParticipantSummary mode={a.category_participant_mode} roles={a.role_definitions} assignments={a.participant_roles} mouseIds={a.mouse_ids} />
              </div>
            </button>
            {focused ? <div id={`behavior-editor-${a.id}`} className="behavior-decision-editor">
              <label htmlFor={`behavior-feedback-${a.id}`}>退回意见 <span className="behavior-required">退回时必填</span>{failedFeedbackId === a.id ? "（保存失败）" : a.decision.status === "rejected" ? "（已保存）" : ""}</label>
              <textarea id={`behavior-feedback-${a.id}`} className="textarea" rows={2} value={feedback} disabled={!decisionOpen} placeholder="说明此行为需要修改的内容" onChange={(event) => onFeedbackChange(a.id, event.target.value)} />
              {failedFeedbackId === a.id ? <div className="behavior-feedback-error" role="alert">保存失败 · 此意见尚未保存</div> : null}
              <div className="behavior-decision-actions">
                <div className="behavior-decision-primary" role="group" aria-label="此行为审核操作">
                  <button type="button" className="btn btn-sm btn-danger" disabled={!decisionOpen || !rejectionReady} title={rejectionReady ? "保存退回意见并退回此行为" : "请先填写退回意见"} onClick={() => onDecision(a, "rejected")}>{busyId === a.id ? "保存中…" : "退回此行为"}</button>
                  <button type="button" className="btn btn-sm btn-primary" disabled={!decisionOpen} onClick={() => onDecision(a, "approved")}>{busyId === a.id ? "保存中…" : "通过此行为"}</button>
                </div>
                <button type="button" className="btn btn-sm btn-ghost behavior-decision-revoke" disabled={(!decisionOpen && !revokeOpen) || a.decision.status === "pending"} onClick={() => onDecision(a, "pending")}>{busyId === a.id ? "保存中…" : "撤销裁决"}</button>
              </div>
              {restriction ? <div className="behavior-decision-restriction" role="note">{restriction}</div> : null}
              {a.decision.decided_at ? <div className="behavior-decision-meta">{a.decision.reviewer ?? "审核人"} · {formatDate(a.decision.decided_at)}{a.decision.origin === "carried" ? " · 沿用上次结果" : ""}</div> : null}
            </div> : null}
          </div>
        );
      })}
    </div>
  );
}

/* ================= 审核历史 ================= */
function ReviewHistory({ reviews }: { reviews: Review[] }) {
  const sorted = useMemo(
    () => [...reviews].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()),
    [reviews]
  );
  if (sorted.length === 0) {
    return <EmptyState compact title="暂无审核记录" hint="该视频尚未被审核" />;
  }
  return (
    <div className="review-history">
      {sorted.map((r) => (
        <div key={r.id} className="review-row">
          <div className="review-row-top">
            <StatusBadge value={r.result} tone={r.result === "approved" ? "ok" : "danger"} />
            <span className="review-rev mono">行为标注版本 v{r.annotation_revision} · 检测导入版本 {r.detection_import_revision} · track 修正版本 {r.identity_revision}</span>
            <span className="flex-spacer" />
            <span className="review-date">{formatDate(r.created_at)}</span>
          </div>
          {r.comment ? <div className="review-comment">{r.comment}</div> : null}
          <div className="review-meta">审核人 {r.reviewer ?? `#${r.reviewer_id}`}</div>
        </div>
      ))}
    </div>
  );
}

type QueueAction = "toggle" | "select" | "escape" | "close";
type RailTab = "queue" | "history";

export function reviewSubmitBlockers(error: unknown): number[] {
  const raw = apiErrorDetail(error);
  const detail = raw && typeof raw === "object" ? raw as { code?: unknown; items?: unknown } : null;
  if (detail?.code !== "rejected_annotations_not_addressed" || !Array.isArray(detail.items)) return [];
  return detail.items.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const row = item as { source_annotation_id?: unknown; submission_annotation_id?: unknown };
    const id = typeof row.source_annotation_id === "number" ? row.source_annotation_id : row.submission_annotation_id;
    return typeof id === "number" ? [id] : [];
  });
}

export function nextReviewQueueOpen(open: boolean, action: QueueAction): boolean {
  return action === "toggle" ? !open : false;
}

export function behaviorDecisionAccess(submissionStatus: string | null | undefined, busy: boolean) {
  return {
    decisionOpen: !busy && submissionStatus === "submitted",
    revokeOpen: !busy && (submissionStatus === "rejected" || submissionStatus === "withdrawn"),
  };
}

export function behaviorDecisionRestriction(submissionStatus: string | null | undefined): string | null {
  if (submissionStatus === "submitted") return null;
  if (submissionStatus === "rejected") return "视频已退回；当前只能撤销已有裁决为待审核。";
  if (submissionStatus === "withdrawn") return "提交已撤回；当前只能撤销已有裁决为待审核。";
  if (submissionStatus === "approved") return "视频已最终通过；如需调整，请先在左侧重新打开审核。";
  return "当前没有可裁决的提交。";
}

export function shouldClearBehaviorFocus(status: BehaviorReviewStatus): boolean {
  return status === "approved" || status === "rejected";
}

export function scheduleBehaviorNoticeDismiss(message: string, onDismiss: (message: string) => void): () => void {
  const timer = window.setTimeout(() => onDismiss(message), 2000);
  return () => window.clearTimeout(timer);
}

export function isReviewRelevantVideo(video: Video): boolean {
  return video.workflow_status !== "draft" || video.submitted_at != null || video.submission_annotations.length > 0;
}

export function sortReviewAnnotations<T extends { decision: { status: string } }>(annotations: T[]): T[] {
  return ["pending", "rejected", "approved"].flatMap((status) => annotations.filter((annotation) => annotation.decision.status === status));
}

export function resolveReviewSelection<T extends { id: number }>(annotations: T[], selectedId: number | null): number | null {
  return selectedId != null && annotations.some((annotation) => annotation.id === selectedId) ? selectedId : null;
}

/* ================= 审核工作台主页面 ================= */
export default function ReviewPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const pid = Number(projectId);

  const videoRef = useRef<HTMLVideoElement>(null);

  const [project, setProject] = useState<Project | null>(null);
  const [queue, setQueue] = useState<Video[] | null>(null);
  const [reviewVideos, setReviewVideos] = useState<Video[]>([]);
  const [queueOpen, setQueueOpen] = useState(true);
  const [railTab, setRailTab] = useState<RailTab>("queue");
  const [videoQuery, setVideoQuery] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [selectedVideo, setSelectedVideo] = useState<Video | null>(null);

  const [categories, setCategories] = useState<Category[]>([]);
  const [annotations, setAnnotations] = useState<BehaviorReviewAnnotation[]>([]);
  const [reviewState, setReviewState] = useState<BehaviorReviewState | null>(null);
  const [feedbackDrafts, setFeedbackDrafts] = useState<Record<number, string>>({});
  const [decisionBusyId, setDecisionBusyId] = useState<number | null>(null);
  const [failedFeedbackId, setFailedFeedbackId] = useState<number | null>(null);
  const [selectedCategoryIds, setSelectedCategoryIds] = useState<Set<number>>(new Set());
  const [focusedAnnotationId, setFocusedAnnotationId] = useState<number | null>(null);
  const [seekAnnouncement, setSeekAnnouncement] = useState("");
  const [reviews, setReviews] = useState<Review[]>([]);

  const [elementDuration, setElementDuration] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(false);

  const [comment, setComment] = useState("");
  const [reviewBusy, setReviewBusy] = useState(false);
  const reviewActionBusyRef = useRef(false);
  const behaviorNoticeCleanupRef = useRef<(() => void) | null>(null);
  const [reviewDisabled, setReviewDisabled] = useState(true);
  const selectGenRef = useRef(0);
  const selectedVideoRef = useRef<Video | null>(null);
  const queueTabRef = useRef<HTMLButtonElement>(null);
  const queuePanelRef = useRef<HTMLElement>(null);
  const mainHeadingRef = useRef<HTMLHeadingElement>(null);
  const focusMainAfterSelectionRef = useRef(false);

  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [colorNotice, setColorNotice] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmDialog, confirm] = useConfirm();
  const handleMediaReady = useCallback((_reason: "initial" | "retry-restored", element: HTMLVideoElement) => {
    setElementDuration(element.duration);
  }, []);
  const media = useMediaSource({ videoId: selectedId, surface: "review", videoRef, onReady: handleMediaReady });
  selectedVideoRef.current = selectedVideo;

  const clearBehaviorNoticeTimer = useCallback(() => {
    behaviorNoticeCleanupRef.current?.();
    behaviorNoticeCleanupRef.current = null;
  }, []);

  useEffect(() => clearBehaviorNoticeTimer, [clearBehaviorNoticeTimer]);

  const annotationView = useMemo(
    () => deriveReviewAnnotationView(annotations, categories, selectedCategoryIds),
    [annotations, categories, selectedCategoryIds]
  );
  const filteredAnnotations = annotationView.annotations;
  const sortedFilteredAnnotations = useMemo(() => sortReviewAnnotations(filteredAnnotations), [filteredAnnotations]);
  const selectedBehaviorId = useMemo(() => resolveReviewSelection(filteredAnnotations, focusedAnnotationId), [filteredAnnotations, focusedAnnotationId]);
  const categorySummaries = annotationView.categories;
  const categoryById = useMemo(
    () => new Map(categorySummaries.map((category) => [category.id, category] as const)),
    [categorySummaries]
  );

  const timelineDuration =
    elementDuration > 0 ? elementDuration : selectedVideo?.duration && selectedVideo.duration > 0 ? selectedVideo.duration : null;

  const canReview = project?.can_review === true;
  const overlayState = useMemo(
    () => deriveReviewOverlay(filteredAnnotations, currentTime, focusedAnnotationId),
    [filteredAnnotations, currentTime, focusedAnnotationId]
  );
  const focusedSnapshot = overlayState.focusedAnnotationId == null
    ? undefined
    : filteredAnnotations.find((annotation) => annotation.id === overlayState.focusedAnnotationId);
  const activeAnnotationIds = useMemo(() => new Set(overlayState.activeAnnotationIds), [overlayState.activeAnnotationIds]);
  const visibleReviewVideos = useMemo(() => {
    const query = videoQuery.trim().toLowerCase();
    return query ? reviewVideos.filter((video) => video.filename.toLowerCase().includes(query)) : reviewVideos;
  }, [reviewVideos, videoQuery]);

  useEffect(() => {
    if (!queueOpen || !window.matchMedia("(max-width: 700px)").matches) return;
    const frame = window.requestAnimationFrame(() => {
      const panel = queuePanelRef.current;
      const target = panel?.querySelector<HTMLButtonElement>("[data-queue-current='true']")
        ?? panel?.querySelector<HTMLButtonElement>("[data-queue-item]")
        ?? panel?.querySelector<HTMLButtonElement>("[data-queue-close]");
      target?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [queueOpen, queue]);

  useEffect(() => {
    if (queueOpen || !focusMainAfterSelectionRef.current || selectedId == null) return;
    focusMainAfterSelectionRef.current = false;
    mainHeadingRef.current?.focus();
  }, [queueOpen, selectedId]);

  /* ---------- 数据加载 ---------- */
  const loadQueue = useCallback(async () => {
    try {
      const projs = await listProjects();
      const currentProject = projs.find((p) => p.id === pid) ?? null;
      setProject(currentProject);
      if (!currentProject?.can_review) {
        setQueue([]);
        setErrorMsg(null);
        return;
      }
      const queued = await listReviewQueue(pid);
      const sorted = [...queued].sort((a, b) => {
        const ta = a.submitted_at ? new Date(a.submitted_at).getTime() : 0;
        const tb = b.submitted_at ? new Date(b.submitted_at).getTime() : 0;
        return ta - tb;
      });
      setQueue(sorted);
      try {
        // `view=all` also preserves withdrawn submissions that currently project as draft.
        const videos = (await listVideos(pid, { view: "all" })).filter(isReviewRelevantVideo);
        const byId = new Map(videos.map((video) => [video.id, video]));
        sorted.forEach((video) => byId.set(video.id, video));
        setReviewVideos([...byId.values()]);
      } catch {
        setReviewVideos(sorted);
      }
      setErrorMsg(null);
    } catch (err) {
      setQueue([]);
      setErrorMsg(err instanceof Error ? err.message : "加载审核队列失败");
    }
  }, [pid]);

  useEffect(() => {
    void loadQueue();
  }, [loadQueue]);

  /* 选中视频：从队列读取 Submission 快照，并加载类别颜色、审核历史与视频流。
   * selectedVideo 由 selectVideo 显式设置：审核通过后队列刷新不再覆盖已选视频，
   * 便于继续核对本次裁决。 */
  const selectVideo = useCallback((v: Video) => {
    clearBehaviorNoticeTimer();
    setNotice(null);
    setSelectedId(v.id);
    setSelectedVideo(v);
    focusMainAfterSelectionRef.current = true;
    setQueueOpen((open) => nextReviewQueueOpen(open, "select"));
    setSearchParams({ video: String(v.id) }, { replace: true });
  }, [clearBehaviorNoticeTimer, setSearchParams]);

  useEffect(() => {
    const requested = searchParams.get("video");
    if (requested == null) return;
    const requestedId = Number(requested);
    if (!Number.isInteger(requestedId) || requestedId === selectedId) return;
    const requestedVideo = reviewVideos.find((video) => video.id === requestedId);
    if (requestedVideo) selectVideo(requestedVideo);
  }, [reviewVideos, searchParams, selectVideo, selectedId]);

  function closeQueue(returnFocus = true) {
    setQueueOpen((open) => nextReviewQueueOpen(open, "close"));
    if (returnFocus) window.requestAnimationFrame(() => queueTabRef.current?.focus());
  }

  function handleQueueKeyDown(e: ReactKeyboardEvent<HTMLElement>) {
    if (e.key !== "Tab" || !window.matchMedia("(max-width: 700px)").matches) return;
    const focusable = [...(e.currentTarget.querySelector(".review-nav-panel") ?? e.currentTarget).querySelectorAll<HTMLElement>("button:not(:disabled), [href], input:not(:disabled), [tabindex]:not([tabindex='-1'])")];
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  useEffect(() => {
    if (selectedId == null) {
      setQueueOpen(true);
      setSelectedVideo(null);
      setAnnotations([]);
      setReviewState(null);
      setFeedbackDrafts({});
      setDecisionBusyId(null);
      setFailedFeedbackId(null);
      setCategories([]);
      setSelectedCategoryIds(new Set());
      setFocusedAnnotationId(null);
      setSeekAnnouncement("");
      setColorNotice(null);
      setReviews([]);
      setElementDuration(0);
      setCurrentTime(0);
      setPlaying(false);
      setReviewDisabled(true);
      return;
    }
    let cancelled = false;
    const vid = selectedId;
    const gen = ++selectGenRef.current;

    setNotice(null);
    setErrorMsg(null);
    setColorNotice(null);
    setElementDuration(0);
    setCurrentTime(0);
    setPlaying(false);
    setReviewDisabled(true);
    setAnnotations([]);
    setReviewState(null);
    setFeedbackDrafts({});
    setDecisionBusyId(null);
    setFailedFeedbackId(null);
    setCategories([]);
    setSelectedCategoryIds(new Set());
    setFocusedAnnotationId(null);
    setSeekAnnouncement("");
    setReviews([]);

    Promise.all([listVideoReviews(pid, vid), getBehaviorReviewState(pid, vid).catch((error) => {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    })])
      .then(([revs, state]) => {
        if (cancelled || gen !== selectGenRef.current) return;
        setReviews(revs);
        setReviewState(state);
        setAnnotations(state?.annotations ?? []);
        setFeedbackDrafts(Object.fromEntries((state?.annotations ?? []).map((annotation) => [annotation.id, annotation.decision.feedback ?? ""])));
        setReviewDisabled(false);
      })
      .catch((err: unknown) => {
        if (cancelled || gen !== selectGenRef.current) return;
        setErrorMsg(err instanceof Error ? err.message : "加载审核数据失败");
      });

    listCategories(pid)
      .then((loadedCategories) => {
        if (cancelled || gen !== selectGenRef.current) return;
        setCategories(loadedCategories);
      })
      .catch(() => {
        if (cancelled || gen !== selectGenRef.current) return;
        setCategories([]);
        setColorNotice("类别颜色暂不可用，已使用灰色显示");
      });

    return () => {
      cancelled = true;
    };
  }, [selectedId, pid]);

  async function saveBehaviorDecision(annotation: BehaviorReviewAnnotation, status: BehaviorReviewStatus) {
    if (!reviewState?.submission_id || reviewActionBusyRef.current || selectedId == null) return;
    const operationVideoId = selectedId;
    const feedback = (feedbackDrafts[annotation.id] ?? "").trim();
    if (status === "rejected" && !feedback) {
      setErrorMsg("退回此行为时请填写意见");
      return;
    }
    reviewActionBusyRef.current = true;
    clearBehaviorNoticeTimer();
    setNotice(null);
    setDecisionBusyId(annotation.id);
    setFailedFeedbackId(null);
    setErrorMsg(null);
    try {
      const refreshed = await putBehaviorDecision(pid, operationVideoId, reviewState.submission_id, annotation.id, {
        status,
        feedback: status === "rejected" ? feedback : null,
        expected_decision_revision: reviewState.decision_revision,
      });
      if (operationVideoId !== selectedVideoRef.current?.id) return;
      setReviewState(refreshed);
      setAnnotations(refreshed.annotations);
      setFeedbackDrafts((drafts) => ({ ...Object.fromEntries(refreshed.annotations.map((item) => [item.id, item.decision.feedback ?? ""])), ...drafts, [annotation.id]: refreshed.annotations.find((item) => item.id === annotation.id)?.decision.feedback ?? "" }));
      const decisionNotice = status === "approved" ? "此行为已通过" : status === "rejected" ? "此行为已退回，意见已保存" : "已撤销此行为裁决";
      setNotice(decisionNotice);
      behaviorNoticeCleanupRef.current = scheduleBehaviorNoticeDismiss(decisionNotice, (dismissed) => setNotice((current) => current === dismissed ? null : current));
      if (shouldClearBehaviorFocus(status)) {
        setFocusedAnnotationId(null);
        setSeekAnnouncement(status === "approved" ? "行为已通过，已关闭操作区" : "行为已退回，已关闭操作区");
        window.requestAnimationFrame(() => document.querySelector<HTMLButtonElement>(`[data-review-annotation-id="${annotation.id}"] .review-annotation-focus`)?.focus());
      }
    } catch (err) {
      const draft = feedbackDrafts[annotation.id] ?? "";
      if (operationVideoId !== selectedVideoRef.current?.id) return;
      if (err instanceof ApiError && err.status === 409) {
        try {
          const refreshed = await getBehaviorReviewState(pid, operationVideoId);
          if (operationVideoId !== selectedVideoRef.current?.id) return;
          setReviewState(refreshed);
          setAnnotations(refreshed.annotations);
          setFeedbackDrafts((drafts) => ({ ...Object.fromEntries(refreshed.annotations.map((item) => [item.id, item.decision.feedback ?? ""])), ...drafts, [annotation.id]: draft }));
          setErrorMsg("审核状态已变化，已刷新；未保存的意见仍在，请重试");
        } catch {
          setErrorMsg("审核状态已变化，刷新失败；未保存的意见仍在");
        }
      } else {
        setErrorMsg(err instanceof Error ? `${err.message}；此意见尚未保存` : "保存裁决失败；此意见尚未保存");
      }
      setFailedFeedbackId(annotation.id);
    } finally {
      reviewActionBusyRef.current = false;
      setDecisionBusyId(null);
    }
  }
  /* ---------- 播放控制 ---------- */
  function togglePlay() {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) void v.play();
    else v.pause();
  }

  function step(dir: 1 | -1) {
    const v = videoRef.current;
    if (!v) return;
    const fps = selectedVideo?.fps && selectedVideo.fps > 0 ? selectedVideo.fps : 30;
    const dt = 1 / fps;
    const next = Math.min(Math.max(0, v.currentTime + dir * dt), v.duration || Number.MAX_VALUE);
    v.currentTime = next;
  }

  function seekTo(t: number) {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = Math.min(Math.max(0, t), v.duration || t);
    setCurrentTime(v.currentTime);
  }

  function activateAnnotation(annotation: SubmissionAnnotationSnapshot) {
    setFocusedAnnotationId(annotation.id);
    seekTo(annotation.start_time);
    setSeekAnnouncement(`已聚焦 ${annotation.category_name ?? `类别 ${annotation.category_id}`}，${formatTime(annotation.start_time)}，参与对象 ${annotation.mouse_ids.map((id) => `Track ${id}`).join("、") || "无"}`);
  }

  function exitAnnotationFocus() {
    setFocusedAnnotationId(null);
    setSeekAnnouncement("已退出聚焦，显示当前重叠行为参与对象并集");
  }

  function changeCategoryFilter(categoryId: number | null) {
    if (focusedAnnotationId != null) exitAnnotationFocus();
    setSelectedCategoryIds(toggleReviewCategory(selectedCategoryIds, categoryId));
  }

  /* ---------- 键盘快捷键（输入框聚焦时不触发） ---------- */
  const keyHandlerRef = useRef<(e: KeyboardEvent) => void>(() => {});
  keyHandlerRef.current = (e: KeyboardEvent) => {
    function isEditable(target: EventTarget | null): boolean {
      if (!(target instanceof HTMLElement)) return false;
      const tag = target.tagName;
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || tag === "BUTTON" || target.isContentEditable;
    }
    // 确认对话框打开时不响应页面快捷键（对话框内部处理 Esc / Enter）。
    if (document.querySelector(".modal-overlay")) return;
    if (e.code === "Escape" && queueOpen && window.matchMedia("(max-width: 700px)").matches) {
      e.preventDefault();
      setQueueOpen((open) => nextReviewQueueOpen(open, "escape"));
      window.requestAnimationFrame(() => queueTabRef.current?.focus());
      return;
    }
    if (e.code === "Escape" && focusedAnnotationId != null) {
      if (isEditable(e.target) && (e.target as HTMLElement).tagName !== "BUTTON") return;
      e.preventDefault();
      exitAnnotationFocus();
      return;
    }
    if (e.code === "Space") {
      if (isEditable(e.target)) return;
      e.preventDefault();
      if (!e.repeat) togglePlay();
      return;
    }
    if (isEditable(e.target)) return;
    if (e.repeat) return;
    switch (e.code) {
      case "ArrowLeft":
        e.preventDefault();
        step(-1);
        break;
      case "ArrowRight":
        e.preventDefault();
        step(1);
        break;
      default:
        break;
    }
  };

  useEffect(() => {
    const handler = (e: KeyboardEvent) => keyHandlerRef.current(e);
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  /* ---------- 通过 / 退回 ---------- */
  async function handleReview(result: "approved" | "rejected") {
    if (!selectedVideo || !reviewState?.submission_id || reviewActionBusyRef.current) return;
    const operationVideoId = selectedVideo.id;
    if (result === "approved" && !reviewState.can_finalize_approval) {
      setErrorMsg("仅当现有行为全部通过后，才能通过视频");
      return;
    }
    const ok = await confirm({
      title: result === "approved" ? "确认通过该视频？" : "确认退回该视频？",
      message:
        result === "approved" ? (
          <>
            这是视频最终通过。通过后现有行为保持锁定；如需再改，须先明确重新打开审核。
          </>
        ) : (
          <>可在未完成全部行为裁决时退回视频。已通过行为继续锁定，退回意见与对应行为一并保留。</>
        ),
      confirmLabel: result === "approved" ? "确认通过" : "确认退回",
      danger: result === "rejected",
    });
    if (!ok) return;

    reviewActionBusyRef.current = true;
    setReviewBusy(true);
    setErrorMsg(null);
    try {
      await createVideoReview(pid, operationVideoId, {
        result,
        comment: comment.trim() || null,
        expected_submission_id: reviewState.submission_id,
        expected_decision_revision: reviewState.decision_revision,
      });
      const [refreshed, refreshedReviews] = await Promise.all([
        getBehaviorReviewState(pid, operationVideoId),
        listVideoReviews(pid, operationVideoId),
      ]);
      if (operationVideoId !== selectedVideoRef.current?.id) return;
      setReviewState(refreshed);
      setAnnotations(refreshed.annotations);
      setReviews(refreshedReviews);
      // 裁决后保留当前视频详情，并立即投影服务端已确认的工作流状态。
      setSelectedVideo((prev) => prev ? {
        ...prev,
        workflow_status: result,
        approved_at: result === "approved" ? new Date().toISOString() : null,
      } : prev);
      setNotice(
        result === "approved"
          ? `已最终通过：${selectedVideo.filename}。`
          : `已退回：${selectedVideo.filename}。已通过行为仍保持锁定。`
      );
      setComment("");
      await loadQueue();
    } catch (err) {
      if (operationVideoId !== selectedVideoRef.current?.id) return;
      const blockers = reviewSubmitBlockers(err);
      if (err instanceof ApiError && err.status === 409) {
        try {
          const refreshed = await getBehaviorReviewState(pid, operationVideoId);
          if (operationVideoId !== selectedVideoRef.current?.id) return;
          setReviewState(refreshed);
          setAnnotations(refreshed.annotations);
          setErrorMsg(blockers.length ? `仍有退回行为未处理：标注 #${blockers.join("、#")}` : "审核状态已变化，已刷新；视频说明仍在，请重试");
        } catch {
          setErrorMsg("审核状态已变化，刷新失败；视频说明仍在");
        }
      } else setErrorMsg(blockers.length ? `仍有退回行为未处理：标注 #${blockers.join("、#")}` : err instanceof Error ? err.message : "提交审核失败");
    } finally {
      reviewActionBusyRef.current = false;
      setReviewBusy(false);
    }
  }

  async function handleReopen() {
    if (!selectedVideo || !reviewState?.submission_id || !reviewState.can_reopen || reviewActionBusyRef.current) return;
    const operationVideoId = selectedVideo.id;
    const ok = await confirm({ title: "重新打开审核？", message: "重新打开后可撤销单条通过。已通过行为在撤销前仍保持锁定。", confirmLabel: "重新打开" });
    if (!ok) return;
    reviewActionBusyRef.current = true;
    setReviewBusy(true);
    try {
      const refreshed = await reopenBehaviorReview(pid, operationVideoId, reviewState.submission_id, { reason: comment.trim() || null });
      if (operationVideoId !== selectedVideoRef.current?.id) return;
      setReviewState(refreshed);
      setAnnotations(refreshed.annotations);
      setFeedbackDrafts(Object.fromEntries(refreshed.annotations.map((item) => [item.id, item.decision.feedback ?? ""])));
      setNotice("审核已重新打开");
      setSelectedVideo((video) => video ? { ...video, workflow_status: "draft", approved_at: null } : video);
      await loadQueue();
    } catch (err) {
      if (operationVideoId !== selectedVideoRef.current?.id) return;
      if (err instanceof ApiError && err.status === 409) {
        try {
          const refreshed = await getBehaviorReviewState(pid, operationVideoId);
          if (operationVideoId !== selectedVideoRef.current?.id) return;
          setReviewState(refreshed);
          setAnnotations(refreshed.annotations);
          setErrorMsg("审核状态已变化，已刷新；重新打开原因仍在，请重试");
        } catch { setErrorMsg("审核状态已变化，刷新失败；重新打开原因仍在"); }
      } else setErrorMsg(err instanceof Error ? err.message : "重新打开失败");
    } finally {
      reviewActionBusyRef.current = false;
      setReviewBusy(false);
    }
  }

  const videoReady = media.status === "ready";
  const decisionAccess = behaviorDecisionAccess(reviewState?.submission_status, reviewBusy || decisionBusyId != null || reviewDisabled);

  return (
    <div className="review-page">
      <div className="annotate-header">
        <Link to={`/projects/${pid}/videos`} className="btn btn-sm btn-ghost" title="返回视频库">
          ← 视频库
        </Link>
        <h1>
          <Link to={`/projects/${pid}/videos`} className="crumb-link" title={project?.name ?? undefined}>
            {project?.name ?? `项目 #${pid}`}
          </Link>
          <span className="crumb-sep">/</span>
          <span className="crumb-current">审核工作台</span>
        </h1>
        {project ? (
          <div className="workflow-chip">
            <span className="workflow-meta">我的角色：{ROLE_LABELS[project.role] ?? "未知角色"}</span>
          </div>
        ) : null}
        <div className="actions">
          <button ref={queueTabRef} type="button" className="btn btn-sm review-rail-trigger" aria-controls="review-queue-panel" onClick={() => setQueueOpen((open) => nextReviewQueueOpen(open, "toggle"))}>视频列表</button>
          <button type="button" className="btn btn-sm" onClick={() => void loadQueue()}>
            刷新
          </button>
        </div>
      </div>

      {notice ? (
        <div className="ok-box" role="status">✓ {notice}</div>
      ) : null}
      {errorMsg ? <div className="error-box" role="alert">⚠ {errorMsg}</div> : null}
      {colorNotice ? <div className="review-color-notice" role="status">{colorNotice}</div> : null}
      <span className="sr-only" aria-live="polite">{seekAnnouncement}</span>
      {confirmDialog}

      {project && !canReview ? (
        <Card>
          <EmptyState
            title="当前角色无法审核"
            hint={`你在该项目中的角色为「${ROLE_LABELS[project.role] ?? "未知角色"}」，当前未启用审核权限。请联系项目管理员。`}
          />
        </Card>
      ) : (
        <div className="review-body">
          {queueOpen ? <button type="button" className="review-queue-backdrop" tabIndex={-1} aria-label="关闭审核队列" onClick={() => closeQueue()} /> : null}
          <aside
            id="review-queue-panel"
            ref={queuePanelRef}
            className={`review-rail${queueOpen ? " queue-open" : ""}`}
            onKeyDown={handleQueueKeyDown}
          >
            <div className="review-nav-panel">
              <div className="review-rail-tabs" role="tablist" aria-label="选择审核视频">
                <button type="button" role="tab" aria-selected={railTab === "queue"} className={railTab === "queue" ? "active" : ""} onClick={() => setRailTab("queue")}>待审核队列 <b>{queue?.length ?? 0}</b></button>
                <button type="button" role="tab" aria-selected={railTab === "history"} className={railTab === "history" ? "active" : ""} onClick={() => setRailTab("history")}>查找历史/复核视频</button>
              </div>
              <Card title={railTab === "queue" ? `待审核队列（${queue?.length ?? 0}）` : `全部相关视频（${reviewVideos.length}）`} extra={<button type="button" className="btn btn-sm btn-ghost review-drawer-close" data-queue-close onClick={() => closeQueue()}>关闭</button>}>
                {railTab === "history" ? <input className="input review-video-search" type="search" value={videoQuery} onChange={(event) => setVideoQuery(event.target.value)} placeholder="按文件名查找…" aria-label="查找历史或复核视频" /> : null}
                {queue === null ? <Loading text="加载视频…" /> : (railTab === "queue" ? queue : visibleReviewVideos).length === 0 ? (
                  <EmptyState compact title={railTab === "queue" ? "队列为空" : "未找到视频"} hint={railTab === "queue" ? "暂无待审核视频。可切换到历史/复核视频。" : "清除搜索词后重试。"} />
                ) : (
                  <div className="review-queue" aria-label={railTab === "queue" ? "待审核队列" : "历史与复核视频"}>
                    {(railTab === "queue" ? queue : visibleReviewVideos).map((v) => (
                      <button key={v.id} type="button" data-queue-item data-queue-current={selectedId === v.id ? "true" : undefined} className={selectedId === v.id ? "queue-item active" : "queue-item"} aria-current={selectedId === v.id ? "true" : undefined} onClick={() => selectVideo(v)} title={v.filename}>
                        <span className="queue-name" title={v.filename}>{v.filename}</span>
                        <span className="queue-meta"><WorkflowBadge value={v.workflow_status} revision={v.annotation_revision} /><span className="queue-date">{selectedId === v.id && reviewState?.submission_status === "withdrawn" ? "提交已撤回" : v.submitted_at ? formatDate(v.submitted_at) : WORKFLOW_LABELS[v.workflow_status] ?? v.workflow_status}</span></span>
                      </button>
                    ))}
                  </div>
                )}
              </Card>
            </div>
            {selectedId == null ? (
              <Card title="审核意见"><EmptyState compact title="尚未选择视频" hint="打开审核队列选择待审核视频" /></Card>
            ) : (
              <Card title="审核裁决" className="review-decision-panel">
                <div className="field review-comment-field">
                  <label htmlFor="review-comment">{reviewState?.can_reopen ? "重新打开原因（可选）" : "视频说明（可选）"}</label>
                  <textarea
                    id="review-comment"
                    className="textarea"
                    rows={5}
                    value={comment}
                    placeholder={reviewState?.can_reopen ? "说明为什么需要重新打开本次最终通过" : "这里只填写视频整体说明；单条意见请写在对应行为下"}
                    onChange={(e) => setComment(e.target.value)}
                  />
                </div>
                <div className="review-actions">
                  {reviewState?.can_reopen ? <button type="button" className="btn" disabled={reviewBusy || decisionBusyId != null} onClick={() => void handleReopen()}>{reviewBusy ? "处理中…" : "重新打开审核"}</button> : <>
                    <button type="button" className="btn btn-danger" disabled={!decisionAccess.decisionOpen} onClick={() => void handleReview("rejected")}>{reviewBusy ? "提交中…" : "退回视频"}</button>
                    <button type="button" className="btn btn-primary" disabled={!decisionAccess.decisionOpen || !reviewState?.can_finalize_approval} title={!reviewState?.can_finalize_approval ? "现有行为全部通过后才能最终通过视频" : "最终通过视频"} onClick={() => void handleReview("approved")}>{reviewBusy ? "提交中…" : "最终通过视频"}</button>
                  </>}
                </div>
                {!reviewDisabled && !reviewState?.submission_id ? <div className="frame-preview">暂无可审核的提交。</div> : null}
                {annotations.length === 0 ? <div className="frame-preview">该视频暂无行为标注，不能通过；可退回或等待标注者补充。</div> : null}
                <details className="review-history-details">
                  <summary>审核历史（{reviews.length}）</summary>
                  <div className="review-history-scroll"><ReviewHistory reviews={reviews} /></div>
                </details>
              </Card>
            )}
          </aside>

          {/* 选中视频详情 */}
          <section className="review-main">
            {selectedId == null ? (
              <Card>
                <EmptyState
                  title="请选择要审核的视频"
                  hint="从左侧队列选择视频后，将加载其播放器、行为标注与审核历史。每处理一个视频都会从队列移除。"
                />
              </Card>
            ) : (
              <>
                <h2 ref={mainHeadingRef} className="review-current-heading" tabIndex={-1}>{selectedVideo?.filename ?? `视频 #${selectedId}`}</h2>
                <div className="card review-player">
                  <div className="video-wrap">
                    <video
                      ref={videoRef}
                      className={videoReady ? "" : "media-player-pending"}
                      onClick={togglePlay}
                      title="点击播放 / 暂停 [Space]"
                      onTimeUpdate={(e) => setCurrentTime(e.currentTarget.currentTime)}
                      onPlay={() => setPlaying(true)}
                      onPause={() => setPlaying(false)}
                      playsInline
                      preload="metadata"
                    />
                    {videoReady ? <DetectionOverlay projectId={pid} videoId={selectedId} video={videoRef.current} currentTime={currentTime} fallbackFps={selectedVideo?.fps} selectedIds={overlayState.mouseIds} showOnlySelected trackRoleLabels={overlayState.roleLabels} /> : null}
                    <MediaLoadProgress state={media} onCancel={media.cancel} />
                    {media.status === "pending" || media.status === "failed" || media.status === "cancelled" ? <div className="media-status-overlay"><EmptyState compact title={media.status === "pending" ? "播放资源处理中" : media.status === "cancelled" ? "下载已取消" : "视频下载失败"} hint={media.message} /><button type="button" className="btn btn-sm" onClick={media.reload}>{media.status === "cancelled" ? "重新下载" : "重试"}</button></div> : null}
                  </div>
                  {videoReady ? (
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
                        <span className="revision-context mono">Submission 快照 · 只读</span>
                        <WorkflowBadge value={selectedVideo?.workflow_status ?? "draft"} revision={selectedVideo?.annotation_revision} />
                      </div>
                      {focusedSnapshot ? (
                        <div className="review-focus-status" role="status">
                          <span><b>聚焦：</b>{focusedSnapshot.category_name ?? `类别 #${focusedSnapshot.category_id}`} · {formatTimeShort(focusedSnapshot.start_time)}–{formatTimeShort(focusedSnapshot.end_time)}</span>
                          <button type="button" className="btn btn-sm btn-ghost" onClick={exitAnnotationFocus} aria-label="退出单条行为聚焦">退出聚焦</button>
                        </div>
                      ) : overlayState.activeAnnotationIds.length > 1 ? (
                        <div className="review-union-status" role="status">当前 {overlayState.activeAnnotationIds.length} 条重叠行为 · 显示参与对象并集</div>
                      ) : null}
                      {timelineDuration && timelineDuration > 0 ? (
                        <div style={{ padding: "0 10px 10px" }}>
                          <Timeline
                            duration={timelineDuration}
                            currentTime={currentTime}
                            annotations={filteredAnnotations}
                            categoryById={categoryById}
                            focusedAnnotationId={overlayState.focusedAnnotationId}
                            onSeek={seekTo}
                          />
                        </div>
                      ) : (
                        <div className="frame-preview" style={{ padding: "0 10px 10px", color: "var(--text-3)" }}>
                          暂无时长信息，时间轴不可用
                        </div>
                      )}
                    </>
                  ) : null}
                </div>

              </>
            )}
          </section>

          <aside className="review-behaviors" aria-label="行为审核列表">
            {selectedId == null ? <Card><EmptyState compact title="行为列表" hint="选择视频后在这里逐条审核行为。" /></Card> : <>
              {reviewState ? <div className="behavior-review-counts" aria-label="行为审核进度"><span><b>{reviewState.counts.pending}</b> 待审核</span><span className="approved"><b>{reviewState.counts.approved}</b> 已通过</span><span className="rejected"><b>{reviewState.counts.rejected}</b> 已退回</span></div> : null}
              <section className="review-category-overview" aria-label="按行为类别筛选">
                  <div className="review-overview-heading">
                    <strong>行为概览</strong>
                    <span>共 {annotations.length} 条 · {categorySummaries.length} 类</span>
                  </div>
                  <div className="review-category-filters">
                    <button
                      type="button"
                      className={selectedCategoryIds.size === 0 ? "review-filter active" : "review-filter"}
                      aria-pressed={selectedCategoryIds.size === 0}
                      onClick={() => changeCategoryFilter(null)}
                    >
                      全部 <b>{annotations.length}</b>
                    </button>
                    {categorySummaries.map((category) => (
                      <button
                        key={category.id}
                        type="button"
                        className={selectedCategoryIds.has(category.id) ? "review-filter active" : "review-filter"}
                        aria-pressed={selectedCategoryIds.has(category.id)}
                        aria-label={`${category.name}，${category.count} 条`}
                        title={category.name}
                        onClick={() => changeCategoryFilter(category.id)}
                      >
                        <span className="swatch" style={{ background: category.color ?? "var(--text-3)" }} aria-hidden="true" />
                        <span className="review-filter-name">{category.name}</span>
                        <b>{category.count}</b>
                      </button>
                    ))}
                  </div>
              </section>

              <Card title={`行为标注（${filteredAnnotations.length} / ${annotations.length}）· 只读`} className="review-anns">
                <ReadOnlyAnnotationList
                    annotations={sortedFilteredAnnotations}
                    categoryById={categoryById}
                    activeAnnotationIds={activeAnnotationIds}
                    focusedAnnotationId={selectedBehaviorId}
                    filtered={selectedCategoryIds.size > 0}
                    onActivate={activateAnnotation}
                    feedbackDrafts={feedbackDrafts}
                    failedFeedbackId={failedFeedbackId}
                    busyId={decisionBusyId}
                    decisionOpen={decisionAccess.decisionOpen}
                    revokeOpen={decisionAccess.revokeOpen}
                    submissionStatus={reviewState?.submission_status}
                    onFeedbackChange={(id, value) => { setFeedbackDrafts((drafts) => ({ ...drafts, [id]: value })); if (failedFeedbackId === id) setFailedFeedbackId(null); }}
                    onDecision={(annotation, status) => void saveBehaviorDecision(annotation, status)}
                />
              </Card>
            </>}
          </aside>
        </div>
      )}
    </div>
  );
}
