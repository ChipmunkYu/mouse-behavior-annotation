import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { createProject, joinProject, listProjects } from "../api";
import type { CategorySchemeCategoryInput, Project } from "../api/types";
import { ROLE_LABELS } from "../api/types";
import CategorySchemeEditor, { toCategorySchemeRequestCategories, validateCategoryScheme } from "../components/CategorySchemeEditor";
import { Card, EmptyState, ErrorBox, Loading, StatusBadge } from "../components/ui";
import { formatDate } from "../utils/format";

export default function ProjectsPage() {
  const navigate = useNavigate();

  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [categories, setCategories] = useState<CategorySchemeCategoryInput[]>([]);
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
    const validation = validateCategoryScheme(categories, { requireColor: true });
    if (!validation.valid) {
      setFormError(`行为类别方案尚未完整：${validation.issues.join("；")}`);
      return;
    }
    setCreating(true);
    try {
      const created = await createProject({
        name: trimmed,
        description: description.trim() || null,
        categories: toCategorySchemeRequestCategories(categories),
      });
      setShowForm(false);
      setName("");
      setDescription("");
      setCategories([]);
      navigate(`/projects/${created.id}/manage`);
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "创建项目失败");
    } finally {
      setCreating(false);
    }
  }

  const createValidation = validateCategoryScheme(categories, { requireColor: true });
  const createDisabled = creating || !name.trim() || !createValidation.valid;

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
        <form className="card create-form create-project-with-scheme" onSubmit={handleCreate}>
          <div className="card-body">
            <div className="create-project-heading">
              <div><span className="scheme-order">1</span><div><b>项目信息</b><small>先命名项目，再定义其完整行为类别方案</small></div></div>
              <span className="scheme-state draft">创建后保持未锁定</span>
            </div>
            <div className="create-project-basics">
              <div className="field">
                <label htmlFor="project-name">项目名称 *</label>
                <input id="project-name" className="input" value={name} maxLength={128} placeholder="例如：顶视群体社会行为标注" onChange={(e) => { setName(e.target.value); setFormError(null); }} />
              </div>
              <div className="field">
                <label htmlFor="project-desc">项目描述（可选）</label>
                <input id="project-desc" className="input" value={description} placeholder="简要说明项目目标或数据范围" onChange={(e) => setDescription(e.target.value)} />
              </div>
            </div>
            <div className="create-project-heading scheme-step-heading"><div><span className="scheme-order">2</span><div><b>行为类别方案</b><small>至少主动添加一个完整类别；创建项目与方案一次提交</small></div></div></div>
            <CategorySchemeEditor value={categories} onChange={(next) => { setCategories(next); setFormError(null); }} requireColor emptyHint="点击“新增类别”开始配置；不会先创建空项目。" />
            {formError ? <div className="error-box" role="alert">{formError}</div> : null}
            <div className="create-project-actions">
              <div className="create-submit-hint">{createValidation.valid ? "方案完整。创建后请在项目管理中复核并决定是否永久锁定。" : createValidation.issues[0]}</div>
              <button type="button" className="btn" disabled={creating} onClick={() => setShowForm(false)}>取消</button>
              <button type="submit" className="btn btn-primary" disabled={createDisabled} title={!name.trim() ? "请填写项目名称" : !createValidation.valid ? createValidation.issues[0] : "创建项目并进入方案复核"}>
                {creating ? "创建中…" : "创建项目并复核 →"}
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
