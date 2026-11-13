import { useCallback, type Dispatch, type SetStateAction } from "react";
import type { RoleDefinition } from "../api/types";
import type { useConfirm } from "./ConfirmDialog";
import { isRoleAccessible } from "../utils/roleNavigation";

/** Controlled so the manual page keeps its existing draft/undo state. */
export function useRoleAssignment({ roleDefinitions, participantRoles, activeRoleKey, unlockedRoleKeys, setParticipantRoles, setSelectedMouseIds, setActiveRoleKey, setUnlockedRoleKeys, setRoleMessage, confirm, beforeToggle }: {
  roleDefinitions: RoleDefinition[]; participantRoles: Record<string, number[]>; activeRoleKey: string | null; unlockedRoleKeys: Set<string>;
  setParticipantRoles: Dispatch<SetStateAction<Record<string, number[]>>>;
  setSelectedMouseIds: Dispatch<SetStateAction<number[]>>;
  setActiveRoleKey: Dispatch<SetStateAction<string | null>>;
  setUnlockedRoleKeys: Dispatch<SetStateAction<Set<string>>>;
  setRoleMessage: Dispatch<SetStateAction<string | null>>;
  confirm: ReturnType<typeof useConfirm>[1]; beforeToggle?: () => boolean;
}) {
  const activateRole = useCallback((key: string) => {
    if (!isRoleAccessible(roleDefinitions, participantRoles, unlockedRoleKeys, key)) { setRoleMessage("请先补全前序角色的最少参与对象，再进入这个角色。"); return; }
    setUnlockedRoleKeys((keys) => new Set([...keys, key])); setActiveRoleKey(key); setRoleMessage(null);
  }, [roleDefinitions, participantRoles, unlockedRoleKeys, setUnlockedRoleKeys, setActiveRoleKey, setRoleMessage]);

  const removeRoleTrack = (key: string, id: number) => {
    setParticipantRoles((prev) => ({ ...prev, [key]: (prev[key] ?? []).filter((x) => x !== id) }));
    setSelectedMouseIds((ids) => [...new Set([...ids, id])].sort((a, b) => a - b));
    setRoleMessage(`已移除 Track ${id}，已放回待分配。`);
  };

  const toggleRoleTrack = async (id: number) => {
    if (beforeToggle && !beforeToggle()) return;
    const role = roleDefinitions.find((r) => r.key === activeRoleKey);
    if (!role) { setRoleMessage("请先选择一个角色槽位。"); return; }
    const currentKey = roleDefinitions.find((r) => (participantRoles[r.key] ?? []).includes(id))?.key;
    if (currentKey === role.key) { removeRoleTrack(role.key, id); return; }
    if ((participantRoles[role.key] ?? []).length >= (role.max_count ?? Number.POSITIVE_INFINITY)) { setRoleMessage(`${role.name}已达到最多 ${role.max_count} 个参与对象，请先移除后再添加。`); return; }
    if (currentKey) {
      const oldRole = roleDefinitions.find((r) => r.key === currentKey)!;
      if (!await confirm({ title: `移至“${role.name}”？`, message: <>Track {id} 当前属于“<b>{oldRole.name}</b>”。确认后会从原角色移除，并一次性加入当前角色。</>, confirmLabel: "移至当前角色" })) { setRoleMessage(`Track ${id} 仍保留在“${oldRole.name}”。`); return; }
    }
    setParticipantRoles((prev) => {
      const next = Object.fromEntries(roleDefinitions.map((r) => [r.key, (prev[r.key] ?? []).filter((x) => x !== id)]));
      next[role.key] = [...next[role.key], id].sort((a, b) => a - b); return next;
    });
    setSelectedMouseIds((ids) => ids.filter((trackId) => trackId !== id));
    setRoleMessage(`Track ${id} 已分配给“${role.name}”。`);
  };
  return { activateRole, toggleRoleTrack, removeRoleTrack };
}
