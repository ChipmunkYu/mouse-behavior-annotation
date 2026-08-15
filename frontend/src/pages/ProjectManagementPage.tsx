import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getAssignmentStats, getProjectInvite, listMembers, listProjects, removeMember, resetProjectInvite, updateMember } from "../api";
import type { AssignmentStats, Membership, Project } from "../api/types";
import { useConfirm } from "../components/ConfirmDialog";
import { Card, EmptyState, ErrorBox, Loading, StatusBadge } from "../components/ui";

function friendlyError(err: unknown): string {
  const text = err instanceof Error ? err.message : "操作失败";
  if (text.includes("Project owner cannot")) return "项目所有者不能被修改或移除。";
  if (text.includes("still assigned to videos")) return "该成员仍负责视频。请先在视频库改派或清空其负责视频，再删除成员。";
  return text;
}

export default function ProjectManagementPage() {
  const pid = Number(useParams<{ projectId: string }>().projectId);
  const [project, setProject] = useState<Project | null>(null);
  const [members, setMembers] = useState<Membership[] | null>(null);
  const [stats, setStats] = useState<AssignmentStats | null>(null);
  const [invite, setInvite] = useState<string | null>(null);
  const [baseError, setBaseError] = useState<string | null>(null);
  const [inviteError, setInviteError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [confirmDialog, confirm] = useConfirm();
  const canManage = project?.role === "owner" || project?.role === "admin";

  const loadBase = useCallback(async () => {
    try {
      const projects = await listProjects();
      const current = projects.find((p) => p.id === pid) ?? null;
      setProject(current);
      if (!current || (current.role !== "owner" && current.role !== "admin")) {
        setMembers([]); setStats(null); setBaseError(null);
        return;
      }
      const [memberRows, assignmentStats] = await Promise.all([listMembers(pid), getAssignmentStats(pid)]);
      setMembers(memberRows); setStats(assignmentStats); setBaseError(null);
    } catch (err) { setBaseError(friendlyError(err)); setMembers([]); }
  }, [pid]);

  useEffect(() => { void loadBase(); }, [loadBase]);

  const loadInvite = useCallback(async () => {
    if (!canManage) { setInvite(null); setInviteError(null); return; }
    try { setInvite((await getProjectInvite(pid)).invite_code); setInviteError(null); }
    catch (err) { setInvite(null); setInviteError(friendlyError(err)); }
  }, [canManage, pid]);

  useEffect(() => { void loadInvite(); }, [loadInvite]);

  async function patchMember(member: Membership, patch: { role?: "admin" | "member"; can_review?: boolean }) {
    setBusyId(member.id); setBaseError(null); setNotice(null);
    try { await updateMember(pid, member.id, patch); await loadBase(); setNotice(`已更新 ${member.username} 的权限。`); }
    catch (err) { setBaseError(friendlyError(err)); } finally { setBusyId(null); }
  }

  async function remove(member: Membership) {
    if (!await confirm({ title: "删除项目成员", message: `确定从项目中删除「${member.username}」吗？此操作不会删除该用户账号。`, confirmLabel: "删除成员", danger: true })) return;
    setBusyId(member.id); setBaseError(null); setNotice(null);
    try { await removeMember(pid, member.id); await loadBase(); setNotice(`已删除成员 ${member.username}。`); }
    catch (err) { setBaseError(friendlyError(err)); } finally { setBusyId(null); }
  }

  async function copyInvite() {
    if (!invite) return;
    try { await navigator.clipboard.writeText(invite); setNotice("邀请码已复制。失效前请仅发给需要加入项目的人。"); }
    catch { setInviteError("浏览器未允许复制，请手动选择邀请码复制。"); }
  }

  async function resetInvite() {
    if (!await confirm({ title: "重置邀请码", message: "旧邀请码会立即失效，尚未加入的成员需要使用新邀请码。", confirmLabel: "重置邀请码", danger: true })) return;
    try { const result = await resetProjectInvite(pid); setInvite(result.invite_code); setInviteError(null); setNotice("邀请码已重置，旧邀请码已失效。"); }
    catch (err) { setInviteError(friendlyError(err)); }
  }

  if (members === null) return <div className="container"><Loading text="加载项目管理信息…" /></div>;
  return <div className="container management-page">
    {confirmDialog}
    <div className="page-header"><div><div className="breadcrumb"><Link to="/projects">项目</Link> / {project?.name ?? `#${pid}`}</div><h1>项目管理</h1><div className="sub">管理成员权限、邀请码与视频责任分配概况</div></div></div>
    {baseError ? <ErrorBox message={baseError} /> : null}{notice ? <div className="ok-box" role="status">✓ {notice}</div> : null}
    {!canManage ? <Card><EmptyState title="无管理权限" hint="只有项目所有者和管理员可以管理成员与邀请码。" /></Card> : <>
      <div className="stats-strip" aria-label="项目工作流统计"><div><b>{stats?.total ?? 0}</b><span>视频总数</span></div><div><b>{stats?.draft ?? 0}</b><span>草稿</span></div><div><b>{stats?.submitted ?? 0}</b><span>待审核</span></div><div><b>{stats?.approved ?? 0}</b><span>已通过</span></div><div><b>{stats?.rejected ?? 0}</b><span>已退回</span></div><div className={(stats?.unassigned ?? 0) > 0 ? "warn" : ""}><b>{stats?.unassigned ?? 0}</b><span>未分配</span></div></div>
      <Card title="项目邀请码" extra={<div className="inline-actions"><button className="btn btn-sm" onClick={() => void copyInvite()} disabled={!invite}>复制</button><button className="btn btn-sm btn-danger" onClick={() => void resetInvite()}>重置</button></div>}>
        {inviteError ? <ErrorBox message={`邀请码加载失败：${inviteError}`} /> : null}<div className="invite-code mono" tabIndex={0}>{invite ?? (inviteError ? "暂不可用" : "加载中…")}</div><div className="field-hint">任何获得邀请码的登录用户都可作为成员加入。重置后旧邀请码立即失效。</div>
      </Card>
      <Card title={`成员（${members.length}）`}>
        <div className="member-table-wrap"><table className="member-table"><thead><tr><th>成员</th><th>角色</th><th>审核权限</th><th>任务进度（总数 / 草稿 / 待审核 / 已通过 / 已退回）</th><th><span className="visually-hidden">操作</span></th></tr></thead><tbody>{members.map((m) => {
          const owner = m.role === "owner"; const progress = stats?.by_assignee.find((item) => item.assignee_membership_id === m.id); return <tr key={m.id}><td><b>{m.username}</b>{m.id === project?.membership_id ? <span className="you-mark">你</span> : null}<small>用户 #{m.user_id}</small></td><td>{owner ? <StatusBadge value="owner" tone="ok" /> : <select className="select" aria-label={`${m.username} 的角色`} value={m.role} disabled={busyId === m.id} onChange={(e) => void patchMember(m, { role: e.target.value as "admin" | "member" })}><option value="member">成员</option><option value="admin">管理员</option></select>}</td><td>{owner || m.role === "admin" ? <span className="permission-fixed">可审核</span> : <label className="switch-label"><input type="checkbox" checked={m.can_review} disabled={busyId === m.id} onChange={(e) => void patchMember(m, { can_review: e.target.checked })} />允许审核</label>}</td><td><div className="member-progress" aria-label={`${m.username} 的任务进度`}><span className="total"><b>{progress?.total ?? 0}</b><small>总数</small></span><span><b>{progress?.draft ?? 0}</b><small>草稿</small></span><span className="submitted"><b>{progress?.submitted ?? 0}</b><small>待审核</small></span><span className="approved"><b>{progress?.approved ?? 0}</b><small>通过</small></span><span className="rejected"><b>{progress?.rejected ?? 0}</b><small>退回</small></span></div></td><td>{owner ? <span className="muted">所有者受保护</span> : <button className="btn btn-sm btn-danger" disabled={busyId === m.id} onClick={() => void remove(m)}>删除</button>}</td></tr>;
        })}</tbody></table></div>
      </Card>
    </>}
  </div>;
}
