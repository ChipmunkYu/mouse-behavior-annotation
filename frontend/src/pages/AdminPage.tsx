/**
 * 项目管理 /projects/:projectId/admin：
 * - 成员与项目内角色（增 / 删 / 改角色）
 * - 视频标注任务分配（视频 → 标注者）
 * - 行为类别列表与启停（12 类北医行为）
 * - 存储概览
 * 明确标注：均为演示交互，后端接口尚未接入；不含快捷键配置。
 */
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import {
  addMember,
  getStorageOverview,
  listAssignments,
  listCategories,
  listProjectMembers,
  listProjects,
  removeMember,
  setAssignment,
  setCategoryActive,
  setMemberRole,
} from "../api";
import type { Category, Project } from "../api/types";
import { ROLE_LABELS, WORKFLOW_LABELS } from "../api/types";
import { Card, EmptyState, Loading, StatusBadge, WorkflowBadge } from "../components/ui";
import type { MemberRole, ProjectMember, StorageOverview, TaskAssignment } from "../demo/types";
import { DEMO_MODE } from "../demo/mode";
import { formatDate, formatFileSize } from "../utils/format";

const MEMBER_ROLES: MemberRole[] = ["owner", "admin", "annotator", "reviewer"];

export default function AdminPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const pid = Number(projectId);

  const [project, setProject] = useState<Project | null>(null);
  const [members, setMembers] = useState<ProjectMember[] | null>(null);
  const [assignments, setAssignments] = useState<TaskAssignment[] | null>(null);
  const [categories, setCategories] = useState<Category[] | null>(null);
  const [storage, setStorage] = useState<StorageOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [newName, setNewName] = useState("");
  const [newRole, setNewRole] = useState<MemberRole>("annotator");
  const [adding, setAdding] = useState(false);

  const loadAll = useCallback(async () => {
    if (!pid) return;
    try {
      const [projs, mems, assigns, cats, store] = await Promise.all([
        listProjects(),
        listProjectMembers(pid),
        listAssignments(pid),
        listCategories(pid),
        getStorageOverview(pid),
      ]);
      setProject(projs.find((p) => p.id === pid) ?? null);
      setMembers(mems);
      setAssignments(assigns);
      setCategories(cats);
      setStorage(store);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载项目管理数据失败");
    }
  }, [pid]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  async function handleRoleChange(memberId: number, role: MemberRole) {
    try {
      setMembers(await setMemberRole(pid, memberId, role));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "修改角色失败");
    }
  }

  async function handleAddMember(e: FormEvent) {
    e.preventDefault();
    setAdding(true);
    try {
      setMembers(await addMember(pid, newName, newRole));
      setNewName("");
      setError(null);
      setNotice(`已添加成员 ${newName.trim()}（${ROLE_LABELS[newRole]}）——演示数据`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "添加成员失败");
    } finally {
      setAdding(false);
    }
  }

  async function handleRemoveMember(memberId: number, username: string) {
    try {
      setMembers(await removeMember(pid, memberId));
      setNotice(`已移除成员 ${username}——演示数据`);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "移除成员失败");
    }
  }

  async function handleAssignment(videoId: number, annotatorId: number | null) {
    try {
      setAssignments(await setAssignment(pid, videoId, annotatorId));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存分配失败");
    }
  }

  async function handleToggleCategory(cat: Category) {
    try {
      setCategories(await setCategoryActive(pid, cat.id, !cat.is_active));
      setNotice(`已${cat.is_active ? "停用" : "启用"}类别「${cat.name}」——演示数据`);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "切换类别失败");
    }
  }

  const annotators = (members ?? []).filter((m) => m.role === "annotator");

  return (
    <div className="container">
      <div className="page-header">
        <div>
          <div style={{ fontSize: 12, color: "var(--text-3)", marginBottom: 2 }}>
            <Link to="/projects">项目</Link> / {project?.name ?? `#${pid}`} / 项目管理
          </div>
          <h1>项目管理</h1>
          <div className="sub">成员角色 · 任务分配 · 行为类别 · 存储概览</div>
        </div>
        <button type="button" className="btn btn-sm" onClick={() => void loadAll()}>
          刷新
        </button>
      </div>

      <div className="muted-box" style={{ marginBottom: 12 }} role="note">
        以下均为<b>演示交互</b>：后端接口尚未接入，操作仅修改本地演示数据。不含快捷键配置。
      </div>
      {DEMO_MODE ? (
        <div className="muted-box" style={{ marginBottom: 12 }}>
          演示模式：当前登录用户为 <b>demo</b>（项目所有者），可执行全部管理操作。
        </div>
      ) : null}
      {notice ? (
        <div className="ok-box" role="status" style={{ marginBottom: 12 }}>✓ {notice}</div>
      ) : null}
      {error ? <div className="error-box" role="alert" style={{ marginBottom: 12 }}>⚠ {error}</div> : null}

      <div className="admin-grid">
        {/* 成员与角色 */}
        <Card title={`成员与角色（${members?.length ?? 0}）`}>
          <div className="card-body">
            {members === null ? (
              <Loading text="加载成员…" />
            ) : members.length === 0 ? (
              <EmptyState compact title="暂无成员" />
            ) : (
              <div className="admin-rows">
                {members.map((m) => (
                  <div key={m.id} className="admin-row">
                    <span className="avatar" aria-hidden="true">{m.username.slice(0, 1).toUpperCase()}</span>
                    <span className="admin-row-name">
                      {m.username}
                      {m.is_self ? <span className="admin-self">（我）</span> : null}
                    </span>
                    <span className="admin-row-date">{formatDate(m.joined_at)}</span>
                    <select
                      className="select"
                      style={{ width: 110 }}
                      value={m.role}
                      disabled={m.is_self}
                      title={m.is_self ? "不能修改自己的角色（演示限制）" : "修改项目内角色（演示）"}
                      onChange={(e) => void handleRoleChange(m.id, e.target.value as MemberRole)}
                    >
                      {MEMBER_ROLES.map((r) => (
                        <option key={r} value={r}>
                          {ROLE_LABELS[r]}
                        </option>
                      ))}
                    </select>
                    <button
                      type="button"
                      className="btn btn-sm btn-danger"
                      disabled={m.is_self}
                      title={m.is_self ? "不能移除自己" : "移除成员（演示）"}
                      onClick={() => void handleRemoveMember(m.id, m.username)}
                    >
                      移除
                    </button>
                  </div>
                ))}
              </div>
            )}
            <form className="admin-add-row" onSubmit={handleAddMember}>
              <input
                className="input"
                style={{ width: 160 }}
                value={newName}
                placeholder="用户名"
                aria-label="新成员用户名"
                onChange={(e) => setNewName(e.target.value)}
              />
              <select
                className="select"
                style={{ width: 110 }}
                value={newRole}
                aria-label="新成员角色"
                onChange={(e) => setNewRole(e.target.value as MemberRole)}
              >
                {MEMBER_ROLES.filter((r) => r !== "owner").map((r) => (
                  <option key={r} value={r}>
                    {ROLE_LABELS[r]}
                  </option>
                ))}
              </select>
              <button type="submit" className="btn btn-sm btn-primary" disabled={adding}>
                {adding ? "添加中…" : "+ 添加成员"}
              </button>
            </form>
          </div>
        </Card>

        {/* 任务分配 */}
        <Card title={`视频标注任务分配（${assignments?.length ?? 0}）`}>
          <div className="card-body">
            {assignments === null ? (
              <Loading text="加载任务分配…" />
            ) : assignments.length === 0 ? (
              <EmptyState compact title="暂无任务" hint="视频上传后可在视频库创建标注任务" />
            ) : (
              <div className="admin-rows">
                {assignments.map((a) => (
                  <div key={a.video_id} className="admin-row">
                    <span className="admin-row-name" title={a.video_filename}>
                      {a.video_filename}
                    </span>
                    <WorkflowBadge value={a.video_workflow} />
                    <select
                      className="select"
                      style={{ width: 130 }}
                      value={a.annotator_id ?? ""}
                      aria-label={`分配 ${a.video_filename}`}
                      onChange={(e) => void handleAssignment(a.video_id, e.target.value ? Number(e.target.value) : null)}
                    >
                      <option value="">未分配</option>
                      {annotators.map((m) => (
                        <option key={m.id} value={m.id}>
                          {m.username}
                        </option>
                      ))}
                    </select>
                    <StatusBadge value={a.status} />
                  </div>
                ))}
              </div>
            )}
            <div className="frame-preview" style={{ marginTop: 8 }}>
              提示：任务分配为演示交互，不会通知成员。
            </div>
          </div>
        </Card>

        {/* 行为类别 */}
        <Card title={`行为类别（${categories?.length ?? 0}）· 启停`}>
          <div className="card-body">
            {categories === null ? (
              <Loading text="加载类别…" />
            ) : categories.length === 0 ? (
              <EmptyState compact title="暂无类别" />
            ) : (
              <div className="admin-rows">
                {categories.map((c) => (
                  <div key={c.id} className="admin-row">
                    <span className="swatch" style={{ background: c.color ?? "var(--text-3)" }} aria-hidden="true" />
                    <span className="admin-row-name">
                      {c.name}
                      <span className="admin-cat-group"> {c.group}</span>
                    </span>
                    <span className="mono admin-cat-order">#{c.sort_order + 1}</span>
                    <button
                      type="button"
                      className={c.is_active ? "btn btn-sm toggle on" : "btn btn-sm toggle"}
                      aria-pressed={c.is_active}
                      title={c.is_active ? "点击停用（演示）" : "点击启用（演示）"}
                      onClick={() => void handleToggleCategory(c)}
                    >
                      {c.is_active ? "已启用" : "已停用"}
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Card>

        {/* 存储概览 */}
        <Card title="存储概览">
          <div className="card-body">
            {storage === null ? (
              <Loading text="计算存储概览…" />
            ) : (
              <>
                <div className="storage-stats">
                  <div>
                    <div className="storage-num mono">{storage.total_videos}</div>
                    <div className="storage-label">视频数</div>
                  </div>
                  <div>
                    <div className="storage-num mono">{formatFileSize(storage.total_bytes)}</div>
                    <div className="storage-label">视频总大小（示意）</div>
                  </div>
                  <div>
                    <div className="storage-num mono">{storage.disk_used_gb} GB</div>
                    <div className="storage-label">已用 / 共 {storage.disk_total_gb} GB（示意）</div>
                  </div>
                </div>
                <div className="upload-progress" role="img" aria-label={`磁盘占用 ${Math.round((storage.disk_used_gb / storage.disk_total_gb) * 100)}%`}>
                  <div className="upload-progress-track">
                    <div
                      className="upload-progress-fill"
                      style={{ width: `${Math.min(100, (storage.disk_used_gb / storage.disk_total_gb) * 100)}%` }}
                    />
                  </div>
                </div>
                <div className="workflow-line" style={{ marginTop: 10 }}>
                  {Object.entries(storage.by_workflow).map(([k, n]) => (
                    <span key={k} className="storage-pill">
                      {WORKFLOW_LABELS[k] ?? k} × {n}
                    </span>
                  ))}
                </div>
                <div className="frame-preview" style={{ marginTop: 8 }}>
                  存储数据按视频元数据推算（演示值），不代表真实磁盘用量。
                </div>
              </>
            )}
          </div>
        </Card>
      </div>
    </div>
  );
}
