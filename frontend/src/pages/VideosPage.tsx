import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { createVideo, listProjects, listVideos } from "../api";
import type { Project, Video } from "../api/types";
import { ROLE_LABELS } from "../api/types";
import {
  Card,
  EmptyState,
  ErrorBox,
  Loading,
  StatusBadge,
  WorkflowBadge,
  statusLabel,
  workflowStatusLabel,
} from "../components/ui";
import { MediaStatusSummary } from "../components/MediaStatusPanel";
import VideoUploadPanel from "../components/VideoUploadPanel";
import { formatDate, formatDuration } from "../utils/format";

/** 可访问审核工作台的项目角色（仅主/次导航对 owner/admin/reviewer 显示）。 */
const REVIEW_ROLES = ["owner", "admin", "reviewer"];

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

/** 待转码的视频：已上传但浏览器可能无法直接播放，明确提示且不提供“进入标注”。 */
function isNeedsTranscode(v: Video): boolean {
  return v.status === "needs_transcode";
}

export default function VideosPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const pid = Number(projectId);

  const [project, setProject] = useState<Project | null>(null);
  const [videos, setVideos] = useState<Video[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [workflowStatusFilter, setWorkflowStatusFilter] = useState("");

  const [uploadOpen, setUploadOpen] = useState(false);

  // 开发用 Mock 元数据表单（折叠区，不参与真实上传）
  const [devOpen, setDevOpen] = useState(false);
  const [form, setForm] = useState<VideoFormState>(EMPTY_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    if (!pid) return;
    try {
      const [projs, vids] = await Promise.all([listProjects(), listVideos(pid)]);
      setProject(projs.find((p) => p.id === pid) ?? null);
      setVideos(vids);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载视频失败");
      setVideos([]);
    }
  }, [pid]);

  useEffect(() => {
    void load();
  }, [load]);

  // 审核状态筛选选项完全来自当前数据（不硬编码状态枚举）
  const workflowStatusOptions = useMemo(() => {
    const set = new Set<string>((videos ?? []).map((v) => v.workflow_status));
    return [...set].sort();
  }, [videos]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (videos ?? []).filter((v) => {
      if (workflowStatusFilter && v.workflow_status !== workflowStatusFilter) return false;
      if (q && !v.filename.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [videos, query, workflowStatusFilter]);

  const handleUploaded = useCallback(async () => {
    await load();
  }, [load]);

  const handleEnterAnnotation = useCallback(
    (video: Video) => {
      navigate(`/projects/${pid}/annotate/${video.id}`);
    },
    [navigate, pid]
  );

  function updateField(key: keyof VideoFormState, value: string) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  function parseNum(value: string): number | null {
    if (value.trim() === "") return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
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

  if (error) {
    return (
      <div className="container">
        <ErrorBox message={error} />
        <div style={{ marginTop: 12 }}>
          <Link to="/projects">← 返回项目列表</Link>
        </div>
      </div>
    );
  }

  return (
    <div className="container">
      <div className="page-header">
        <div>
          <div style={{ fontSize: 12, color: "var(--text-3)", marginBottom: 2 }}>
            <Link to="/projects">项目</Link> / {project?.name ?? `#${pid}`}
            {project ? <span> · 角色：{ROLE_LABELS[project.role] ?? "未知角色"}</span> : null}
          </div>
          <h1>视频库</h1>
          <div className="sub">共 {videos?.length ?? 0} 个视频 · 展示 {filtered.length} 个</div>
        </div>
        <div className="page-header-actions">
          {project && REVIEW_ROLES.includes(project.role) ? (
            <Link to={`/projects/${pid}/review`} className="btn btn-sm review-entry" title="审核提交的视频">
              ✓ 审核工作台
            </Link>
          ) : null}
          <button
            type="button"
            className="btn btn-primary upload-cta"
            onClick={() => setUploadOpen((v) => !v)}
            aria-expanded={uploadOpen}
          >
            {uploadOpen ? "收起" : "↑ 上传视频"}
          </button>
        </div>
      </div>

      {uploadOpen ? (
        <VideoUploadPanel
          projectId={pid}
          onUploaded={() => void handleUploaded()}
          onEnterAnnotation={handleEnterAnnotation}
          onClose={() => setUploadOpen(false)}
        />
      ) : null}

      <div className="video-toolbar">
        <input
          className="input search"
          type="search"
          value={query}
          placeholder="按文件名搜索…"
          onChange={(e) => setQuery(e.target.value)}
        />
        <select
          className="select"
          style={{ width: 150 }}
          value={workflowStatusFilter}
          onChange={(e) => setWorkflowStatusFilter(e.target.value)}
        >
          <option value="">全部审核状态</option>
          {workflowStatusOptions.map((s) => (
            <option key={s} value={s}>
              {workflowStatusLabel(s)}
            </option>
          ))}
        </select>
        <span className="flex-spacer" />
        <button type="button" className="btn btn-sm" onClick={() => void load()}>
          刷新
        </button>
      </div>

      {videos === null ? (
        <Loading />
      ) : filtered.length === 0 ? (
        <Card>
          <EmptyState
            title={videos.length === 0 ? "暂无视频" : "没有匹配的视频"}
            hint={
              videos.length === 0
                ? "点击右上角「上传视频」上传真实视频文件；开发调试可展开页面底部「开发用」区域录入 Mock 元数据"
                : "调整搜索关键词或审核状态筛选"
            }
          />
        </Card>
      ) : (
        <div className="video-grid">
          {filtered.map((v) => (
            <div key={v.id} className="card video-card">
              <div className="thumb" aria-hidden="true">
                {isNeedsTranscode(v) ? "⟳" : v.storage_path ? "▶" : "▢"}
              </div>
              <div className="name" title={v.filename}>
                {v.filename}
              </div>
              {isNeedsTranscode(v) ? (
                <div className="video-note" role="note">
                  已上传，待转码：当前浏览器可能无法播放该视频，转码完成后可正常播放与标注。
                </div>
              ) : null}
              <div className="meta">
                <span>
                  时长 <b>{formatDuration(v.duration)}</b>
                </span>
                <span>
                  帧率 <b>{v.fps != null ? `${v.fps} fps` : "—"}</b>
                </span>
                <span>
                  分辨率 <b>{v.width && v.height ? `${v.width}×${v.height}` : "—"}</b>
                </span>
                <span>
                  状态 <b>{statusLabel(v.status)}</b>
                </span>
              </div>
              <div className="workflow-line">
                <WorkflowBadge value={v.workflow_status} revision={v.annotation_revision} />
                {v.workflow_status === "submitted" && v.submitted_at ? (
                  <span>提交于 {formatDate(v.submitted_at)}</span>
                ) : null}
                {v.workflow_status === "approved" && v.approved_at ? (
                  <span>通过于 {formatDate(v.approved_at)}</span>
                ) : null}
                {/* approved 卡片仅展示一次片段概要，不做高频轮询；详情进入审核 / 标注页查看 */}
                {v.workflow_status === "approved" ? (
                  <MediaStatusSummary projectId={pid} videoId={v.id} />
                ) : null}
              </div>
              <div className="foot">
                <span className="date">{formatDate(v.created_at)}</span>
                <StatusBadge value={v.status} />
              </div>
              {isNeedsTranscode(v) ? (
                <div className="actions">
                  <details className="video-meta-details">
                    <summary>查看元数据</summary>
                    <dl className="video-meta-list">
                      <div>
                        <dt>filename</dt>
                        <dd>{v.filename}</dd>
                      </div>
                      <div>
                        <dt>status</dt>
                        <dd>{v.status}</dd>
                      </div>
                      <div>
                        <dt>storage_path</dt>
                        <dd>{v.storage_path ?? "—"}</dd>
                      </div>
                      <div>
                        <dt>duration / fps</dt>
                        <dd>
                          {formatDuration(v.duration)} · {v.fps != null ? `${v.fps} fps` : "—"}
                        </dd>
                      </div>
                      <div>
                        <dt>分辨率</dt>
                        <dd>{v.width && v.height ? `${v.width}×${v.height}` : "—"}</dd>
                      </div>
                      <div>
                        <dt>created_at</dt>
                        <dd>{formatDate(v.created_at)}</dd>
                      </div>
                    </dl>
                  </details>
                  <button
                    type="button"
                    className="btn btn-sm"
                    style={{ width: "100%" }}
                    disabled
                    title="视频待转码，转码完成后可进入行为标注"
                  >
                    待转码 · 暂不可标注
                  </button>
                </div>
              ) : (
                <div className="actions">
                  <button
                    type="button"
                    className="btn btn-sm btn-primary"
                    style={{ width: "100%" }}
                    onClick={() => navigate(`/projects/${pid}/annotate/${v.id}`)}
                  >
                    进入行为标注 →
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* 开发用：Mock 元数据录入（不经过真实上传，仅本地调试） */}
      <details
        className="dev-panel"
        open={devOpen}
        onToggle={(e) => setDevOpen(e.currentTarget.open)}
      >
        <summary>开发用：Mock 元数据录入</summary>
        <form className="card create-form" onSubmit={handleCreate}>
          <div className="card-body">
            <div className="field">
              <label htmlFor="video-filename">文件名 filename *</label>
              <input
                id="video-filename"
                className="input"
                value={form.filename}
                placeholder="例如 experiment_01.mp4"
                onChange={(e) => updateField("filename", e.target.value)}
              />
            </div>
            <div className="field-row">
              <div className="field">
                <label htmlFor="video-duration">时长 duration（秒）</label>
                <input
                  id="video-duration"
                  className="input"
                  type="number"
                  min="0"
                  step="0.01"
                  value={form.duration}
                  placeholder="如 120.5"
                  onChange={(e) => updateField("duration", e.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor="video-fps">帧率 fps</label>
                <input
                  id="video-fps"
                  className="input"
                  type="number"
                  min="0"
                  step="0.01"
                  value={form.fps}
                  placeholder="如 30"
                  onChange={(e) => updateField("fps", e.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor="video-width">宽度 width</label>
                <input
                  id="video-width"
                  className="input"
                  type="number"
                  min="0"
                  value={form.width}
                  placeholder="如 1920"
                  onChange={(e) => updateField("width", e.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor="video-height">高度 height</label>
                <input
                  id="video-height"
                  className="input"
                  type="number"
                  min="0"
                  value={form.height}
                  placeholder="如 1080"
                  onChange={(e) => updateField("height", e.target.value)}
                />
              </div>
            </div>
            <div className="field-row">
              <div className="field">
                <label htmlFor="video-status">状态 status（可选）</label>
                <input
                  id="video-status"
                  className="input"
                  value={form.status}
                  placeholder="默认 metadata；如 ready / needs_transcode / error"
                  onChange={(e) => updateField("status", e.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor="video-path">存储路径 storage_path（可选）</label>
                <input
                  id="video-path"
                  className="input"
                  value={form.storage_path}
                  placeholder="data/videos/ 内相对路径或绝对路径"
                  onChange={(e) => updateField("storage_path", e.target.value)}
                />
              </div>
            </div>
            <div className="form-error">{formError ?? ""}</div>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button type="button" className="btn" onClick={() => setDevOpen(false)}>
                收起
              </button>
              <button type="submit" className="btn" disabled={creating}>
                {creating ? "创建中…" : "创建视频"}
              </button>
            </div>
          </div>
        </form>
      </details>
    </div>
  );
}
