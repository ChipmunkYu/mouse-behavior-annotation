import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { createProject, joinProject, listProjects } from "../api";
import type { Project } from "../api/types";
import { ROLE_LABELS } from "../api/types";
import { Card, EmptyState, ErrorBox, Loading, StatusBadge } from "../components/ui";
import { formatDate } from "../utils/format";

export default function ProjectsPage() {
  const navigate = useNavigate();

  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [inviteCode, setInviteCode] = useState("");
  const [joining, setJoining] = useState(false);
  const [joinFeedback, setJoinFeedback] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await listProjects();
      setProjects(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载项目失败");
      setProjects([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    setFormError(null);
    const trimmed = name.trim();
    if (!trimmed) {
      setFormError("项目名称不能为空");
      return;
    }
    setCreating(true);
    try {
      const created = await createProject({ name: trimmed, description: description.trim() || null });
      await load();
      setShowForm(false);
      setName("");
      setDescription("");
      navigate(`/projects/${created.id}/videos`);
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "创建项目失败");
    } finally {
      setCreating(false);
    }
  }

  async function handleJoin(e: FormEvent) {
    e.preventDefault();
    const code = inviteCode.trim();
    if (!code) { setJoinFeedback({ tone: "error", text: "请输入邀请码" }); return; }
    const before = projects ?? [];
    setJoining(true); setJoinFeedback(null);
    try {
      const membership = await joinProject(code);
      const duplicate = before.some((p) => p.id === membership.project_id);
      await load();
      setInviteCode("");
      setJoinFeedback({ tone: "ok", text: duplicate ? "你已经是该项目成员，无需重复加入。" : "已加入项目，可以从项目列表进入。" });
    } catch (err) {
      const raw = err instanceof Error ? err.message : "加入项目失败";
      setJoinFeedback({ tone: "error", text: raw.includes("Invite code not found") ? "邀请码无效或已被重置，请向项目管理员确认。" : raw });
    } finally { setJoining(false); }
  }

  return (
    <div className="container">
      <div className="page-header">
        <div>
          <h1>我的项目</h1>
          <div className="sub">创建或加入项目后，在此进入对应视频库进行标注</div>
        </div>
        <button type="button" className="btn btn-primary" onClick={() => setShowForm((v) => !v)}>
          {showForm ? "收起" : "+ 创建项目"}
        </button>
      </div>

      <form className="join-project-bar" onSubmit={handleJoin}>
        <div><b>通过邀请码加入</b><span> 加入后可查看项目内全部视频</span></div>
        <input className="input mono" aria-label="项目邀请码" value={inviteCode} onChange={(e) => setInviteCode(e.target.value)} placeholder="粘贴邀请码" autoComplete="off" />
        <button className="btn" type="submit" disabled={joining}>{joining ? "加入中…" : "加入项目"}</button>
      </form>
      {joinFeedback ? <div className={joinFeedback.tone === "ok" ? "ok-box" : "error-box"} role="status">{joinFeedback.text}</div> : null}

      {showForm ? (
        <form className="card create-form" onSubmit={handleCreate}>
          <div className="card-body">
            <div className="field">
              <label htmlFor="project-name">项目名称 *</label>
              <input
                id="project-name"
                className="input"
                value={name}
                placeholder="例如：顶视群体社会行为标注"
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="project-desc">项目描述（可选）</label>
              <input
                id="project-desc"
                className="input"
                value={description}
                placeholder="简要说明项目目标或数据范围"
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            <div className="form-error">{formError ?? ""}</div>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button type="button" className="btn" onClick={() => setShowForm(false)}>
                取消
              </button>
              <button type="submit" className="btn btn-primary" disabled={creating}>
                {creating ? "创建中…" : "创建项目"}
              </button>
            </div>
          </div>
        </form>
      ) : null}

      {error ? <ErrorBox message={error} /> : null}

      {projects === null ? (
        <Loading />
      ) : projects.length === 0 ? (
        <Card>
          <EmptyState title="暂无项目" hint="创建新项目，或在上方粘贴邀请码加入已有项目" />
        </Card>
      ) : (
        <div className="project-grid">
          {projects.map((p) => (
            <div key={p.id} className="card project-card">
              <div className="name">
                <span>{p.name}</span>
                <StatusBadge value={p.role} tone="ok" />
              </div>
              <div className="desc">{p.description || "无描述"}</div>
              <div className="meta">
                <span>角色：{ROLE_LABELS[p.role] ?? p.role}</span>
                <span>·</span>
                <span>创建于 {formatDate(p.created_at)}</span>
              </div>
              <div className="actions">
                <button type="button" className="btn btn-sm btn-primary" onClick={() => navigate(`/projects/${p.id}/videos`)}>
                  进入视频库 →
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
