import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { batchAssignVideos, claimVideo, createVideo, getAssignmentStats, listAssignees, listProjects, listVideos, releaseVideo } from "../api";
import type { AssigneeDirectoryItem, AssignmentStats, Project, Video, VideoView } from "../api/types";
import { ROLE_LABELS } from "../api/types";
import { useConfirm } from "../components/ConfirmDialog";
import { Card, EmptyState, ErrorBox, Loading, StatusBadge, WorkflowBadge, statusLabel } from "../components/ui";
import VideoUploadPanel from "../components/VideoUploadPanel";
import { MediaStatusSummary } from "../components/MediaStatusPanel";
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
  if (text.includes("already been claimed")) return "该视频刚刚已被其他成员领取，列表已刷新。";
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
  const [uploadOpen, setUploadOpen] = useState(false);
  // 开发用 Mock 元数据表单（折叠区，不参与真实上传）
  const [devOpen, setDevOpen] = useState(false);
  const [form, setForm] = useState<VideoFormState>(EMPTY_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [confirmDialog, confirm] = useConfirm();
  const canManage = project?.role === "owner" || project?.role === "admin";

  const load = useCallback(async () => {
    if (!pid) return;
    try {
      const params = { view, workflow_status: workflow || undefined, assignee_membership_id: assigneeFilter ? Number(assigneeFilter) : undefined };
      const [projects, assigneeRows, videoRows, assignmentStats] = await Promise.all([listProjects(), listAssignees(pid), listVideos(pid, params), getAssignmentStats(pid)]);
      setProject(projects.find((p) => p.id === pid) ?? null); setAssignees(assigneeRows); setVideos(videoRows); setStats(assignmentStats); setError(null);
      setSelected((old) => new Set([...old].filter((id) => videoRows.some((v) => v.id === id && ["draft", "rejected"].includes(v.workflow_status)))));
    } catch (err) { setError(err instanceof Error ? err.message : "加载视频失败"); setVideos([]); }
  }, [pid, view, workflow, assigneeFilter]);
  useEffect(() => { void load(); }, [load]);

  const displayed = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (videos ?? []).filter((v) => !q || v.filename.toLowerCase().includes(q));
  }, [videos, query]);
  const selectable = displayed.filter((v) => ["draft", "rejected"].includes(v.workflow_status));

  async function perform(id: number, kind: "claim" | "release") {
    setActionId(id); setError(null); setNotice(null);
    try { if (kind === "claim") await claimVideo(pid, id); else await releaseVideo(pid, id); setNotice(kind === "claim" ? "已领取视频，已加入「我的任务」。" : "已释放视频，其他成员现在可以领取。"); await load(); }
    catch (err) { setError(actionError(err)); await load(); } finally { setActionId(null); }
  }

  async function applyBatch() {
    const ids = [...selected]; if (!ids.length) return;
    const target = batchAssignee ? assignees.find((m) => m.membership_id === Number(batchAssignee))?.username : "未分配";
    if (!await confirm({ title: "确认批量改派", message: `将 ${ids.length} 个视频的负责人改为「${target}」。只有草稿或已退回视频可以改派。`, confirmLabel: batchAssignee ? "确认改派" : "清空负责人", danger: !batchAssignee })) return;
    setBusy(true); setError(null); setNotice(null);
    try { await batchAssignVideos(pid, ids, batchAssignee ? Number(batchAssignee) : null); setSelected(new Set()); setNotice(`已更新 ${ids.length} 个视频的负责人。`); await load(); }
    catch (err) { setError(actionError(err)); await load(); } finally { setBusy(false); }
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

  function toggleAll() { setSelected(selected.size === selectable.length && selectable.length ? new Set() : new Set(selectable.map((v) => v.id))); }

  return <div className="container videos-page">
    {confirmDialog}
    <div className="page-header"><div><div className="breadcrumb"><Link to="/projects">项目</Link> / {project?.name ?? `#${pid}`}{project ? ` · ${ROLE_LABELS[project.role]}` : ""}</div><h1>视频库</h1><div className="sub">分工表示责任归属，不限制项目成员查看或提交视频。</div></div><div className="page-header-actions">{project?.can_review ? <Link className="btn btn-sm" to={`/projects/${pid}/review`}>审核工作台</Link> : null}<button className="btn btn-primary" onClick={() => setUploadOpen((x) => !x)}>{uploadOpen ? "收起上传" : "↑ 上传视频"}</button></div></div>
    {uploadOpen && project ? <VideoUploadPanel projectId={pid} canManage={canManage} assignees={assignees} onUploaded={() => void load()} onEnterAnnotation={(v) => navigate(`/projects/${pid}/annotate/${v.id}`)} onClose={() => setUploadOpen(false)} /> : null}
    {error ? <ErrorBox message={error} /> : null}{notice ? <div className="ok-box" role="status">✓ {notice}</div> : null}
    <div className="assignment-summary"><span><b>{stats?.total ?? 0}</b> 个视频</span><span className={(stats?.unassigned ?? 0) ? "warn" : ""}><b>{stats?.unassigned ?? 0}</b> 个未分配</span>{canManage ? <Link to={`/projects/${pid}/manage`}>查看分配统计 →</Link> : null}</div>
    <div className="view-tabs" role="tablist" aria-label="视频视图">{VIEWS.map((item) => <button key={item.key} role="tab" aria-selected={view === item.key} className={view === item.key ? "active" : ""} title={item.hint} onClick={() => { setView(item.key); setSelected(new Set()); if (item.key !== "all") setAssigneeFilter(""); }}>{item.label}{item.key === "unassigned" && stats?.unassigned ? <span>{stats.unassigned}</span> : null}</button>)}</div>
    <div className="video-toolbar"><input className="input search" type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="按文件名搜索…" aria-label="按文件名搜索"/><select className="select" value={workflow} onChange={(e) => setWorkflow(e.target.value)} aria-label="工作流状态"><option value="">全部工作流状态</option><option value="draft">草稿</option><option value="submitted">待审核</option><option value="approved">审核通过</option><option value="rejected">已退回</option></select>{view === "all" ? <select className="select" value={assigneeFilter} onChange={(e) => { if (e.target.value === "unassigned") { setView("unassigned"); setAssigneeFilter(""); } else setAssigneeFilter(e.target.value); }} aria-label="负责人"><option value="">全部负责人</option><option value="unassigned">未分配</option>{assignees.map((m) => <option key={m.membership_id} value={m.membership_id}>{m.username}</option>)}</select> : null}<span className="flex-spacer"/><button className="btn btn-sm" onClick={() => void load()}>刷新</button></div>
    {canManage && selected.size > 0 ? <div className="batch-bar" role="region" aria-label="批量分配"><b>已选 {selected.size} 个</b><span>改派给</span><select className="select" value={batchAssignee} onChange={(e) => setBatchAssignee(e.target.value)}><option value="">未分配（清空）</option>{assignees.map((m) => <option key={m.membership_id} value={m.membership_id}>{m.username}</option>)}</select><button className="btn btn-primary btn-sm" disabled={busy} onClick={() => void applyBatch()}>{busy ? "处理中…" : "应用"}</button><button className="btn btn-ghost btn-sm" onClick={() => setSelected(new Set())}>取消选择</button></div> : null}
    {videos === null ? <Loading text="加载视频…" /> : displayed.length === 0 ? <Card><EmptyState title={view === "mine" ? "暂无我的任务" : view === "unassigned" ? "暂无待领取视频" : "没有匹配的视频"} hint={view === "mine" ? "可到「待领取」领取未分配视频；你仍可在「全部」进入其他视频。" : view === "unassigned" ? "当前所有视频都已有负责人。" : "调整搜索或筛选条件。"}/></Card> : <>
      {canManage ? <label className="select-all"><input type="checkbox" checked={selectable.length > 0 && selected.size === selectable.length} onChange={toggleAll}/> 选择当前页可改派视频 <span>（草稿、已退回）</span></label> : null}
      <div className="video-grid">{displayed.map((v) => { const assignable = ["draft", "rejected"].includes(v.workflow_status); const mine = v.assignee_membership_id === project?.membership_id; return <article key={v.id} className={`card video-card ${selected.has(v.id) ? "selected" : ""}`}>
        {canManage ? <label className="card-select" title={assignable ? "选择视频" : "当前状态不可改派"}><input type="checkbox" checked={selected.has(v.id)} disabled={!assignable} onChange={() => setSelected((old) => { const n = new Set(old); n.has(v.id) ? n.delete(v.id) : n.add(v.id); return n; })}/><span className="visually-hidden">选择 {v.filename}</span></label> : null}
        <div className="thumb" aria-hidden="true">{v.status === "needs_transcode" ? "⟳" : v.storage_path ? "▶" : "▢"}</div><div className="name" title={v.filename}>{v.filename}</div>
        <div className={`assignee-line ${v.assignee ? "" : "unassigned"}`}><span className="assignee-avatar">{v.assignee?.username.slice(0, 1).toUpperCase() ?? "?"}</span><span>{v.assignee ? <><small>负责人</small><b>{v.assignee.username}{mine ? "（我）" : ""}</b></> : <><small>负责人</small><b>未分配</b></>}</span></div>
        <div className="meta"><span>时长 <b>{formatDuration(v.duration)}</b></span><span>帧率 <b>{v.fps != null ? `${v.fps} fps` : "—"}</b></span><span>分辨率 <b>{v.width != null && v.height != null ? `${v.width} × ${v.height}` : "—"}</b></span><span>状态 <b>{statusLabel(v.status)}</b></span></div>
        {v.status === "needs_transcode" ? <div className="transcode-note" role="note">需先转码：当前源格式不能在浏览器中直接播放，因此暂不可进入标注。</div> : null}
        <div className="workflow-line"><WorkflowBadge value={v.workflow_status} revision={v.annotation_revision}/></div>
        {v.submitted_at || v.approved_at ? <div className="workflow-times">{v.submitted_at ? <span>提交 <b>{formatDate(v.submitted_at)}</b></span> : null}{v.approved_at ? <span>通过 <b>{formatDate(v.approved_at)}</b></span> : null}</div> : null}
        {v.workflow_status === "approved" ? <div className="card-media-summary"><MediaStatusSummary projectId={pid} videoId={v.id}/></div> : null}
        <div className="foot"><span>上传 {formatDate(v.created_at)}</span><StatusBadge value={v.status}/></div>
        <div className="actions card-actions">{view === "unassigned" && !v.assignee ? <button className="btn btn-sm" disabled={actionId === v.id} onClick={() => void perform(v.id, "claim")}>{actionId === v.id ? "领取中…" : "领取任务"}</button> : null}{mine && v.workflow_status === "draft" ? <button className="btn btn-sm btn-ghost" disabled={actionId === v.id} onClick={() => void perform(v.id, "release")}>释放</button> : null}<button className="btn btn-sm btn-primary" disabled={v.status === "needs_transcode"} onClick={() => navigate(`/projects/${pid}/annotate/${v.id}`)}>进入标注 →</button></div>
      </article>; })}</div></>}
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
