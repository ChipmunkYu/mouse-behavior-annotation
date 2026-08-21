import type { ParticipantMode, RoleDefinition } from "../api/types";

export function ParticipantSummary({ mode, roles, assignments, mouseIds, compact = false }: {
  mode: ParticipantMode; roles: RoleDefinition[]; assignments: Record<string, number[]>; mouseIds: number[]; compact?: boolean;
}) {
  if (mode !== "role_based") return <span className="participant-summary">参与对象：{mouseIds.length ? mouseIds.map((id) => `Track ${id}`).join("、") : "无"}</span>;
  const sorted = [...roles].sort((a, b) => a.role_sort_order - b.role_sort_order);
  return <span className={`participant-summary role-based${compact ? " compact" : ""}`}>
    {sorted.map((role) => <span className="participant-summary-item" key={role.key}><b>{role.name}：</b>{(assignments[role.key] ?? []).length ? (assignments[role.key] ?? []).map((id) => `Track ${id}`).join("、") : "未分配"}</span>)}
  </span>;
}
