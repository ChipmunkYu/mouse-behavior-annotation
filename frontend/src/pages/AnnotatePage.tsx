import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type Ref } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  createAnnotation,
  deleteAnnotation,
  exportAnnotations,
  fetchVideoStreamUrl,
  listAnnotations,
  listCategories,
  listProjects,
  listVideos,
  submitVideoForReview,
  updateAnnotation,
} from "../api";
import { ApiError } from "../api/client";
import type {
  Annotation,
  AnnotationPatchInput,
  Category,
  Project,
  Video,
} from "../api/types";
import { ROLE_LABELS, WORKFLOW_LABELS } from "../api/types";
import { Card, EmptyState, Loading, WorkflowBadge } from "../components/ui";
import { useConfirm } from "../components/ConfirmDialog";
import { MediaStatusPanel } from "../components/MediaStatusPanel";
import Timeline from "../components/Timeline";
import { formatDate, formatTime, formatTimeShort, timeToFrame } from "../utils/format";

type SaveState = "idle" | "saving" | "saved" | "error";
type StreamState = "loading" | "ok" | "empty" | "error";
type Point = { time: number; frame: number };

/* ================= 播放器叠加层（P1 空层，预留 YOLO 框/骨架/空间标注） ================= */
function OverlayLayer() {
  return <div className="overlay-layer" aria-hidden="true" />;
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
  onSelect,
  disabled,
}: {
  categories: Category[];
  activeCategory: Category | null;
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
                  // 失焦：让后续 Space 回到“播放/暂停”，而不是重复触发本类别按钮
                  e.currentTarget.blur();
                  onSelect(c);
                }}
                title={`${c.group} · ${c.name}`}
              >
                <span className="swatch" style={{ background: c.color ?? "var(--text-3)" }} />
                {c.name}
              </button>
            ))}
          </div>
        </div>
      ))}
      </div>
      {activeCategory ? (
        <div className="frame-preview" style={{ marginTop: 10 }}>
          当前类别：<b>{activeCategory.name}</b> · 按 S 设起点、D 设终点提交
        </div>
      ) : null}
    </Card>
  );
}

/* ================= 标注段列表 ================= */
function AnnotationRow({
  ann,
  categoryById,
  active,
  busy,
  onEdit,
  onDelete,
  rowRef,
}: {
  ann: Annotation;
  categoryById: Map<number, Category>;
  active: boolean;
  busy: boolean;
  onEdit: () => void;
  onDelete: () => void;
  rowRef?: Ref<HTMLDivElement>;
}) {
  const cat = categoryById.get(ann.category_id);
  return (
    <div ref={rowRef} className={active ? "anno-row active" : "anno-row"}>
      <div className="anno-row-top">
        <span className="anno-cat" title={cat?.group ?? ""}>
          <span className="swatch" style={{ background: cat?.color ?? "var(--text-3)" }} />
          <span className="name">{ann.category_name ?? cat?.name ?? `类别 #${ann.category_id}`}</span>
        </span>
        <span className="anno-times">
          <b>{formatTimeShort(ann.start_time)}</b> – <b>{formatTimeShort(ann.end_time)}</b>
        </span>
        <span className="anno-row-actions">
          <button type="button" className="btn-link" disabled={busy} onClick={onEdit}>
            编辑
          </button>
          <button type="button" className="btn-link" disabled={busy} onClick={onDelete} style={{ color: "var(--danger)" }}>
            删除
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
          可信度 {ann.confidence} · 审核 {ann.review_status}
        </span>
      </div>
    </div>
  );
}

function AnnotationEditForm({
  ann,
  categories,
  fps,
  onCancel,
  onSave,
}: {
  ann: Annotation;
  categories: Category[];
  fps: number | null | undefined;
  onCancel: () => void;
  onSave: (patch: AnnotationPatchInput) => Promise<void>;
}) {
  const [categoryId, setCategoryId] = useState(ann.category_id);
  const [start, setStart] = useState(ann.start_time.toFixed(3));
  const [end, setEnd] = useState(ann.end_time.toFixed(3));
  const [localError, setLocalError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const groups = useMemo(() => groupCategories(categories), [categories]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const s = Number(start);
    const en = Number(end);
    if (!Number.isFinite(s) || !Number.isFinite(en)) {
      setLocalError("时间必须是数字");
      return;
    }
    if (s < 0 || en < 0) {
      setLocalError("时间不能为负");
      return;
    }
    if (en <= s) {
      setLocalError("结束时间必须大于开始时间");
      return;
    }
    setSaving(true);
    setLocalError(null);
    try {
      await onSave({
        category_id: categoryId,
        start_time: s,
        end_time: en,
        start_frame: timeToFrame(s, fps),
        end_frame: timeToFrame(en, fps),
      });
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : "保存失败");
      setSaving(false);
    }
  }

  const sNum = Number(start);
  const eNum = Number(end);

  return (
    <form className="anno-edit" onSubmit={submit}>
      <div className="field">
        <label htmlFor={`edit-cat-${ann.id}`}>类别</label>
        <select
          id={`edit-cat-${ann.id}`}
          className="select"
          value={categoryId}
          onChange={(e) => setCategoryId(Number(e.target.value))}
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
          <label htmlFor={`edit-start-${ann.id}`}>开始（秒）</label>
          <input
            id={`edit-start-${ann.id}`}
            className="input"
            type="number"
            min="0"
            step="0.01"
            value={start}
            onChange={(e) => setStart(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor={`edit-end-${ann.id}`}>结束（秒）</label>
          <input
            id={`edit-end-${ann.id}`}
            className="input"
            type="number"
            min="0"
            step="0.01"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
          />
        </div>
      </div>
      <div className="frame-preview">
        对应帧：{Number.isFinite(sNum) ? timeToFrame(sNum, fps) : "—"} →{" "}
        {Number.isFinite(eNum) ? timeToFrame(eNum, fps) : "—"}
      </div>
      <div className="form-error">{localError ?? ""}</div>
      <div className="actions">
        <button type="button" className="btn btn-sm" onClick={onCancel} disabled={saving}>
          取消
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
  currentTime,
  onEditSave,
  onDelete,
}: {
  annotations: Annotation[];
  categories: Category[];
  categoryById: Map<number, Category>;
  fps: number | null | undefined;
  currentTime: number;
  onEditSave: (id: number, patch: AnnotationPatchInput) => Promise<void>;
  onDelete: (ann: Annotation) => Promise<void>;
}) {
  const [editingId, setEditingId] = useState<number | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const activeId = annotations.find(
    (a) => currentTime >= a.start_time && currentTime <= a.end_time
  )?.id;

  const activeRowRef = useRef<HTMLDivElement | null>(null);

  // 播放/跳转时，若当前活动标注段位于折叠区之外，将其轻微滚入视野
  useEffect(() => {
    if (activeId == null) return;
    activeRowRef.current?.scrollIntoView({ block: "nearest" });
  }, [activeId]);

  async function handleDelete(ann: Annotation) {
    setBusyId(ann.id);
    try {
      await onDelete(ann);
      setEditingId(null);
    } catch {
      // 错误提示已由父级处理
    } finally {
      setBusyId(null);
    }
  }

  return (
    <Card title={`标注段（${annotations.length}）`}>
      {annotations.length === 0 ? (
        <EmptyState compact title="暂无标注" hint="播放视频，选好类别后按 S 设起点、D 设终点" />
      ) : (
        <div className="anno-list-body">
          {annotations.map((a) =>
            editingId === a.id ? (
              <AnnotationEditForm
                key={a.id}
                ann={a}
                categories={categories}
                fps={fps}
                onCancel={() => setEditingId(null)}
                onSave={async (patch) => {
                  await onEditSave(a.id, patch);
                  setEditingId(null);
                }}
              />
            ) : (
              <AnnotationRow
                key={a.id}
                ann={a}
                categoryById={categoryById}
                active={activeId === a.id}
                busy={busyId === a.id}
                onEdit={() => setEditingId(a.id)}
                onDelete={() => void handleDelete(a)}
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
  submitting,
  onSubmit,
}: {
  status: string;
  hasAnnotations: boolean;
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
      disabled={!hasAnnotations || submitting}
      title={hasAnnotations ? "提交审核后，审核人将按当前标注版本审核" : "至少需要一条标注才能提交审核"}
      onClick={onSubmit}
    >
      {submitting ? "提交中…" : "提交审核"}
    </button>
  );
}

/* ================= 标注工作台主页面 ================= */
export default function AnnotatePage() {
  const { projectId, videoId } = useParams<{ projectId: string; videoId: string }>();
  const pid = Number(projectId);
  const vid = Number(videoId);

  const videoRef = useRef<HTMLVideoElement>(null);

  const [project, setProject] = useState<Project | null>(null);
  const [video, setVideo] = useState<Video | null>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [loading, setLoading] = useState(true);

  const [streamState, setStreamState] = useState<StreamState>("loading");
  const [streamUrl, setStreamUrl] = useState<string | null>(null);
  const [elementDuration, setElementDuration] = useState(0);

  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [activeCategory, setActiveCategory] = useState<Category | null>(null);
  const [startPoint, setStartPoint] = useState<Point | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("idle");

  const [submitting, setSubmitting] = useState(false);
  const [confirmDialog, confirm] = useConfirm();

  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [hint, setHint] = useState("播放视频，选择类别后按 S 设起点、D 设终点；Space 播放/暂停，←/→ 步进一帧");

  // 从片段库跳回标注位置：?t=<秒> 定位播放头
  const [searchParams] = useSearchParams();
  const seekParamRaw = searchParams.get("t");
  const seekParam = Number(seekParamRaw);
  const hasSeekTarget = seekParamRaw != null && Number.isFinite(seekParam);
  const pendingSeekRef = useRef<number | null>(null);

  // 供键盘监听读取最新值（避免闭包过期）
  const latest = useRef({ startPoint, activeCategory, video });
  latest.current = { startPoint, activeCategory, video };

  const categoryById = useMemo(
    () => new Map(categories.map((c) => [c.id, c] as const)),
    [categories]
  );

  // 优先使用浏览器实际解析的媒体时长（elementDuration）作为时间轴基准，
  // DB 元数据时长仅作回退：避免元数据 duration 与真实播放时长不一致时时间轴错位。
  const timelineDuration =
    elementDuration > 0 ? elementDuration : video?.duration && video.duration > 0 ? video.duration : null;

  /* ---------- 数据加载 ---------- */
  const loadAnnotations = useCallback(async () => {
    try {
      setAnnotations(await listAnnotations(pid, vid));
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "加载标注失败");
    }
  }, [pid, vid]);

  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      const [projs, vids, cats, anns] = await Promise.all([
        listProjects(),
        listVideos(pid),
        listCategories(pid),
        listAnnotations(pid, vid),
      ]);
      setProject(projs.find((p) => p.id === pid) ?? null);
      setVideo(vids.find((v) => v.id === vid) ?? null);
      setCategories(cats);
      setAnnotations(anns);
      setErrorMsg(null);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "加载数据失败");
    } finally {
      setLoading(false);
    }
  }, [pid, vid]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  /* ---------- 视频元数据刷新（标注变更后工作流状态可能回到 draft） ---------- */
  const refreshVideo = useCallback(async () => {
    try {
      const vids = await listVideos(pid);
      const v = vids.find((item) => item.id === vid) ?? null;
      if (v) setVideo(v);
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
            该视频当前为「<b>{label}</b>」（修订 v{video?.annotation_revision ?? 1}）。
            修改标注将使其退回<b>草稿</b>，已有审核结果将<b>失效</b>，已有片段（如有）将被删除，需要重新提交审核。
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
      setErrorMsg("至少需要一条标注才能提交审核");
      return;
    }
    const ok = await confirm({
      title: "提交审核？",
      message: (
        <>
          提交后该视频将进入审核队列，审核通过前标注将被锁定（修改需经确认并退回草稿）。
          <br />
          当前共 <b>{annotations.length}</b> 条标注，修订号 <b>v{video.annotation_revision ?? 1}</b>。
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
  }, [video, annotations.length, pid, vid, confirm]);

  /* ---------- 视频流（带 token 拉取 blob） ---------- */
  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    setStreamState("loading");
    setStreamUrl(null);
    setElementDuration(0);

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
  }, [vid]);

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

  function step(dir: 1 | -1) {
    const v = videoRef.current;
    if (!v) return;
    const fps = latest.current.video?.fps && latest.current.video.fps > 0 ? latest.current.video.fps : 30;
    const dt = 1 / fps;
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
    const v = videoRef.current;
    if (!v) return;
    const time = v.currentTime;
    const frame = timeToFrame(time, latest.current.video?.fps);
    setStartPoint({ time, frame });
    setErrorMsg(null);
    setHint(`起点已设置 ${formatTime(time)}（帧 ${frame}），到终点时按 D 提交`);
  }

  async function markEnd() {
    const v = videoRef.current;
    if (!v) return;
    const sp = latest.current.startPoint;
    const cat = latest.current.activeCategory;
    if (!cat) {
      setErrorMsg("请先选择行为类别");
      setHint("");
      return;
    }
    if (!sp) {
      setErrorMsg("请先按 S 或点击「设起点」设置起点");
      setHint("");
      return;
    }
    const time = videoRef.current?.currentTime ?? 0;
    if (time <= sp.time) {
      setErrorMsg("终点必须晚于起点，请先前进再设终点");
      return;
    }
    const frame = timeToFrame(time, latest.current.video?.fps);
    // 已提交 / 已通过 / 已退回的视频新增标注前需确认（会退回草稿、审核失效）
    if (!(await guardMutation("新增标注"))) {
      setStartPoint(null);
      setHint("");
      return;
    }
    setSaveState("saving");
    try {
      await createAnnotation(pid, vid, {
        category_id: cat.id,
        start_time: sp.time,
        end_time: time,
        start_frame: sp.frame,
        end_frame: frame,
        confidence: "certain",
      });
      setSaveState("saved");
      setStartPoint(null);
      setErrorMsg(null);
      const wasLocked = (video?.workflow_status ?? "draft") !== "draft";
      setHint(
        wasLocked
          ? `已标注：${cat.name} ${formatTime(sp.time)} → ${formatTime(time)}（视频已退回草稿，请重新提交审核）`
          : `已标注：${cat.name} ${formatTime(sp.time)} → ${formatTime(time)}（帧 ${sp.frame}→${frame}）`
      );
      await loadAnnotations();
      if (wasLocked) await refreshVideo();
    } catch (err) {
      setSaveState("error");
      setErrorMsg(err instanceof Error ? err.message : "保存标注失败");
    }
  }

  /* ---------- 列表编辑 / 删除 / 导出 ---------- */
  async function handleEditSave(id: number, patch: AnnotationPatchInput) {
    if (!(await guardMutation("编辑标注"))) return;
    try {
      await updateAnnotation(pid, vid, id, patch);
      setSaveState("saved");
      const wasLocked = (video?.workflow_status ?? "draft") !== "draft";
      setHint(wasLocked ? "标注已更新，视频已退回草稿，请重新提交审核" : "标注已更新");
      await loadAnnotations();
      if (wasLocked) await refreshVideo();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "保存标注失败");
      throw err;
    }
  }

  async function handleDelete(ann: Annotation) {
    const item = `${ann.category_name ?? `#${ann.category_id}`}（${formatTimeShort(ann.start_time)}–${formatTimeShort(ann.end_time)}）`;
    const status = video?.workflow_status ?? "draft";
    const locked = status !== "draft" && status !== "";
    const ok = await confirm({
      title: "删除标注？",
      message: locked ? (
        <>
          将删除标注「{item}」。该视频当前为「<b>{WORKFLOW_LABELS[status] ?? status}</b>」状态：
          删除后视频将退回<b>草稿</b>，已有审核结果失效，已有片段（如有）将被删除。
        </>
      ) : (
        <>将删除标注「{item}」，此操作不可撤销。</>
      ),
      confirmLabel: "删除",
      danger: true,
    });
    if (!ok) return;
    try {
      await deleteAnnotation(pid, vid, ann.id);
      setSaveState("saved");
      setHint(locked ? "标注已删除，视频已退回草稿，请重新提交审核" : "标注已删除");
      await loadAnnotations();
      if (locked) await refreshVideo();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "删除标注失败");
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
      setHint(`已导出 ${events.length} 条标注事件 JSON`);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "导出失败");
    }
  }

  /* ---------- 键盘快捷键（输入聚焦时不触发） ----------
   * 处理函数通过 ref 持有最新闭包：effect 仅注册一次，但每次渲染都更新 ref，
   * 避免路由参数变化（组件未卸载）时 S/D 等快捷键继续作用于旧的 pid/vid。
   */
  const keyHandlerRef = useRef<(e: KeyboardEvent) => void>(() => {});
  keyHandlerRef.current = (e: KeyboardEvent) => {
    function isEditable(target: EventTarget | null): boolean {
      if (!(target instanceof HTMLElement)) return false;
      const tag = target.tagName;
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
    }

    if (e.code === "Space") {
      // 输入框 / 按钮聚焦时交给默认行为（按钮会触发 click）
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
      case "KeyS":
        e.preventDefault();
        markStart();
        break;
      case "KeyD":
        e.preventDefault();
        void markEnd();
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

  const videoReady = streamState === "ok";

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
        {video ? (
          <div className="workflow-chip" title="审核工作流状态与修订号">
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
          <SubmitReviewControl
            status={video?.workflow_status ?? "draft"}
            hasAnnotations={annotations.length > 0}
            submitting={submitting}
            onSubmit={() => void handleSubmitReview()}
          />
          <button type="button" className="btn btn-sm" onClick={() => void loadAnnotations()}>
            刷新标注
          </button>
          <button type="button" className="btn btn-sm" onClick={() => void handleExport()}>
            导出 JSON
          </button>
        </div>
      </div>

      {errorMsg ? <div className="error-box" role="alert">⚠ {errorMsg}</div> : null}
      {confirmDialog}

      <div className="annotate-body">
        <section className="annotate-main">
          <div className="card player-card">
            {loading ? (
              <Loading text="加载标注数据…" />
            ) : streamState === "loading" ? (
              <Loading text="视频流加载中…" />
            ) : videoReady && streamUrl ? (
              <>
                <div className="video-wrap">
                  <video
                    ref={videoRef}
                    src={streamUrl}
                    onClick={togglePlay}
                    title="点击播放 / 暂停（或按 Space）"
                    onLoadedMetadata={(e) => {
                      setElementDuration(e.currentTarget.duration);
                      if (pendingSeekRef.current != null) {
                        e.currentTarget.currentTime = Math.min(pendingSeekRef.current, e.currentTarget.duration);
                        setCurrentTime(e.currentTarget.currentTime);
                        pendingSeekRef.current = null;
                      }
                    }}
                    onTimeUpdate={(e) => setCurrentTime(e.currentTarget.currentTime)}
                    onPlay={() => setPlaying(true)}
                    onPause={() => setPlaying(false)}
                    playsInline
                  />
                  <OverlayLayer />
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
                  <button
                    type="button"
                    className={startPoint ? "btn btn-sm btn-point armed" : "btn btn-sm btn-point"}
                    onClick={(e) => {
                      e.currentTarget.blur();
                      markStart();
                    }}
                    title="在当前时间设置起点"
                  >
                    设起点 (S)
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm btn-point"
                    onClick={(e) => {
                      e.currentTarget.blur();
                      void markEnd();
                    }}
                    title="在当前时间设置终点并保存标注"
                  >
                    设终点 (D)
                  </button>
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
                    暂无时长信息，时间轴不可用（可在视频库中补充 duration 元数据）
                  </div>
                )}
              </>
            ) : (
              <EmptyState
                title={streamState === "empty" ? "无视频文件" : "视频流加载失败"}
                hint={
                  streamState === "empty"
                    ? "该视频未配置 storage_path 或文件不存在。可先在视频库中补全元数据与文件路径，或直接在下方标注列表管理已有标注。"
                    : "请确认后端已启动且视频文件路径合法"
                }
              />
            )}
          </div>

          <div className="statusbar">
            <span>
              时间 <b className="mono">{formatTime(currentTime)}</b>
            </span>
            <span>
              起点{" "}
              <b className="mono">
                {startPoint ? formatTime(startPoint.time) : "未设置"}
              </b>
            </span>
            <span>
              终点{" "}
              <b className="mono">
                {startPoint ? formatTime(currentTime) : "未设置"}
              </b>
            </span>
            <span className="flex-spacer" />
            <span className="hint">{hint}</span>
            <SaveStatus state={saveState} message={errorMsg} />
          </div>
        </section>

        <aside className="annotate-side">
          {video?.workflow_status === "approved" ? (
            <Card title="媒体片段生成" className="media-card">
              {/* 已通过：只读展示片段生成状态（无生成 / 重试按钮），轮询在有任务进行时自动继续 */}
              <MediaStatusPanel
                projectId={pid}
                videoId={vid}
                workflowStatus="approved"
                retryable={false}
              />
            </Card>
          ) : null}
          <CategoryPanel
            categories={categories}
            activeCategory={activeCategory}
            onSelect={setActiveCategory}
            disabled={!videoReady}
          />
          <AnnotationList
            annotations={annotations}
            categories={categories}
            categoryById={categoryById}
            fps={video?.fps}
            currentTime={currentTime}
            onEditSave={handleEditSave}
            onDelete={handleDelete}
          />
          {project ? (
            <div className="frame-preview" style={{ color: "var(--text-3)" }}>
              项目 {project.name} · 我的角色：{ROLE_LABELS[project.role] ?? project.role}
            </div>
          ) : null}
        </aside>
      </div>
    </div>
  );
}
