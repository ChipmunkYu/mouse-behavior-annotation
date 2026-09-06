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
import { Link, useParams } from "react-router-dom";
import {
  createVideoReview,
  listCategories,
  listProjects,
  listReviewQueue,
  listVideoReviews,
} from "../api";
import type { Category, Project, Review, SubmissionAnnotationSnapshot, Video } from "../api/types";
import { ROLE_LABELS } from "../api/types";
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
}: {
  annotations: SubmissionAnnotationSnapshot[];
  categoryById: Map<number, Category>;
  activeAnnotationIds: ReadonlySet<number>;
  focusedAnnotationId: number | null;
  filtered: boolean;
  onActivate: (annotation: SubmissionAnnotationSnapshot) => void;
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
        return (
          <button
            key={a.id}
            type="button"
            className={`anno-row review-anno-button${current ? " is-current" : ""}${focused ? " active" : ""}`}
            aria-current={focused ? "true" : undefined}
            aria-label={`${a.category_name ?? `类别 ${a.category_id}`}，${formatTimeShort(a.start_time)} 至 ${formatTimeShort(a.end_time)}，参与对象 ${a.mouse_ids.join("、") || "无"}`}
            onClick={() => onActivate(a)}
          >
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
                <StatusBadge value="pending" />
              </span>
            </div>
            <div className="anno-row-meta">
              <span>帧 {a.start_frame} → {a.end_frame}</span>
              <span>·</span>
              {a.category_group ? <span>{a.category_group}</span> : null}
              <ParticipantSummary mode={a.category_participant_mode} roles={a.role_definitions} assignments={a.participant_roles} mouseIds={a.mouse_ids} />
            </div>
          </button>
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
type BehaviorSummaryMode = "none" | "single" | "overlap" | "focused";

export function nextReviewQueueOpen(open: boolean, action: QueueAction): boolean {
  return action === "toggle" ? !open : false;
}

export function deriveReviewBehaviorSummary(
  annotations: SubmissionAnnotationSnapshot[],
  activeAnnotationIds: number[],
  focusedAnnotationId: number | null,
): { mode: BehaviorSummaryMode; items: SubmissionAnnotationSnapshot[]; categories: string[]; mouseIds: number[] } {
  const activeIds = new Set(activeAnnotationIds);
  const active = annotations.filter((annotation) => activeIds.has(annotation.id));
  const focused = focusedAnnotationId == null ? undefined : active.find((annotation) => annotation.id === focusedAnnotationId);
  const items = focused ? [focused] : active;
  return {
    mode: focused ? "focused" : active.length === 0 ? "none" : active.length === 1 ? "single" : "overlap",
    items,
    categories: [...new Set(items.map((annotation) => annotation.category_name ?? `类别 #${annotation.category_id}`))],
    mouseIds: [...new Set(items.flatMap((annotation) => annotation.mouse_ids))].sort((a, b) => a - b),
  };
}

function CurrentBehaviorSummary({
  summary,
  onExitFocus,
}: {
  summary: ReturnType<typeof deriveReviewBehaviorSummary>;
  onExitFocus: () => void;
}) {
  if (summary.mode === "none") {
    return <div className="review-current-empty">当前时刻无行为</div>;
  }
  return (
    <section className="review-current-behavior" aria-label="当前行为摘要">
      <div className="review-current-behavior-head">
        <strong>{summary.mode === "focused" ? "聚焦中" : summary.mode === "overlap" ? `当前 ${summary.items.length} 条重叠行为` : "当前行为"}</strong>
        {summary.mode === "focused" ? <button type="button" className="btn btn-sm btn-ghost" onClick={onExitFocus}>退出聚焦</button> : null}
      </div>
      <div className="review-current-facts">
        <span>类别：{summary.categories.join("、")}</span>
        <span>参与对象：{summary.mouseIds.length ? summary.mouseIds.map((id) => `Track ${id}`).join("、") : "无"}</span>
      </div>
      <div className="review-current-events">
        {summary.items.map((annotation) => (
          <div key={annotation.id} className="review-current-event">
            <div><b>{annotation.category_name ?? `类别 #${annotation.category_id}`}</b><span>{formatTimeShort(annotation.start_time)}–{formatTimeShort(annotation.end_time)}</span></div>
            <ParticipantSummary mode={annotation.category_participant_mode} roles={annotation.role_definitions} assignments={annotation.participant_roles} mouseIds={annotation.mouse_ids} compact />
          </div>
        ))}
      </div>
    </section>
  );
}

/* ================= 审核工作台主页面 ================= */
export default function ReviewPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const pid = Number(projectId);

  const videoRef = useRef<HTMLVideoElement>(null);

  const [project, setProject] = useState<Project | null>(null);
  const [queue, setQueue] = useState<Video[] | null>(null);
  const [queueOpen, setQueueOpen] = useState(true);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [selectedVideo, setSelectedVideo] = useState<Video | null>(null);

  const [categories, setCategories] = useState<Category[]>([]);
  const [annotations, setAnnotations] = useState<SubmissionAnnotationSnapshot[]>([]);
  const [selectedCategoryIds, setSelectedCategoryIds] = useState<Set<number>>(new Set());
  const [focusedAnnotationId, setFocusedAnnotationId] = useState<number | null>(null);
  const [seekAnnouncement, setSeekAnnouncement] = useState("");
  const [reviews, setReviews] = useState<Review[]>([]);

  const [elementDuration, setElementDuration] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(false);

  const [comment, setComment] = useState("");
  const [reviewBusy, setReviewBusy] = useState(false);
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

  const annotationView = useMemo(
    () => deriveReviewAnnotationView(annotations, categories, selectedCategoryIds),
    [annotations, categories, selectedCategoryIds]
  );
  const filteredAnnotations = annotationView.annotations;
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
  const behaviorSummary = useMemo(
    () => deriveReviewBehaviorSummary(filteredAnnotations, overlayState.activeAnnotationIds, overlayState.focusedAnnotationId),
    [filteredAnnotations, overlayState.activeAnnotationIds, overlayState.focusedAnnotationId]
  );

  useEffect(() => {
    if (!queueOpen) return;
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

  useEffect(() => {
    if (focusedAnnotationId != null && overlayState.focusedAnnotationId == null) {
      setFocusedAnnotationId(null);
      setSeekAnnouncement("已退出聚焦，显示当前重叠行为参与对象并集");
    }
  }, [focusedAnnotationId, overlayState.focusedAnnotationId]);

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
    setSelectedId(v.id);
    setSelectedVideo(v);
    focusMainAfterSelectionRef.current = true;
    setQueueOpen((open) => nextReviewQueueOpen(open, "select"));
  }, []);

  function closeQueue(returnFocus = true) {
    setQueueOpen((open) => nextReviewQueueOpen(open, "close"));
    if (returnFocus) window.requestAnimationFrame(() => queueTabRef.current?.focus());
  }

  function handleQueueKeyDown(e: ReactKeyboardEvent<HTMLElement>) {
    if (e.key !== "Tab" || !window.matchMedia("(max-width: 960px)").matches) return;
    const focusable = [...e.currentTarget.querySelectorAll<HTMLElement>("button:not(:disabled), [href], textarea:not(:disabled), [tabindex]:not([tabindex='-1'])")];
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
    const snapshots = selectedVideoRef.current?.submission_annotations ?? [];
    setAnnotations(snapshots);
    setCategories([]);
    setSelectedCategoryIds(new Set());
    setFocusedAnnotationId(null);
    setSeekAnnouncement("");
    setReviews([]);

    listVideoReviews(pid, vid)
      .then((revs) => {
        if (cancelled || gen !== selectGenRef.current) return;
        setReviews(revs);
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
    if (e.code === "Escape" && queueOpen) {
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
    if (!selectedVideo) return;
    if (reviewBusy) return;
    if (result === "rejected" && comment.trim() === "") {
      setErrorMsg("退回时请填写意见，说明需要修改的内容");
      return;
    }
    const ok = await confirm({
      title: result === "approved" ? "确认通过该视频？" : "确认退回该视频？",
      message:
        result === "approved" ? (
          <>
            通过后该视频审核完成、行为标注将被锁定，系统将在后台自动开始<b>生成视频片段</b>。
          </>
        ) : (
          <>退回后该视频将返回标注者修改，本次审核意见将保留在历史记录中。修改行为标注将使其回到草稿并需要重新提交。</>
        ),
      confirmLabel: result === "approved" ? "确认通过" : "确认退回",
      danger: result === "rejected",
    });
    if (!ok) return;

    setReviewBusy(true);
    setErrorMsg(null);
    try {
      await createVideoReview(pid, selectedVideo.id, {
        result,
        comment: comment.trim() || null,
      });
      if (result === "approved") {
        // 通过后保留当前视频详情，便于核对本次裁决结果。
        setSelectedVideo((prev) =>
          prev
            ? { ...prev, workflow_status: "approved", approved_at: new Date().toISOString() }
            : prev
        );
      }
      setNotice(
        result === "approved"
          ? `已通过：${selectedVideo.filename}。视频片段将在后台生成。`
          : `已退回：${selectedVideo.filename}，标注者将收到意见并修改。`
      );
      setComment("");
      await loadQueue();
      if (result === "rejected") setSelectedId(null);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "提交审核失败");
    } finally {
      setReviewBusy(false);
    }
  }

  const videoReady = media.status === "ready";

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
          <button type="button" className="btn btn-sm" onClick={() => void loadQueue()}>
            刷新队列
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
            <div className="review-rail-tabs" role="tablist" aria-label="审核视图">
              <button
                ref={queueTabRef}
                type="button"
                role="tab"
                aria-selected={queueOpen}
                aria-controls="review-queue-panel"
                className={queueOpen ? "active" : ""}
                onClick={() => setQueueOpen(true)}
              >
                审核队列 {queue?.length ?? 0}
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={!queueOpen}
                className={!queueOpen ? "active" : ""}
                onClick={() => setQueueOpen(false)}
              >
                审核裁决
              </button>
            </div>
            {queueOpen ? (
              <Card
                title={`审核队列（${queue?.length ?? 0}）`}
                extra={<button type="button" className="btn btn-sm btn-ghost" data-queue-close onClick={() => closeQueue()}>关闭</button>}
              >
                {queue === null ? (
                  <Loading text="加载队列…" />
                ) : queue.length === 0 ? (
                  <EmptyState compact title="队列为空" hint="暂无待审核视频。标注者提交审核后会出现在这里。" />
                ) : (
                  <div className="review-queue" aria-label="审核队列">
                    {queue.map((v) => (
                      <button
                        key={v.id}
                        type="button"
                        data-queue-item
                        data-queue-current={selectedId === v.id ? "true" : undefined}
                        className={selectedId === v.id ? "queue-item active" : "queue-item"}
                        aria-current={selectedId === v.id ? "true" : undefined}
                        onClick={() => selectVideo(v)}
                        title={v.filename}
                      >
                        <span className="queue-name" title={v.filename}>{v.filename}</span>
                        <span className="queue-meta">
                          <WorkflowBadge value={v.workflow_status} revision={v.annotation_revision} />
                          <span className="queue-date">{v.submitted_at ? formatDate(v.submitted_at) : "—"}</span>
                        </span>
                      </button>
                    ))}
                  </div>
                )}
              </Card>
            ) : selectedId == null ? (
              <Card title="审核意见"><EmptyState compact title="尚未选择视频" hint="打开审核队列选择待审核视频" /></Card>
            ) : (
              <Card title="审核裁决" className="review-decision-panel">
                <CurrentBehaviorSummary summary={behaviorSummary} onExitFocus={exitAnnotationFocus} />
                <div className="field review-comment-field">
                  <label htmlFor="review-comment">审核意见（退回时必填，通过时可选）</label>
                  <textarea
                    id="review-comment"
                    className="textarea"
                    rows={5}
                    value={comment}
                    placeholder="例如：第 2 条行为标注起点偏晚，请重新校准后再提交"
                    onChange={(e) => setComment(e.target.value)}
                  />
                </div>
                <div className="review-actions">
                  <button type="button" className="btn btn-danger" disabled={reviewBusy || reviewDisabled} onClick={() => void handleReview("rejected")}>{reviewBusy ? "提交中…" : "退回"}</button>
                  <button type="button" className="btn btn-primary" disabled={reviewBusy || reviewDisabled || annotations.length === 0} title={annotations.length === 0 ? "该视频暂无行为标注，无法通过" : reviewDisabled ? "审核数据加载中" : "通过该视频"} onClick={() => void handleReview("approved")}>{reviewBusy ? "提交中…" : "通过"}</button>
                </div>
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
                    annotations={filteredAnnotations}
                    categoryById={categoryById}
                    activeAnnotationIds={activeAnnotationIds}
                    focusedAnnotationId={overlayState.focusedAnnotationId}
                    filtered={selectedCategoryIds.size > 0}
                    onActivate={activateAnnotation}
                  />
                </Card>
              </>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
