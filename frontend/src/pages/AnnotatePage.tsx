import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type Ref } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  createAnnotation,
  deleteAnnotation,
  exportAnnotations,
  fetchVideoStreamUrl,
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
} from "../api/types";
import { ROLE_LABELS, WORKFLOW_LABELS } from "../api/types";
import { Card, EmptyState, Loading, WorkflowBadge, statusLabel } from "../components/ui";
import { useConfirm } from "../components/ConfirmDialog";
import { MediaStatusPanel } from "../components/MediaStatusPanel";
import Timeline from "../components/Timeline";
import DetectionOverlay from "../components/DetectionOverlay";
import { formatDate, formatTime, formatTimeShort, timeToFrame } from "../utils/format";

type SaveState = "idle" | "saving" | "saved" | "error";
type StreamState = "loading" | "ok" | "empty" | "error";
type Point = { time: number; frame: number };

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

/* ================= 标注区间列表 ================= */
function AnnotationRow({
  ann,
  categoryById,
  active,
  busy,
  readOnly,
  onEdit,
  onDelete,
  rowRef,
}: {
  ann: Annotation;
  categoryById: Map<number, Category>;
  active: boolean;
  busy: boolean;
  readOnly: boolean;
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
          <button type="button" className="btn-link" disabled={busy || readOnly} onClick={onEdit}>
            编辑
          </button>
          <button type="button" className="btn-link" disabled={busy || readOnly} onClick={onDelete} style={{ color: "var(--danger)" }}>
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
          可信度 {statusLabel(ann.confidence)} · 审核 {statusLabel(ann.review_status)}
        </span>
        <span>· 参与对象 {ann.mouse_ids.length ? ann.mouse_ids.map((id) => `track ID ${id}`).join("、") : "待补选"}</span>
        {ann.mouse_id_status === "needs_mouse_ids" ? <span className="mouse-status warning">需补选</span> : null}
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
  const [mouseIds, setMouseIds] = useState(ann.mouse_ids.join(", "));
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
        mouse_ids: [...new Set(mouseIds.split(/[,，\s]+/).filter(Boolean).map(Number))].filter(Number.isInteger).sort((a, b) => a - b),
        detection_import_revision: ann.detection_import_revision,
        identity_revision: ann.identity_revision,
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
      <div className="field"><label htmlFor={`edit-mice-${ann.id}`}>参与对象（填写 track ID，以逗号分隔）</label><input id={`edit-mice-${ann.id}`} className="input mono" value={mouseIds} onChange={(e) => setMouseIds(e.target.value)} /></div>
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
  readOnly,
  onEditSave,
  onDelete,
}: {
  annotations: Annotation[];
  categories: Category[];
  categoryById: Map<number, Category>;
  fps: number | null | undefined;
  currentTime: number;
  readOnly: boolean;
  onEditSave: (id: number, patch: AnnotationPatchInput) => Promise<void>;
  onDelete: (ann: Annotation) => Promise<void>;
}) {
  const [editingId, setEditingId] = useState<number | null>(null);
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
      setEditingId(null);
    } catch {
      // 错误提示已由父级处理
    } finally {
      setBusyId(null);
    }
  }

  return (
    <Card title={`标注区间（${annotations.length}）`}>
      {annotations.length === 0 ? (
        <EmptyState compact title="暂无行为标注" hint="播放视频，选好类别后按 S 设起点、D 设终点" />
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
                readOnly={readOnly}
                onEdit={() => { if (!readOnly) setEditingId(a.id); }}
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
      title={hasAnnotations ? "提交审核后，审核人将按当前行为标注版本审核" : "至少需要一条行为标注才能提交审核"}
      onClick={onSubmit}
    >
      {submitting ? "提交中…" : "提交审核"}
    </button>
  );
}

function MouseIdsPanel({ tracks, selected, category, disabled, onToggle }: {
  tracks: CorrectedTrackSummary[];
  selected: number[];
  category: Category | null;
  disabled: boolean;
  onToggle: (id: number) => void;
}) {
  const min = category?.mouse_count_min ?? 1;
  const max = category?.mouse_count_max ?? null;
  const valid = selected.length >= min && (max == null || selected.length <= max);
  const rule = max == null ? `至少 ${min} 个对象` : min === max ? `恰好 ${min} 个对象` : `${min}–${max} 个对象`;
  return <Card title="参与对象" className="mouse-ids-panel" extra={<span className={valid ? "mouse-count valid" : "mouse-count"}>{selected.length} / {rule}</span>}>
    <div className="selected-mice">{selected.length ? selected.map((id) => <button key={id} className="mouse-chip selected" onClick={() => onToggle(id)}>track ID {id} ×</button>) : <span>点击视频框或下方 track ID 选择参与对象</span>}</div>
    <div className="mouse-id-list">{tracks.map((track) => <button key={track.display_track_id} disabled={disabled} className={selected.includes(track.display_track_id) ? "mouse-id-item selected" : "mouse-id-item"} onClick={() => onToggle(track.display_track_id)}><b>track ID {track.display_track_id}</b><span>{track.visible_in_current_frame ? "当前可见" : `${track.first_frame ?? "?"}–${track.last_frame ?? "?"}`}</span></button>)}</div>
    {!valid && category ? <div className="mouse-rule-warning">“{category.name}”需要{rule}，当前选择不符合规则。</div> : null}
  </Card>;
}

function IdentityPanel({ tracks, selected, frame, search, showAll, busy, suppressions, canRevertSuppression, canRevertIdentity, onSearch, onShowAll, onToggle, onSplit, onMerge, onSuppressTrack, onRevertSuppression, onRevertIdentity }: {
  tracks: CorrectedTrackSummary[]; selected: number[]; frame: number; search: string; showAll: boolean; busy: boolean; suppressions: DetectionSuppression[]; canRevertSuppression: boolean; canRevertIdentity: boolean;
  onSearch: (s: string) => void; onShowAll: (v: boolean) => void; onToggle: (id: number) => void; onSplit: () => void; onMerge: () => void; onSuppressTrack: () => void; onRevertSuppression: (id?: number) => void; onRevertIdentity: () => void;
}) {
  return <Card title="track 修正" className="identity-panel" extra={<span className="identity-frame mono">帧 {frame}</span>}>
    <div className="identity-filter"><div className="segmented"><button className={!showAll ? "active" : ""} onClick={() => onShowAll(false)}>当前帧可见</button><button className={showAll ? "active" : ""} onClick={() => onShowAll(true)}>全部 track ID</button></div><input className="input" placeholder="搜索 track ID" aria-label="搜索 track ID" value={search} onChange={(e) => onSearch(e.target.value)} /></div>
    <div className="identity-track-list">{tracks.map((t) => <button key={t.display_track_id} className={selected.includes(t.display_track_id) ? "identity-track selected" : "identity-track"} onClick={() => onToggle(t.display_track_id)}><span className="track-id mono">track ID {t.display_track_id}</span><span>{t.visible_in_current_frame ? "● 当前可见" : `${t.first_frame ?? "?"} → ${t.last_frame ?? "?"}`}</span><span className="flex-spacer" /><span>{t.detection_count} 框</span></button>)}</div>
    <div className="identity-actions"><button className="btn btn-primary" disabled={busy || selected.length !== 1} onClick={onSplit}>从当前帧 Split</button><button className="btn btn-primary" disabled={busy || selected.length < 2} onClick={onMerge}>Merge 所选</button></div>
    <div className="identity-danger-zone"><button className="btn btn-sm btn-danger" disabled={busy || selected.length !== 1} onClick={onSuppressTrack}>忽略整个 track</button><button className="btn btn-sm" disabled={busy || !canRevertSuppression} onClick={() => onRevertSuppression()}>撤销上一次忽略</button><button className="btn btn-sm" disabled={busy || !canRevertIdentity} onClick={onRevertIdentity}>撤销上一次 Split / Merge</button></div>
    {suppressions.length ? <div className="identity-track-list">{suppressions.map((s) => <div key={s.id} className="identity-track"><span>忽略记录 #{s.id}</span><span>{s.frozen_detection_count} 个检测</span><span className="flex-spacer" /><button className="btn btn-sm" disabled={busy} onClick={() => onRevertSuppression(s.id)}>撤销</button></div>)}</div> : null}
  </Card>;
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
  const [workspaceMode, setWorkspaceMode] = useState<"behavior" | "identity">("behavior");
  const [selectedMouseIds, setSelectedMouseIds] = useState<number[]>([]);
  const [tracks, setTracks] = useState<CorrectedTrackSummary[]>([]);
  const [currentFrame, setCurrentFrame] = useState(0);
  const [detectionImport, setDetectionImport] = useState<DetectionImport | null>(null);
  const [identityRevision, setIdentityRevision] = useState(0);
  const [identitySearch, setIdentitySearch] = useState("");
  const [showAllTracks, setShowAllTracks] = useState(false);
  const [identityBusy, setIdentityBusy] = useState(false);
  const [overlayRefresh, setOverlayRefresh] = useState(0);
  const [lastSuppressionId, setLastSuppressionId] = useState<number | null>(null);
  const [activeSuppressions, setActiveSuppressions] = useState<DetectionSuppression[]>([]);
  const [lastIdentityEditId, setLastIdentityEditId] = useState<number | null>(null);

  const [submitting, setSubmitting] = useState(false);
  const [confirmDialog, confirm] = useConfirm();

  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [hint, setHint] = useState("播放视频，选择类别后按 S 设起点、D 设终点；Space 播放/暂停，←/→ 步进一帧");

  useEffect(() => {
    if (workspaceMode === "identity") {
      setStartPoint(null);
      setActiveCategory(null);
      setSelectedMouseIds([]);
    }
  }, [workspaceMode]);

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

  const toggleMouseId = useCallback((id: number) => {
    setSelectedMouseIds((prev) => prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id].sort((a, b) => a - b));
  }, []);

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
        const items = showAllTracks ? result.items : result.items.filter((t) => t.visible_in_current_frame || selectedMouseIds.includes(t.display_track_id));
        setTracks(items);
      })
      .catch((err: unknown) => { if (alive) setErrorMsg(err instanceof Error ? err.message : "加载 track ID 失败"); });
    return () => { alive = false; };
  }, [pid, vid, detectionImport, currentFrame, identitySearch, showAllTracks, identityRevision, selectedMouseIds]);

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

  const syncSuppressions = useCallback(async () => {
    try {
      const suppressions = await listDetectionSuppressions(pid, vid);
      setActiveSuppressions(suppressions);
      setLastSuppressionId(suppressions[0]?.id ?? null);
    } catch (err) {
      setErrorMsg(err instanceof Error ? `操作已完成，但忽略记录同步失败：${err.message}` : "操作已完成，但忽略记录同步失败");
    }
  }, [pid, vid]);

  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      const [projs, vids, cats, anns, suppressions] = await Promise.all([
        listProjects(),
        listVideos(pid),
        listCategories(pid),
        listAnnotations(pid, vid),
        listDetectionSuppressions(pid, vid),
      ]);
      const loadedVideo = vids.find((v) => v.id === vid) ?? null;
      setProject(projs.find((p) => p.id === pid) ?? null);
      setVideo(loadedVideo);
      setIdentityRevision(loadedVideo?.identity_revision ?? 0);
      setCategories(cats);
      setAnnotations(anns);
      setActiveSuppressions(suppressions);
      setLastSuppressionId(suppressions[0]?.id ?? null);
      setLastIdentityEditId(null);
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
    const invalid = annotations.filter((a) => a.mouse_id_status === "needs_mouse_ids" || !mouseIdsValid(categoryById.get(a.category_id) ?? null, a.mouse_ids));
    if (!detectionImport || invalid.length > 0) {
      setErrorMsg(!detectionImport ? "缺少有效 YOLO 检测数据，不能提交审核" : `有 ${invalid.length} 条行为标注需要补选参与对象，不能提交审核`);
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
  }, [video, annotations, pid, vid, confirm, mouseIdsValid, categoryById, detectionImport]);

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
    if (detectionImport && !mouseIdsValid(cat, selectedMouseIds)) {
      const max = cat.mouse_count_max;
      setErrorMsg(`“${cat.name}”需要${max === cat.mouse_count_min ? `恰好 ${cat.mouse_count_min} 个对象` : max == null ? `至少 ${cat.mouse_count_min} 个对象` : `${cat.mouse_count_min}–${max} 个对象`}，请先点击检测框或 track ID 列表选择`);
      return;
    }
    // 已提交 / 已通过 / 已退回的视频新增标注前需确认（会退回草稿、审核失效）
    if (!(await guardMutation("新增行为标注"))) {
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
        ...(detectionImport ? {
          mouse_ids: selectedMouseIds,
          detection_import_revision: detectionImport.revision,
          identity_revision: identityRevision,
        } : {}),
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
      setErrorMsg(err instanceof Error ? err.message : "保存行为标注失败");
    }
  }

  async function runIdentityEdit(operation: "split" | "merge") {
    if (!detectionImport) return;
    const request = {
      operation,
      track_ids: selectedMouseIds,
      frame: operation === "split" ? currentFrame : undefined,
      base_identity_revision: identityRevision,
      base_detection_import_revision: detectionImport.revision,
    };
    setIdentityBusy(true); setErrorMsg(null);
    try {
      const check = await checkIdentityEdit(pid, vid, request);
      if (check.conflict_frames?.length) { setErrorMsg(`Merge 冲突：帧 ${check.conflict_frames.slice(0, 12).join("、")} 同一 track ID 将对应多个框，请先忽略误检框`); return; }
      const ok = await confirm({
        title: operation === "split" ? `Split track ID ${selectedMouseIds[0]}？` : `Merge ${selectedMouseIds.length} 个 track？`,
        message: <>{operation === "split" ? <>从帧 <b>{currentFrame}</b> 起生成新 track ID，前后分别有 {check.detections_before} / {check.detections_after} 个检测。</> : <>保留 track ID <b>{check.retained_display_track_id}</b>，影响 {check.affected_detection_count} 个检测。</>}<br />受影响行为标注：<b>{check.affected_annotation_count}</b> 条。</>,
        confirmLabel: operation === "split" ? "确认 Split" : "确认 Merge",
      });
      if (!ok) return;
      const result = await commitIdentityEdit(pid, vid, request);
      setIdentityRevision(result.identity_revision);
      setVideo((v) => v ? { ...v, identity_revision: result.identity_revision } : v);
      setSelectedMouseIds(result.new_display_track_id != null ? [result.new_display_track_id] : result.retained_display_track_id != null ? [result.retained_display_track_id] : []);
      if (result.edit_id) setLastIdentityEditId(result.edit_id);
      setOverlayRefresh((x) => x + 1);
      await loadAnnotations();
      setHint("track 修正已提交；受影响行为标注和审核状态已刷新");
    } catch (err) { setErrorMsg(err instanceof Error ? err.message : "track 修正失败"); }
    finally { setIdentityBusy(false); }
  }

  async function suppressTrack() {
    if (!detectionImport || selectedMouseIds.length !== 1) return;
    const selectedId = selectedMouseIds[0];
    const ok = await confirm({ title: `忽略整个 track（track ID ${selectedId}）？`, message: <>此操作会将所选内容标记为误检并忽略；不会删除原始检测数据。忽略的数据不再参与显示、校验和导出，且可撤销。</>, confirmLabel: "标记为误检并忽略", danger: true });
    if (!ok) return;
    setIdentityBusy(true);
    try {
      const result = await createSuppression(pid, vid, { scope: "corrected_track", track_id: selectedId, base_identity_revision: identityRevision, base_detection_import_revision: detectionImport.revision });
      setIdentityRevision(result.identity_revision); setVideo((v) => v ? { ...v, identity_revision: result.identity_revision } : v); setLastSuppressionId(result.suppression_id ?? null); setSelectedMouseIds([]); setOverlayRefresh((x) => x + 1); await loadAnnotations();
      await syncSuppressions();
    } catch (err) { setErrorMsg(err instanceof Error ? err.message : "忽略检测失败"); }
    finally { setIdentityBusy(false); }
  }

  async function revertLastSuppression(suppressionId = lastSuppressionId ?? undefined) {
    if (!detectionImport || suppressionId == null) return;
    setIdentityBusy(true);
    try {
      const result = await revertSuppression(pid, vid, suppressionId, { base_identity_revision: identityRevision, base_detection_import_revision: detectionImport.revision });
      setIdentityRevision(result.identity_revision); setVideo((v) => v ? { ...v, identity_revision: result.identity_revision } : v); setOverlayRefresh((x) => x + 1); await loadAnnotations();
      setActiveSuppressions((current) => current.filter((s) => s.id !== suppressionId));
      if (lastSuppressionId === suppressionId) setLastSuppressionId(null);
      await syncSuppressions();
    } catch (err) { setErrorMsg(err instanceof Error ? err.message : "撤销失败"); }
    finally { setIdentityBusy(false); }
  }

  async function revertLastIdentity() {
    if (!detectionImport || lastIdentityEditId == null) return;
    setIdentityBusy(true);
    try {
      const result = await revertIdentityEdit(pid, vid, lastIdentityEditId, { base_identity_revision: identityRevision, base_detection_import_revision: detectionImport.revision });
      setIdentityRevision(result.identity_revision); setVideo((v) => v ? { ...v, identity_revision: result.identity_revision } : v); setLastIdentityEditId(null); setOverlayRefresh((x) => x + 1); await loadAnnotations();
    } catch (err) { setErrorMsg(err instanceof Error ? err.message : "撤销 Split / Merge 失败"); }
    finally { setIdentityBusy(false); }
  }

  /* ---------- 列表编辑 / 删除 / 导出 ---------- */
  async function handleEditSave(id: number, patch: AnnotationPatchInput) {
    if (!(await guardMutation("编辑行为标注"))) return;
    try {
      await updateAnnotation(pid, vid, id, patch);
      setSaveState("saved");
      const wasLocked = (video?.workflow_status ?? "draft") !== "draft";
      setHint(wasLocked ? "行为标注已更新，视频已退回草稿，请重新提交审核" : "行为标注已更新");
      await loadAnnotations();
      if (wasLocked) await refreshVideo();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "保存行为标注失败");
      throw err;
    }
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
    try {
      await deleteAnnotation(pid, vid, ann.id);
      setSaveState("saved");
      setHint(locked ? "行为标注已删除，视频已退回草稿，请重新提交审核" : "行为标注已删除");
      await loadAnnotations();
      if (locked) await refreshVideo();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "删除行为标注失败");
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
        if (workspaceMode === "identity") break;
        e.preventDefault();
        markStart();
        break;
      case "KeyD":
        if (workspaceMode === "identity") break;
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
          <SubmitReviewControl
            status={video?.workflow_status ?? "draft"}
            hasAnnotations={annotations.length > 0}
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
      {annotations.some((a) => a.mouse_id_status === "needs_mouse_ids") ? <div className="mouse-warning-banner" role="status">⚠ 有 {annotations.filter((a) => a.mouse_id_status === "needs_mouse_ids").length} 条行为标注需要补选参与对象，补齐前不能提交审核。</div> : null}
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
                    title="点击播放 / 暂停 (Space)"
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
                  <DetectionOverlay
                    projectId={pid}
                    videoId={vid}
                    video={videoRef.current}
                    currentTime={currentTime}
                    fallbackFps={video?.fps}
                    selectedIds={selectedMouseIds}
                    interactive
                    onToggleTrack={toggleMouseId}
                    onFrameData={handleFrameData}
                    refreshKey={overlayRefresh}
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
                    {playing ? "⏸ 暂停 (Space)" : "▶ 播放 (Space)"}
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={(e) => {
                      e.currentTarget.blur();
                      step(-1);
                    }}
                  >
                    ⟨ 退一帧 (←)
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={(e) => {
                      e.currentTarget.blur();
                      step(1);
                    }}
                  >
                    进一帧 (→) ⟩
                  </button>
                  <span className="time-display">
                    <b>{formatTime(currentTime)}</b> / {timelineDuration ? formatTime(timelineDuration) : "?"}
                  </span>
                  <span className="flex-spacer" />
                  <div className="workspace-tabs" role="tablist"><button className={workspaceMode === "behavior" ? "active" : ""} onClick={() => setWorkspaceMode("behavior")}>行为标注</button><button className={workspaceMode === "identity" ? "active" : ""} disabled={!detectionImport} onClick={() => setWorkspaceMode("identity")}>track 修正</button></div>
                  <button
                    type="button"
                    className={startPoint ? "btn btn-sm btn-point armed" : "btn btn-sm btn-point"}
                    disabled={workspaceMode === "identity"}
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
                    disabled={workspaceMode === "identity"}
                    onClick={(e) => {
                      e.currentTarget.blur();
                      void markEnd();
                    }}
                    title="在当前时间设置终点并保存行为标注"
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
          {workspaceMode === "behavior" ? <>
            <CategoryPanel categories={categories} activeCategory={activeCategory} onSelect={setActiveCategory} disabled={!videoReady} />
            <MouseIdsPanel tracks={tracks} selected={selectedMouseIds} category={activeCategory} disabled={!detectionImport} onToggle={toggleMouseId} />
          </> : <IdentityPanel tracks={tracks} selected={selectedMouseIds} frame={currentFrame} search={identitySearch} showAll={showAllTracks} busy={identityBusy} suppressions={activeSuppressions} canRevertSuppression={lastSuppressionId != null} canRevertIdentity={lastIdentityEditId != null} onSearch={setIdentitySearch} onShowAll={setShowAllTracks} onToggle={toggleMouseId} onSplit={() => void runIdentityEdit("split")} onMerge={() => void runIdentityEdit("merge")} onSuppressTrack={() => void suppressTrack()} onRevertSuppression={(id) => void revertLastSuppression(id)} onRevertIdentity={() => void revertLastIdentity()} />}
          <AnnotationList
            annotations={annotations}
            categories={categories}
            categoryById={categoryById}
            fps={video?.fps}
            currentTime={currentTime}
            readOnly={workspaceMode === "identity"}
            onEditSave={handleEditSave}
            onDelete={handleDelete}
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
