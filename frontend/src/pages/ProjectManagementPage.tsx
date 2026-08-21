import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getAssignmentStats, getCategoryScheme, getProjectInvite, listCategorySchemeAudit, listMembers, listProjects, lockCategoryScheme, putCategoryScheme, removeMember, resetProjectInvite, updateMember } from "../api";
import { ApiError } from "../api/client";
import type { AssignmentStats, CategoryScheme, CategorySchemeAudit, CategorySchemeCategoryInput, Membership, Project } from "../api/types";
import CategorySchemeEditor, { normalizeCategorySchemeDraft, toCategorySchemeRequestCategories, validateCategoryScheme } from "../components/CategorySchemeEditor";
import { useConfirm } from "../components/ConfirmDialog";
import { Card, EmptyState, ErrorBox, Loading, StatusBadge } from "../components/ui";
import { formatDate } from "../utils/format";

function friendlyError(err: unknown): string {
  const text = err instanceof Error ? err.message : "操作失败";
  if (text.includes("Project owner cannot")) return "项目所有者不能被修改或移除。";
  if (text.includes("still assigned to videos")) return "该成员仍负责视频。请先改派或清空其负责视频，再删除成员。";
  return text;
}

function schemeConflictMessage(err: ApiError, action: "save" | "lock"): string {
  const detail = err.message;
  if (detail.includes("permanently locked")) return "类别方案已被永久锁定。请重新加载查看服务器上的最终方案。";
  if (detail.includes("being modified") || detail.includes("retry")) return "另一项类别方案操作正在进行，请稍后重试；当前编辑内容仍保留在本页。";
  if (detail.includes("version changed")) return action === "save"
    ? "方案版本已被其他操作更新，当前内容尚未覆盖服务器。请重新加载后再编辑。"
    : "方案版本已变化，请重新加载并核对后再锁定。";
  return friendlyError(err);
}

function toDraft(scheme: CategoryScheme): CategorySchemeCategoryInput[] {
  return scheme.categories.map((c, index) => ({ id: c.id, name: c.name, group: c.group, color: c.color, sort_order: index, is_active: c.is_active, participant_mode: c.participant_mode, role_definitions: c.role_definitions.map((r) => ({ ...r })) , ...(c.participant_mode === "unordered" ? { mouse_count_min: c.mouse_count_min, mouse_count_max: c.mouse_count_max } : {}) }));
}

function CategorySchemeManager({ pid, confirm }: { pid: number; confirm: ReturnType<typeof useConfirm>[1] }) {
  const [scheme, setScheme] = useState<CategoryScheme | null>(null);
  const [draft, setDraft] = useState<CategorySchemeCategoryInput[]>([]);
  const [audit, setAudit] = useState<CategorySchemeAudit[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const locked = scheme?.category_scheme_locked_at != null;

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [next, history] = await Promise.all([getCategoryScheme(pid), listCategorySchemeAudit(pid)]);
      setScheme(next); setDraft(toDraft(next)); setAudit(history);
    } catch (err) { setError(friendlyError(err)); }
    finally { setLoading(false); }
  }, [pid]);
  useEffect(() => { void load(); }, [load]);

  const normalized = normalizeCategorySchemeDraft(draft);
  const validation = validateCategoryScheme(normalized);
  const dirty = scheme != null && JSON.stringify(normalized) !== JSON.stringify(toDraft(scheme));

  async function save() {
    if (!scheme) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      if (!validation.valid) { setError(validation.issues.join("；")); setBusy(false); return; }
      const categories = toCategorySchemeRequestCategories(normalized);
      const next = await putCategoryScheme(pid, { expected_version: scheme.category_scheme_version, categories });
      setScheme(next); setDraft(toDraft(next)); setAudit(await listCategorySchemeAudit(pid)); setNotice(`类别方案已保存为版本 ${next.category_scheme_version}。`);
    } catch (err) {
      setError(err instanceof ApiError && err.status === 409 ? schemeConflictMessage(err, "save") : friendlyError(err));
    } finally { setBusy(false); }
  }

  async function lock() {
    if (!scheme || draft.length === 0) { setError("至少添加并保存一个类别后才能锁定。"); return; }
    if (dirty) { setError("当前方案还有未保存的修改。请先保存完整方案，再核对并锁定。"); return; }
    const summary = normalized.map((c) => <li key={c.id ?? c.sort_order}><b>{c.group} · {c.name}</b>（{c.participant_mode === "unordered" ? "无序参与对象" : c.role_definitions.map((r) => `${r.name} ${r.min_count}–${r.max_count ?? "不限"}`).join("；")}）</li>);
    const ok = await confirm({ title: "永久锁定类别方案？", message: <><p>请最后核对类别与参与对象角色。锁定后将永久不可修改，也不能新增、删除或调整顺序。</p><ul className="scheme-lock-summary">{summary}</ul><b>此操作不可撤销。</b></>, confirmLabel: "确认永久锁定", danger: true });
    if (!ok) return;
    setBusy(true); setError(null);
    try { const next = await lockCategoryScheme(pid, { expected_version: scheme.category_scheme_version }); setScheme(next); setDraft(toDraft(next)); setAudit(await listCategorySchemeAudit(pid)); setNotice("类别方案已永久锁定。"); }
    catch (err) { setError(err instanceof ApiError && err.status === 409 ? schemeConflictMessage(err, "lock") : friendlyError(err)); }
    finally { setBusy(false); }
  }

  if (loading) return <Card title="类别方案"><Loading text="加载类别方案…" /></Card>;
  return <>
    <Card title="类别方案" extra={<span className={`scheme-state ${locked ? "locked" : "draft"}`}>{locked ? "🔒 已永久锁定" : `配置中 · 版本 ${scheme?.category_scheme_version ?? 0}`}</span>}>
      {error ? <ErrorBox message={error} /> : null}{notice ? <div className="ok-box" role="status">✓ {notice}</div> : null}
      {!locked ? <div className="scheme-intro">配置行为类别及参与对象规则。角色标识由系统维护，无需手动填写。</div> : <div className="scheme-intro">以下是已锁定的运行方案，仅供查看。</div>}
      <CategorySchemeEditor value={normalized} onChange={setDraft} disabled={locked} showCompleteness={!locked} emptyHint="新增至少一个类别，保存并核对后即可永久锁定。" />
      {!locked ? <div className="scheme-actions"><span className="flex-spacer" /><button className="btn" disabled={busy} onClick={() => void load()}>重新加载</button><button className="btn btn-primary" disabled={busy || !validation.valid || !dirty} title={!validation.valid ? validation.issues[0] : !dirty ? "当前没有待保存修改" : "保存完整方案"} onClick={() => void save()}>{busy ? "处理中…" : "保存完整方案"}</button><button className="btn btn-danger" disabled={busy || draft.length === 0 || dirty || !validation.valid} title={!validation.valid ? validation.issues[0] : dirty ? "请先保存当前修改" : "核对后永久锁定"} onClick={() => void lock()}>永久锁定…</button></div> : null}
    </Card>
    <Card title="方案历史"><div className="scheme-audit">{audit.length ? [...audit].reverse().map((item) => <div key={item.id}><b>{item.action === "lock" ? "永久锁定" : "保存方案"}</b><span>版本 {item.scheme_version}</span><span>{formatDate(item.created_at)}</span></div>) : <span className="muted">尚无方案操作记录</span>}</div></Card>
  </>;
}

export default function ProjectManagementPage() {
  const pid = Number(useParams<{ projectId: string }>().projectId);
  const [project, setProject] = useState<Project | null>(null); const [members, setMembers] = useState<Membership[] | null>(null); const [stats, setStats] = useState<AssignmentStats | null>(null); const [invite, setInvite] = useState<string | null>(null);
  const [baseError, setBaseError] = useState<string | null>(null); const [inviteError, setInviteError] = useState<string | null>(null); const [notice, setNotice] = useState<string | null>(null); const [busyId, setBusyId] = useState<number | null>(null); const [confirmDialog, confirm] = useConfirm();
  const canManage = project?.role === "owner" || project?.role === "admin";
  const loadBase = useCallback(async () => { try { const projects = await listProjects(); const current = projects.find((p) => p.id === pid) ?? null; setProject(current); if (!current || (current.role !== "owner" && current.role !== "admin")) { setMembers([]); setStats(null); setBaseError(null); return; } const [memberRows, assignmentStats] = await Promise.all([listMembers(pid), getAssignmentStats(pid)]); setMembers(memberRows); setStats(assignmentStats); setBaseError(null); } catch (err) { setBaseError(friendlyError(err)); setMembers([]); } }, [pid]);
  useEffect(() => { void loadBase(); }, [loadBase]);
  const loadInvite = useCallback(async () => { if (!canManage) { setInvite(null); setInviteError(null); return; } try { setInvite((await getProjectInvite(pid)).invite_code); setInviteError(null); } catch (err) { setInvite(null); setInviteError(friendlyError(err)); } }, [canManage, pid]);
  useEffect(() => { void loadInvite(); }, [loadInvite]);
  async function patchMember(member: Membership, patch: { role?: "admin" | "member"; can_review?: boolean }) { setBusyId(member.id); try { await updateMember(pid, member.id, patch); await loadBase(); setNotice(`已更新 ${member.username} 的权限。`); } catch (err) { setBaseError(friendlyError(err)); } finally { setBusyId(null); } }
  async function remove(member: Membership) { if (!await confirm({ title: "删除项目成员", message: `确定从项目中删除「${member.username}」吗？此操作不会删除该用户账号。`, confirmLabel: "删除成员", danger: true })) return; setBusyId(member.id); try { await removeMember(pid, member.id); await loadBase(); setNotice(`已删除成员 ${member.username}。`); } catch (err) { setBaseError(friendlyError(err)); } finally { setBusyId(null); } }
  async function copyInvite() { if (!invite) return; try { await navigator.clipboard.writeText(invite); setNotice("邀请码已复制。失效前请仅发给需要加入项目的人。"); } catch { setInviteError("浏览器未允许复制，请手动选择邀请码复制。"); } }
  async function resetInvite() { if (!await confirm({ title: "重置邀请码", message: "旧邀请码会立即失效，尚未加入的成员需要使用新邀请码。", confirmLabel: "重置邀请码", danger: true })) return; try { const result = await resetProjectInvite(pid); setInvite(result.invite_code); setNotice("邀请码已重置，旧邀请码已失效。"); } catch (err) { setInviteError(friendlyError(err)); } }
  if (members === null) return <div className="container"><Loading text="加载项目管理信息…" /></div>;
  return <div className="container management-page">{confirmDialog}<div className="page-header"><div><div className="breadcrumb"><Link to="/projects">项目</Link> / {project?.name ?? `#${pid}`}</div><h1>项目管理</h1><div className="sub">管理类别方案、成员权限、邀请码与责任分配概况</div></div></div>{baseError ? <ErrorBox message={baseError} /> : null}{notice ? <div className="ok-box" role="status">✓ {notice}</div> : null}
    {project?.role === "owner" ? <CategorySchemeManager pid={pid} confirm={confirm} /> : null}
    {project?.role === "admin" ? <Card title="类别方案" extra={<span className={`scheme-state ${project.category_scheme_locked_at ? "locked" : "draft"}`}>{project.category_scheme_locked_at ? "🔒 已永久锁定" : `配置中 · 版本 ${project.category_scheme_version}`}</span>}><div className="scheme-intro">类别方案只能由项目所有者查看、配置并永久锁定；管理员仍可管理成员、邀请码和任务分配。</div></Card> : null}
    {!canManage ? <Card><EmptyState title="无管理权限" hint="只有项目所有者和管理员可以管理成员与邀请码。" /></Card> : <><div className="stats-strip" aria-label="项目工作流统计">{[[stats?.total,"视频总数"],[stats?.draft,"草稿"],[stats?.submitted,"待审核"],[stats?.approved,"已通过"],[stats?.rejected,"已退回"],[stats?.unassigned,"未分配"]].map(([value,label]) => <div key={String(label)}><b>{value ?? 0}</b><span>{label}</span></div>)}</div><Card title="项目邀请码" extra={<div className="inline-actions"><button className="btn btn-sm" onClick={() => void copyInvite()} disabled={!invite}>复制</button><button className="btn btn-sm btn-danger" onClick={() => void resetInvite()}>重置</button></div>}>{inviteError ? <ErrorBox message={`邀请码加载失败：${inviteError}`} /> : null}<div className="invite-code mono" tabIndex={0}>{invite ?? (inviteError ? "暂不可用" : "加载中…")}</div><div className="field-hint">任何获得邀请码的登录用户都可作为成员加入。重置后旧邀请码立即失效。</div></Card><Card title={`成员（${members.length}）`}><div className="member-table-wrap"><table className="member-table"><thead><tr><th>成员</th><th>角色</th><th>审核权限</th><th>任务进度（总数 / 草稿 / 待审核 / 已通过 / 已退回）</th><th><span className="visually-hidden">操作</span></th></tr></thead><tbody>{members.map((m) => { const owner = m.role === "owner"; const progress = stats?.by_assignee.find((item) => item.assignee_membership_id === m.id); return <tr key={m.id}><td><b>{m.username}</b>{m.id === project?.membership_id ? <span className="you-mark">你</span> : null}<small>用户 #{m.user_id}</small></td><td>{owner ? <StatusBadge value="owner" tone="ok" /> : <select className="select" aria-label={`${m.username} 的角色`} value={m.role} disabled={busyId === m.id} onChange={(e) => void patchMember(m, { role: e.target.value as "admin" | "member" })}><option value="member">成员</option><option value="admin">管理员</option></select>}</td><td>{owner || m.role === "admin" ? <span className="permission-fixed">可审核</span> : <label className="switch-label"><input type="checkbox" checked={m.can_review} disabled={busyId === m.id} onChange={(e) => void patchMember(m, { can_review: e.target.checked })} />允许审核</label>}</td><td><div className="member-progress">{[progress?.total,progress?.draft,progress?.submitted,progress?.approved,progress?.rejected].map((v,i) => <span key={i}><b>{v ?? 0}</b></span>)}</div></td><td>{owner ? <span className="muted">所有者受保护</span> : <button className="btn btn-sm btn-danger" disabled={busyId === m.id} onClick={() => void remove(m)}>删除</button>}</td></tr>; })}</tbody></table></div></Card></>}
  </div>;
}
