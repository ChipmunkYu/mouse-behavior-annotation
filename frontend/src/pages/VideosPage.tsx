import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { createVideo, listProjects, listVideos } from "../api";
import type { Project, Video } from "../api/types";
import { ROLE_LABELS } from "../api/types";
import { Card, EmptyState, ErrorBox, Loading, StatusBadge } from "../components/ui";
import { formatDate, formatDuration } from "../utils/format";

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

export default function VideosPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const pid = Number(projectId);

  const [project, setProject] = useState<Project | null>(null);
  const [videos, setVideos] = useState<Video[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("");

  const [showForm, setShowForm] = useState(false);
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

  // 状态筛选选项完全来自当前数据（不硬编码状态枚举）
  const statusOptions = useMemo(() => {
    const set = new Set<string>((videos ?? []).map((v) => v.status));
    return [...set].sort();
  }, [videos]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (videos ?? []).filter((v) => {
      if (statusFilter && v.status !== statusFilter) return false;
      if (q && !v.filename.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [videos, query, statusFilter]);

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
      setShowForm(false);
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
            {project ? <span> · 角色：{ROLE_LABELS[project.role] ?? project.role}</span> : null}
          </div>
          <h1>视频库</h1>
          <div className="sub">共 {videos?.length ?? 0} 个视频 · 展示 {filtered.length} 个</div>
        </div>
        <button type="button" className="btn btn-primary" onClick={() => setShowForm((v) => !v)}>
          {showForm ? "收起" : "+ 新建视频（JSON 元数据）"}
        </button>
      </div>

      {showForm ? (
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
                  placeholder="默认 metadata；如 ready / uploading / error"
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
              <button type="button" className="btn" onClick={() => setShowForm(false)}>
                取消
              </button>
              <button type="submit" className="btn btn-primary" disabled={creating}>
                {creating ? "创建中…" : "创建视频"}
              </button>
            </div>
          </div>
        </form>
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
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          <option value="">全部状态</option>
          {statusOptions.map((s) => (
            <option key={s} value={s}>
              {s}
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
                ? "点击「新建视频（JSON 元数据）」录入 Mock 视频信息"
                : "调整搜索关键词或状态筛选"
            }
          />
        </Card>
      ) : (
        <div className="video-grid">
          {filtered.map((v) => (
            <div key={v.id} className="card video-card">
              <div className="thumb" aria-hidden="true">
                {v.storage_path ? "▶" : "▢"}
              </div>
              <div className="name" title={v.filename}>
                {v.filename}
              </div>
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
                  状态 <b>{v.status}</b>
                </span>
              </div>
              <div className="foot">
                <span className="date">{formatDate(v.created_at)}</span>
                <StatusBadge value={v.status} />
              </div>
              <div className="actions">
                <button
                  type="button"
                  className="btn btn-sm btn-primary"
                  style={{ width: "100%" }}
                  onClick={() => navigate(`/projects/${pid}/annotate/${v.id}`)}
                >
                  进入标注 →
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
