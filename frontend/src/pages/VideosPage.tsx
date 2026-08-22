import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type MouseEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { batchAssignVideos, claimVideo, claimVideos, createVideo, getAssignmentStats, listAssignees, listProjects, listVideos, releaseVideo } from "../api";
import type { AssigneeDirectoryItem, AssignmentStats, Project, Video, VideoView } from "../api/types";
import { ROLE_LABELS } from "../api/types";
import { useConfirm } from "../components/ConfirmDialog";
import { Card, EmptyState, ErrorBox, Loading, StatusBadge, WorkflowBadge, statusLabel } from "../components/ui";
import VideoUploadPanel from "../components/VideoUploadPanel";
import { MediaStatusSummary } from "../components/MediaStatusPanel";
import { ApiError } from "../api/client";
import { useVideoMarqueeSelection } from "../hooks/useVideoMarqueeSelection";
import { formatDate, formatDuration } from "../utils/format";

const VIEWS: { key: VideoView; label: string; hint: string }[] = [
  { key: "mine", label: "我的任务", hint: "由你负责的视频" },
  { key: "unassigned", label: "待领取", hint: "尚无负责人的视频" },
  { key: "all", label: "全部", hint: "项目内全部视频" },
];

interface VideoFormState {
  filename: string;
  duration: string;
  fps: string;
  width: string;
  height: string;
  status: string;
  storage_path: string;
}

const EMPTY_FORM: VideoFormState = {
  filename: "",
  duration: "",
  fps: "",
  width: "",
  height: "",
  status: "",
  storage_path: "",
};

function actionError(err: unknown): string {
  const text = err instanceof Error ? err.message : "操作失败";
  if (text.includes("already been claimed")) return "该视频已被其他成员领取，或已不再是草稿，列表已刷新。";
  if (text.includes("Only the current assignee")) return "只有当前负责人可以释放草稿视频，列表已刷新。";
  if (text.includes("Assignment conflict") || text.includes("every video must still belong")) return "批量改派发生冲突：部分视频已被删除、移出项目或进入待审核/已通过状态。列表已刷新，请重新选择。";
  return text;
}

export default function VideosPage() {
  const pid = Number(useParams<{ projectId: string }>().projectId);
  const navigate = useNavigate();
  const [project, setProject] = useState<Project | null>(null);
  const [assignees, setAssignees] = useState<AssigneeDirectoryItem[]>([]);
  const [stats, setStats] = useState<AssignmentStats | null>(null);
  const [videos, setVideos] = useState<Video[] | null>(null);
  const [view, setView] = useState<VideoView>("mine");
  const [workflow, setWorkflow] = useState("");
  const [assigneeFilter, setAssigneeFilter] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [batchAssignee, setBatchAssignee] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionId, setActionId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selectionAnnouncement, setSelectionAnnouncement] = useState("");
  const [uploadOpen, setUploadOpen] = useState(false);
  // 开发用 Mock 元数据表单（折叠区，不参与真实上传）
  const [devOpen, setDevOpen] = useState(false);
  const [form, setForm] = useState<VideoFormState>(EMPTY_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [confirmDialog, confirm] = useConfirm();
  const selectAllRef = useRef<HTMLInputElement>(null);
  const canManage = project?.role === "owner" || project?.role === "admin";
  const canSelect = Boolean(project && (canManage || view === "unassigned"));
  const memberClaimMode = Boolean(project?.role === "member" && view === "unassigned");

  const load = useCallback(async () => {
    if (!pid) return [] as Video[];
    try {
      const params = { view, workflow_status: view === "unassigned" ? undefined : workflow || undefined, assignee_membership_id: assigneeFilter ? Number(assigneeFilter) : undefined };
      const [projects, assigneeRows, videoRows, assignmentStats] = await Promise.all([listProjects(), listAssignees(pid), listVideos(pid, params), getAssignmentStats(pid)]);
      setProject(projects.find((p) => p.id === pid) ?? null); setAssignees(assigneeRows); setVideos(videoRows); setStats(assignmentStats); setError(null);
      setSelected((old) => new Set([...old].filter((id) => videoRows.some((v) => v.id === id && ["draft", "rejected"].includes(v.workflow_status)))));
      return videoRows;
    } catch (err) { setError(err instanceof Error ? err.message : "加载视频失败"); setVideos([]); return [] as Video[]; }
  }, [pid, view, workflow, assigneeFilter]);
  useEffect(() => { void load(); }, [load]);

  const displayed = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (videos ?? []).filter((v) => !q || v.filename.toLowerCase().includes(q));
  }, [videos, query]);
  const selectable = useMemo(() => canSelect
    ? displayed.filter((video) => view === "unassigned" ? video.workflow_status === "draft" : ["draft", "rejected"].includes(video.workflow_status))
    : [], [canSelect, displayed, view]);
  const visibleSelectedCount = selectable.reduce((count, video) => count + (selected.has(video.id) ? 1 : 0), 0);
  const announceSelection = useCallback((message: string) => setSelectionAnnouncement(message), []);
  const marquee = useVideoMarqueeSelection({ enabled: canSelect, selected, setSelected, onAnnounce: announceSelection });

  useEffect(() => {
    const visibleIds = new Set(selectable.map((video) => video.id));
    setSelected((old) => {
      const next = new Set([...old].filter((id) => visibleIds.has(id)));
      if (next.size === old.size) return old;
      setSelectionAnnouncement(next.size ? `筛选后保留 ${next.size} 个已选视频` : "筛选后已清除隐藏选择");
      return next;
    });
  }, [selectable]);

  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = visibleSelectedCount > 0 && visibleSelectedCount < selectable.length;
  }, [selectable.length, visibleSelectedCount]);

  async function perform(id: number, kind: "claim" | "release") {
    setActionId(id); setError(null); setNotice(null);
    try {
      if (kind === "claim") {
        await claimVideo(pid, id);
        setSelected((old) => { const next = new Set(old); next.delete(id); return next; });
        setNotice("已领取 1 个视频，已加入「我的任务」。");
        setSelectionAnnouncement("已领取 1 个视频，已加入我的任务");
      } else {
        await releaseVideo(pid, id);
        setNotice("已释放视频，其他成员现在可以领取。");
      }
      await load();
    }
    catch (err) {
      const message = kind === "claim" && err instanceof ApiError && err.status === 409
        ? "该视频已被其他成员领取，或已不再是草稿，列表已刷新。"
        : actionError(err);
      await load();
      setError(message);
    } finally { setActionId(null); }
  }

  async function applyBatch() {
    const ids = [...selected]; if (!ids.length) return;
    const target = batchAssignee ? assignees.find((m) => m.membership_id === Number(batchAssignee))?.username : "未分配";
    const assigning = view === "unassigned";
    if (!await confirm({ title: assigning ? "确认批量分配" : "确认批量改派", message: `将 ${ids.length} 个视频的负责人改为「${target}」。只有草稿或已退回视频可以操作。`, confirmLabel: batchAssignee ? (assigning ? "确认分配" : "确认改派") : "清空负责人", danger: !batchAssignee })) return;
    setBusy(true); setError(null); setNotice(null);
    try { await batchAssignVideos(pid, ids, batchAssignee ? Number(batchAssignee) : null); setSelected(new Set()); setNotice(`已更新 ${ids.length} 个视频的负责人。`); await load(); }
    catch (err) { const message = actionError(err); await load(); setError(message); } finally { setBusy(false); }
  }

  async function applyBatchClaim() {
    const ids = [...selected]; if (!ids.length) return;
    if (ids.length > 200) { setError("一次最多领取 200 个视频，请减少选择后重试。"); return; }
    if (!await confirm({ title: "确认批量领取", message: `领取所选 ${ids.length} 个视频，并将你设为负责人。`, confirmLabel: "确认领取" })) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      const result = await claimVideos(pid, { video_ids: ids });
      setSelected(new Set());
      setNotice(`已领取 ${result.claimed_count} 个视频，已加入「我的任务」。`);
      setSelectionAnnouncement(`已领取 ${result.claimed_count} 个视频，已加入我的任务`);
      await load();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        const rows = await load();
        const normalizedQuery = query.trim().toLowerCase();
        const remaining = ids.filter((id) => rows.some((video) => video.id === id && video.workflow_status === "draft" && (!normalizedQuery || video.filename.toLowerCase().includes(normalizedQuery))));
        setSelected(new Set(remaining));
        setError("部分视频已被领取或不再是草稿，本次所选视频均未领取；列表已刷新。");
        setSelectionAnnouncement(`批量领取发生冲突，本次所选视频均未领取；刷新后仍选择 ${remaining.length} 个可领取视频`);
      } else {
        setError(err instanceof Error ? err.message : "批量领取失败");
      }
    } finally { setBusy(false); }
  }

  function updateField(key: keyof VideoFormState, value: string) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function parseNum(value: string): number | null {
    if (value.trim() === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  async function handleCreate(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    const filename = form.filename.trim();
    if (!filename) {
      setFormError("文件名（filename）不能为空");
      return;
    }
    const duration = parseNum(form.duration);
    const fps = parseNum(form.fps);
    const width = parseNum(form.width);
    const height = parseNum(form.height);
    if (duration !== null && duration < 0) {
      setFormError("时长 duration 必须 ≥ 0");
      return;
    }
    if (width !== null && height !== null && (width < 0 || height < 0)) {
      setFormError("分辨率必须 ≥ 0");
      return;
    }

    setCreating(true);
    try {
      await createVideo(pid, {
        filename,
        duration,
        fps,
        width,
        height,
        status: form.status.trim() || "metadata",
        storage_path: form.storage_path.trim() || null,
      });
      await load();
      setDevOpen(false);
      setForm(EMPTY_FORM);
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "创建视频失败");
    } finally {
      setCreating(false);
    }
  }

  function toggleAll() {
    const next = visibleSelectedCount === selectable.length && selectable.length ? new Set<number>() : new Set(selectable.map((v) => v.id));
    setSelected(next);
    setSelectionAnnouncement(`已选择 ${next.size} 个视频`);
  }

  function clearSelection(message = "已清除选择") {
    marquee.cancelGesture();
    setSelected(new Set());
    setSelectionAnnouncement(message);
  }

  function toggleOne(videoId: number) {
    setSelected((old) => {
      const next = new Set(old);
      next.has(videoId) ? next.delete(videoId) : next.add(videoId);
      setSelectionAnnouncement(`已选择 ${next.size} 个视频`);
      return next;
    });
  }

  function selectCard(videoId: number, event: MouseEvent<HTMLElement>) {
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (target.closest("button, a, input, select, textarea, label, [data-selection-interactive], [data-selection-copy]")) return;
    marquee.selectCard(videoId, event);
  }

  const selectAllLabel = memberClaimMode ? "全选待领取视频" : view === "unassigned" ? "全选可分配视频" : "全选可改派视频";
  const selectAllAriaLabel = memberClaimMode
    ? "全选当前搜索结果中的待领取视频"
    : view === "unassigned"
      ? "全选当前搜索结果中的可分配草稿视频"
      : "全选当前筛选和搜索结果中的可改派草稿或已退回视频";
  const cardSelectionVerb = memberClaimMode ? "选择领取" : view === "unassigned" ? "选择分配" : "选择改派";

  return <div className="container videos-page">
    {confirmDialog}
    <div className="page-header"><div><div className="breadcrumb"><Link to="/projects">项目</Link> / {project?.name ?? `#${pid}`}{project ? ` · ${ROLE_LABELS[project.role]}` : ""}</div><h1>视频库</h1></div><div className="page-header-actions">{project?.can_review ? <Link className="btn btn-sm" to={`/projects/${pid}/review`}>审核工作台</Link> : null}<button className="btn btn-primary" onClick={() => setUploadOpen((x) => !x)}>{uploadOpen ? "收起上传" : "↑ 上传视频"}</button></div></div>
    {uploadOpen && project ? <VideoUploadPanel projectId={pid} canManage={canManage} assignees={assignees} onUploaded={() => void load()} onEnterAnnotation={(v) => navigate(`/projects/${pid}/annotate/${v.id}`)} onClose={() => setUploadOpen(false)} /> : null}
    {error ? <ErrorBox message={error} /> : null}{notice ? <div className="ok-box" role="status">✓ {notice}</div> : null}
    <div className="assignment-summary"><span><b>{stats?.total ?? 0}</b> 个视频</span><span className={(stats?.unassigned ?? 0) ? "warn" : ""}><b>{stats?.unassigned ?? 0}</b> 个未分配</span>{canManage ? <Link to={`/projects/${pid}/manage`}>查看分配统计 →</Link> : null}</div>
    <div className="view-tabs" role="tablist" aria-label="视频视图">{VIEWS.map((item) => <button key={item.key} role="tab" aria-selected={view === item.key} className={view === item.key ? "active" : ""} title={item.hint} onClick={() => { setView(item.key); if (item.key === "unassigned") setWorkflow(""); clearSelection("切换视图后已清除选择"); if (item.key !== "all") setAssigneeFilter(""); }}>{item.label}{item.key === "unassigned" && (stats?.claimable ?? 0) > 0 ? <span title="可领取数">{stats?.claimable}</span> : null}</button>)}</div>
    <div className="video-toolbar"><input className="input search" type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="按文件名搜索…" aria-label="按文件名搜索"/>{view === "unassigned" ? <span className="fixed-filter" aria-label="工作流状态固定为草稿">工作流：草稿</span> : <select className="select workflow-filter" value={workflow} onChange={(e) => { setWorkflow(e.target.value); clearSelection("筛选变化后已清除选择"); }} aria-label="工作流状态"><option value="">全部工作流状态</option><option value="draft">草稿</option><option value="submitted">待审核</option><option value="approved">审核通过</option><option value="rejected">已退回</option></select>}{view === "all" ? <select className="select assignee-filter" value={assigneeFilter} onChange={(e) => { clearSelection("筛选变化后已清除选择"); if (e.target.value === "unassigned") { setView("unassigned"); setWorkflow(""); setAssigneeFilter(""); } else setAssigneeFilter(e.target.value); }} aria-label="负责人"><option value="">全部负责人</option><option value="unassigned">未分配</option>{assignees.map((m) => <option key={m.membership_id} value={m.membership_id}>{m.username}</option>)}</select> : null}<span className="flex-spacer"/><button className="btn btn-sm toolbar-refresh" onClick={() => void load()}>刷新</button></div>
    <div className="visually-hidden" aria-live="polite" aria-atomic="true">{selectionAnnouncement}</div>
    {canSelect && selected.size > 0 ? memberClaimMode
      ? <div className="batch-bar batch-bar-claim" role="region" aria-label="批量领取" aria-busy={busy}><b className="batch-count">已选 {selected.size} 个</b><div className="batch-actions"><button className="btn btn-primary btn-sm" disabled={busy || actionId !== null} onClick={() => void applyBatchClaim()}>{busy ? "领取中…" : "领取所选"}</button><button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => clearSelection()}>取消选择</button></div></div>
      : <div className="batch-bar" role="region" aria-label="批量分配" aria-busy={busy}><b className="batch-count">已选 {selected.size} 个</b><span className="batch-label">{view === "unassigned" ? "分配给" : "改派给"}</span><select className="select" value={batchAssignee} onChange={(e) => setBatchAssignee(e.target.value)} disabled={busy} aria-label={view === "unassigned" ? "批量分配负责人" : "批量改派负责人"}><option value="">未分配（清空）</option>{assignees.map((m) => <option key={m.membership_id} value={m.membership_id}>{m.username}</option>)}</select><div className="batch-actions"><button className="btn btn-primary btn-sm" disabled={busy || actionId !== null} onClick={() => void applyBatch()}>{busy ? "处理中…" : view === "unassigned" ? "分配所选" : "应用改派"}</button><button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => clearSelection()}>取消选择</button></div></div>
      : null}
    {videos === null ? <Loading text="加载视频…" /> : displayed.length === 0 ? <Card><EmptyState title={view === "mine" ? "暂无我的任务" : view === "unassigned" ? "暂无待领取视频" : "没有匹配的视频"} hint={view === "mine" ? "可到「待领取」领取未分配视频；你仍可在「全部」进入其他视频。" : view === "unassigned" ? "当前没有未分配的草稿视频。" : "调整搜索或筛选条件。"}/></Card> : <>
      {selectable.length > 0 ? <div className="selection-tools"><label className="select-all"><input ref={selectAllRef} type="checkbox" checked={visibleSelectedCount === selectable.length} onChange={toggleAll} aria-label={selectAllAriaLabel}/> <span>{selectAllLabel}</span></label>{marquee.interactionEnabled ? <span className="selection-hint">拖动框选 · Shift 追加 · Ctrl/Cmd 切换</span> : null}</div> : null}
      <div ref={marquee.gridRef} className={`video-grid ${marquee.interactionEnabled ? "selection-enabled" : ""}`} tabIndex={marquee.interactionEnabled ? 0 : -1} aria-label={canSelect ? memberClaimMode ? "可批量领取的视频列表" : view === "unassigned" ? "可批量分配的视频列表" : "可批量改派的视频列表" : "视频列表"} {...marquee.gridPointerHandlers}>{displayed.map((v) => { const assignable = canSelect && (view === "unassigned" ? v.workflow_status === "draft" : ["draft", "rejected"].includes(v.workflow_status)); const mine = v.assignee_membership_id === project?.membership_id; return <article key={v.id} data-video-id={v.id} data-video-selectable={assignable ? "true" : "false"} onClick={assignable ? (event) => selectCard(v.id, event) : undefined} className={`card video-card ${selected.has(v.id) ? "selected" : ""}`}>
        {canSelect ? <label className="card-select" title={assignable ? `${cardSelectionVerb}视频` : "当前状态不可选择"}><input type="checkbox" checked={selected.has(v.id)} disabled={!assignable} onChange={() => toggleOne(v.id)} aria-label={`${cardSelectionVerb}：${v.filename}`}/></label> : null}
        <div className="thumb" aria-hidden="true">{v.status === "needs_transcode" ? "⟳" : v.storage_path ? "▶" : "▢"}</div><div className="name" data-selection-copy title={v.filename}>{v.filename}</div>
        <div className={`assignee-line ${v.assignee ? "" : "unassigned"}`} data-selection-copy><span className="assignee-avatar">{v.assignee?.username.slice(0, 1).toUpperCase() ?? "?"}</span><span>{v.assignee ? <><small>负责人</small><b title={v.assignee.username}>{v.assignee.username}{mine ? "（我）" : ""}</b></> : <><small>负责人</small><b>未分配</b></>}</span></div>
        <div className="meta" data-selection-copy><span>时长 <b>{formatDuration(v.duration)}</b></span><span>帧率 <b>{v.fps != null ? `${v.fps} fps` : "—"}</b></span><span>分辨率 <b>{v.width != null && v.height != null ? `${v.width} × ${v.height}` : "—"}</b></span><span>状态 <b>{statusLabel(v.status)}</b></span></div>
        {v.status === "needs_transcode" ? <div className="transcode-note" role="note" data-selection-copy>需先转码：当前源格式不能在浏览器中直接播放，因此暂不可进入标注。</div> : null}
        <div className="workflow-line" data-selection-copy><WorkflowBadge value={v.workflow_status} revision={v.annotation_revision}/></div>
        {v.submitted_at || v.approved_at ? <div className="workflow-times" data-selection-copy>{v.submitted_at ? <span>提交 <b>{formatDate(v.submitted_at)}</b></span> : null}{v.approved_at ? <span>通过 <b>{formatDate(v.approved_at)}</b></span> : null}</div> : null}
        {v.workflow_status === "approved" ? <div className="card-media-summary" data-selection-copy><MediaStatusSummary projectId={pid} videoId={v.id}/></div> : null}
        <div className="foot" data-selection-copy><span>上传 {formatDate(v.created_at)}</span><StatusBadge value={v.status}/></div>
        {view === "unassigned" ? <div className="actions card-actions"><button className="btn btn-sm btn-primary" disabled={actionId !== null || busy} onClick={() => void perform(v.id, "claim")}>{actionId === v.id ? "领取中…" : "领取任务"}</button></div> : view === "mine" ? <div className="actions card-actions">{mine && v.workflow_status === "draft" ? <button className="btn btn-sm btn-ghost" disabled={actionId !== null || busy} onClick={() => void perform(v.id, "release")}>释放</button> : null}<button className="btn btn-sm btn-primary" disabled={v.status === "needs_transcode"} onClick={() => navigate(`/projects/${pid}/annotate/${v.id}`)}>进入标注 →</button></div> : null}
      </article>; })}{marquee.rect ? <div className="marquee-rect" aria-hidden="true" style={marquee.rect}/> : null}</div></>}
    {/* 开发工具与普通分工流程分层，并默认折叠。 */}
    <details className="dev-panel" open={devOpen} onToggle={(event) => setDevOpen(event.currentTarget.open)}>
      <summary>开发用：Mock 元数据录入</summary>
      <form className="card create-form" onSubmit={handleCreate}>
        <div className="card-body">
          <div className="field">
            <label htmlFor="video-filename">文件名 filename *</label>
            <input id="video-filename" className="input" value={form.filename} placeholder="例如 experiment_01.mp4" onChange={(event) => updateField("filename", event.target.value)} />
          </div>
          <div className="field-row">
            <div className="field">
              <label htmlFor="video-duration">时长 duration（秒）</label>
              <input id="video-duration" className="input" type="number" min="0" step="0.01" value={form.duration} placeholder="如 120.5" onChange={(event) => updateField("duration", event.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="video-fps">帧率 fps</label>
              <input id="video-fps" className="input" type="number" min="0" step="0.01" value={form.fps} placeholder="如 30" onChange={(event) => updateField("fps", event.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="video-width">宽度 width</label>
              <input id="video-width" className="input" type="number" min="0" value={form.width} placeholder="如 1920" onChange={(event) => updateField("width", event.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="video-height">高度 height</label>
              <input id="video-height" className="input" type="number" min="0" value={form.height} placeholder="如 1080" onChange={(event) => updateField("height", event.target.value)} />
            </div>
          </div>
          <div className="field-row">
            <div className="field">
              <label htmlFor="video-status">状态 status（可选）</label>
              <input id="video-status" className="input" value={form.status} placeholder="默认 metadata；如 ready / needs_transcode / error" onChange={(event) => updateField("status", event.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="video-path">存储路径 storage_path（可选）</label>
              <input id="video-path" className="input" value={form.storage_path} placeholder="data/videos/ 内相对路径或绝对路径" onChange={(event) => updateField("storage_path", event.target.value)} />
            </div>
          </div>
          <div className="form-error">{formError ?? ""}</div>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button type="button" className="btn" onClick={() => setDevOpen(false)}>收起</button>
            <button type="submit" className="btn" disabled={creating}>{creating ? "创建中…" : "创建视频"}</button>
          </div>
        </div>
      </form>
    </details>
  </div>;
}
