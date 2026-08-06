/**
 * 审核工作台 /projects/:projectId/review：
 * - 队列列表（仅元数据，避免一次加载所有视频的标注与流）
 * - 选中视频的共享播放器 + 时间轴 + 只读标注列表
 * - 审核历史、意见输入、通过 / 退回（均有确认）
 * - 通过后自动开始片段生成：展示「审核已通过，片段生成已排队/处理中」，
 *   媒体状态面板轮询 media-status 直至任务落定（失败可重试生成）
 * - 键盘可用：Space 播放/暂停、←/→ 步进一帧（输入框聚焦时不触发）
 * - 仅 owner / admin / reviewer 角色可见入口（项目内角色由 project API 提供）
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  createVideoReview,
  fetchVideoStreamUrl,
  listAnnotations,
  listCategories,
  listProjects,
  listReviewQueue,
  listVideoReviews,
} from "../api";
import { ApiError } from "../api/client";
import type { Annotation, Category, Project, Review, Video } from "../api/types";
import { ROLE_LABELS } from "../api/types";
import { Card, EmptyState, Loading, StatusBadge, WorkflowBadge } from "../components/ui";
import { useConfirm } from "../components/ConfirmDialog";
import { MediaStatusPanel } from "../components/MediaStatusPanel";
import Timeline from "../components/Timeline";
import { formatDate, formatTime, formatTimeShort } from "../utils/format";

type StreamState = "idle" | "loading" | "ok" | "empty" | "error";

/* ================= 只读标注列表（审核视角，无编辑/删除） ================= */
function ReadOnlyAnnotationList({
  annotations,
  categoryById,
}: {
  annotations: Annotation[];
  categoryById: Map<number, Category>;
}) {
  if (annotations.length === 0) {
    return (
      <EmptyState compact title="暂无标注" hint="该视频尚未添加标注，无法通过" />
    );
  }
  return (
    <div className="anno-list-body">
      {annotations.map((a) => {
        const cat = categoryById.get(a.category_id);
        return (
          <div key={a.id} className="anno-row">
            <div className="anno-row-top">
              <span className="anno-cat" title={cat?.group ?? ""}>
                <span className="swatch" style={{ background: cat?.color ?? "var(--text-3)" }} />
                <span className="name">{a.category_name ?? cat?.name ?? `类别 #${a.category_id}`}</span>
              </span>
              <span className="anno-times">
                <b>{formatTimeShort(a.start_time)}</b> – <b>{formatTimeShort(a.end_time)}</b>
              </span>
              <span className="anno-row-actions">
                <StatusBadge value={a.review_status} />
              </span>
            </div>
            <div className="anno-row-meta">
              <span>帧 {a.start_frame} → {a.end_frame}</span>
              <span>·</span>
              <span>标注者 {a.annotator ?? `#${a.annotator_id}`}</span>
              <span>·</span>
              <span>可信度 {a.confidence}</span>
            </div>
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
            <span className="review-rev mono">修订 v{r.annotation_revision}</span>
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

/* ================= 审核工作台主页面 ================= */
export default function ReviewPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const pid = Number(projectId);

  const videoRef = useRef<HTMLVideoElement>(null);

  const [project, setProject] = useState<Project | null>(null);
  const [queue, setQueue] = useState<Video[] | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [selectedVideo, setSelectedVideo] = useState<Video | null>(null);

  const [categories, setCategories] = useState<Category[]>([]);
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [reviews, setReviews] = useState<Review[]>([]);

  const [streamState, setStreamState] = useState<StreamState>("idle");
  const [streamUrl, setStreamUrl] = useState<string | null>(null);
  const [elementDuration, setElementDuration] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(false);

  const [comment, setComment] = useState("");
  const [reviewBusy, setReviewBusy] = useState(false);

  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmDialog, confirm] = useConfirm();

  const categoryById = useMemo(
    () => new Map(categories.map((c) => [c.id, c] as const)),
    [categories]
  );

  const timelineDuration =
    elementDuration > 0 ? elementDuration : selectedVideo?.duration && selectedVideo.duration > 0 ? selectedVideo.duration : null;

  const canReview = project ? ["owner", "admin", "reviewer"].includes(project.role) : true;

  /* ---------- 数据加载 ---------- */
  const loadQueue = useCallback(async () => {
    try {
      const [projs, queued] = await Promise.all([listProjects(), listReviewQueue(pid)]);
      setProject(projs.find((p) => p.id === pid) ?? null);
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

  /* 选中视频：只加载该视频的标注 / 类别 / 审核历史 / 视频流，避免一次加载全部。
   * selectedVideo 由 selectVideo 显式设置：审核通过后队列刷新不再覆盖已选视频，
   * 便于在详情中继续查看片段生成进度。 */
  const selectVideo = useCallback((v: Video) => {
    setSelectedId(v.id);
    setSelectedVideo(v);
  }, []);

  useEffect(() => {
    if (selectedId == null) {
      setSelectedVideo(null);
      setAnnotations([]);
      setCategories([]);
      setReviews([]);
      setStreamUrl(null);
      setStreamState("idle");
      setElementDuration(0);
      setCurrentTime(0);
      setPlaying(false);
      return;
    }
    let cancelled = false;
    let url: string | null = null;
    const vid = selectedId;

    setNotice(null);
    setErrorMsg(null);
    setStreamState("loading");

    Promise.all([listAnnotations(pid, vid), listCategories(pid), listVideoReviews(pid, vid)])
      .then(([anns, cats, revs]) => {
        if (cancelled) return;
        setAnnotations(anns);
        setCategories(cats);
        setReviews(revs);
      })
      .catch((err: unknown) => {
        if (!cancelled) setErrorMsg(err instanceof Error ? err.message : "加载审核数据失败");
      });

    fetchVideoStreamUrl(vid)
      .then((u) => {
        if (cancelled) {
          URL.revokeObjectURL(u);
          return;
        }
        url = u;
        setStreamUrl(u);
        setStreamState("ok");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          setStreamState("empty");
        } else {
          setStreamState("error");
          setErrorMsg(err instanceof Error ? err.message : "视频流加载失败");
        }
      });

    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
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
  }

  /* ---------- 键盘快捷键（输入框聚焦时不触发） ---------- */
  const keyHandlerRef = useRef<(e: KeyboardEvent) => void>(() => {});
  keyHandlerRef.current = (e: KeyboardEvent) => {
    function isEditable(target: EventTarget | null): boolean {
      if (!(target instanceof HTMLElement)) return false;
      const tag = target.tagName;
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
    }
    if (e.code === "Space") {
      if (isEditable(e.target) || e.target instanceof HTMLButtonElement) return;
      e.preventDefault();
      if (!e.repeat) togglePlay();
      return;
    }
    if (isEditable(e.target)) return;
    if (e.repeat) return;
    // 确认对话框打开时不响应全局快捷键（对话框内部自行处理 Esc / 空格）
    if (document.querySelector(".modal-overlay")) return;
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
            通过后该视频审核完成、标注将被锁定，系统将自动开始<b>生成媒体（片段）</b>，
            任务会<b>排队 / 处理中</b>，可在下方查看进度；生成失败时可重试。
          </>
        ) : (
          <>退回后该视频将返回标注者修改，本次审核意见将保留在历史记录中。标注者的修改将使其回到草稿并需要重新提交。</>
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
        // 通过后保留当前视频在详情中，便于查看片段生成进度
        setSelectedVideo((prev) =>
          prev
            ? { ...prev, workflow_status: "approved", approved_at: new Date().toISOString() }
            : prev
        );
      }
      setNotice(
        result === "approved"
          ? `已通过：${selectedVideo.filename}。片段生成已排队 / 处理中，可在下方查看进度。`
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

  const videoReady = streamState === "ok";

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
            <span className="workflow-meta">我的角色：{ROLE_LABELS[project.role] ?? project.role}</span>
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
      {confirmDialog}

      {project && !canReview ? (
        <Card>
          <EmptyState
            title="当前角色无法审核"
            hint={`你在该项目中的角色为「${ROLE_LABELS[project.role] ?? project.role}」，仅 owner / admin / reviewer 可访问审核工作台。`}
          />
        </Card>
      ) : (
        <div className="review-body">
          {/* 队列 */}
          <aside className="review-side">
            <Card
              title={`审核队列（${queue?.length ?? 0}）`}
              extra={
                <span className="review-side-note" title="仅展示待审核视频的元数据，选中后按需加载">
                  待审核
                </span>
              }
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
                      className={selectedId === v.id ? "queue-item active" : "queue-item"}
                      onClick={() => selectVideo(v)}
                      title={v.filename}
                    >
                      <span className="queue-name" title={v.filename}>
                        {v.filename}
                      </span>
                      <span className="queue-meta">
                        <WorkflowBadge value={v.workflow_status} revision={v.annotation_revision} />
                        <span className="queue-date">{v.submitted_at ? formatDate(v.submitted_at) : "—"}</span>
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </Card>
          </aside>

          {/* 选中视频详情 */}
          <section className="review-main">
            {selectedId == null ? (
              <Card>
                <EmptyState
                  title="请选择要审核的视频"
                  hint="从左侧队列选择视频后，将加载其播放器、标注与审核历史。每处理一个视频都会从队列移除。"
                />
              </Card>
            ) : (
              <>
                <div className="card review-player">
                  {streamState === "idle" || streamState === "loading" ? (
                    <Loading text="视频流加载中…" />
                  ) : videoReady && streamUrl ? (
                    <>
                      <div className="video-wrap">
                        <video
                          ref={videoRef}
                          src={streamUrl}
                          onClick={togglePlay}
                          title="点击播放 / 暂停（或按 Space）"
                          onLoadedMetadata={(e) => setElementDuration(e.currentTarget.duration)}
                          onTimeUpdate={(e) => setCurrentTime(e.currentTarget.currentTime)}
                          onPlay={() => setPlaying(true)}
                          onPause={() => setPlaying(false)}
                          playsInline
                        />
                      </div>
                      <div className="player-controls">
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={(e) => {
                            e.currentTarget.blur();
                            togglePlay();
                          }}
                        >
                          {playing ? "⏸ 暂停" : "▶ 播放"}
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={(e) => {
                            e.currentTarget.blur();
                            step(-1);
                          }}
                        >
                          ⟨ 退一帧
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={(e) => {
                            e.currentTarget.blur();
                            step(1);
                          }}
                        >
                          进一帧 ⟩
                        </button>
                        <span className="time-display">
                          <b>{formatTime(currentTime)}</b> / {timelineDuration ? formatTime(timelineDuration) : "?"}
                        </span>
                        <span className="flex-spacer" />
                        <WorkflowBadge value={selectedVideo?.workflow_status ?? "draft"} revision={selectedVideo?.annotation_revision} />
                      </div>
                      {timelineDuration && timelineDuration > 0 ? (
                        <div style={{ padding: "0 10px 10px" }}>
                          <Timeline
                            duration={timelineDuration}
                            currentTime={currentTime}
                            annotations={annotations}
                            categoryById={categoryById}
                            onSeek={seekTo}
                          />
                        </div>
                      ) : (
                        <div className="frame-preview" style={{ padding: "0 10px 10px", color: "var(--text-3)" }}>
                          暂无时长信息，时间轴不可用
                        </div>
                      )}
                    </>
                  ) : (
                    <EmptyState
                      title={streamState === "empty" ? "无视频文件" : "视频流加载失败"}
                      hint={streamState === "empty" ? "该视频未配置存储文件，仅可查看标注与审核历史。" : "请确认后端已启动且视频文件路径合法"}
                    />
                  )}
                </div>

                <Card title="媒体片段生成" className="media-card">
                  <MediaStatusPanel
                    projectId={pid}
                    videoId={selectedId}
                    workflowStatus={selectedVideo?.workflow_status ?? "draft"}
                    retryable
                  />
                </Card>

                <div className="review-detail">
                  <Card title={`标注（${annotations.length}）· 只读`} className="review-anns">
                    <ReadOnlyAnnotationList annotations={annotations} categoryById={categoryById} />
                  </Card>

                  <div className="review-side-col">
                    <Card title={`审核历史（${reviews.length}）`} className="review-history-card">
                      <ReviewHistory reviews={reviews} />
                    </Card>

                    <Card title="审核意见" className="review-opinion">
                      <div className="field">
                        <label htmlFor="review-comment">意见（退回时必填，通过时可选）</label>
                        <textarea
                          id="review-comment"
                          className="textarea"
                          rows={4}
                          value={comment}
                          placeholder="例如：第 2 条标注起点偏晚，请重新校准后再提交"
                          onChange={(e) => setComment(e.target.value)}
                        />
                      </div>
                      <div className="review-actions">
                        <button
                          type="button"
                          className="btn btn-danger"
                          disabled={reviewBusy}
                          onClick={() => void handleReview("rejected")}
                        >
                          {reviewBusy ? "提交中…" : "退回"}
                        </button>
                        <button
                          type="button"
                          className="btn btn-primary"
                          disabled={reviewBusy || annotations.length === 0}
                          title={annotations.length === 0 ? "该视频暂无标注，无法通过" : "通过该视频"}
                          onClick={() => void handleReview("approved")}
                        >
                          {reviewBusy ? "提交中…" : "通过"}
                        </button>
                      </div>
                      {annotations.length === 0 ? (
                        <div className="frame-preview" style={{ marginTop: 8 }}>
                          该视频暂无标注，不能通过；可退回或等待标注者补充。
                        </div>
                      ) : null}
                    </Card>
                  </div>
                </div>
              </>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
