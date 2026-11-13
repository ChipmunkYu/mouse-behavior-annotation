// @vitest-environment jsdom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Category } from "../api/types";
import { MouseIdsPanel, RoleSlotsPanel, IdentityPanel } from "./AnnotationTrackPanels";
import { useRoleAssignment } from "./useRoleAssignment";
import { getInitiallyUnlockedRoleKeys } from "../utils/roleNavigation";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const category: Category = { id: 7, project_id: 1, group: "社交", name: "社交行为", sort_order: 1, is_active: true, participant_mode: "role_based", mouse_count_min: 2, mouse_count_max: 2, role_definitions: [{ key: "actor", name: "发起鼠", min_count: 1, max_count: 1, role_sort_order: 0 }, { key: "target", name: "接受鼠", min_count: 1, max_count: 1, role_sort_order: 1 }] };
const tracks = [12, 31].map((display_track_id) => ({ display_track_id, detection_count: 5, visible_in_current_frame: true }));
let host: HTMLDivElement, root: Root;
const confirm = vi.fn();
beforeEach(() => { host = document.createElement("div"); document.body.append(host); root = createRoot(host); confirm.mockReset().mockResolvedValue(true); });
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });
async function click(selector: string, index = 0) { await act(async () => host.querySelectorAll<HTMLButtonElement>(selector)[index].click()); }

// Same controlled state shape and hook as manual AnnotatePage, without its media/route setup.
function ManualParticipants({ roleBased = true }: { roleBased?: boolean }) {
  const [roles, setRoles] = useState<Record<string, number[]>>({});
  const [pending, setPending] = useState<number[]>([]);
  const [active, setActive] = useState<string | null>("actor");
  const [unlocked, setUnlocked] = useState(() => getInitiallyUnlockedRoleKeys(category.role_definitions, {}));
  const [message, setMessage] = useState<string | null>(null);
  const { activateRole, toggleRoleTrack, removeRoleTrack } = useRoleAssignment({ roleDefinitions: category.role_definitions, participantRoles: roles, activeRoleKey: active, unlockedRoleKeys: unlocked, setParticipantRoles: setRoles, setSelectedMouseIds: setPending, setActiveRoleKey: setActive, setUnlockedRoleKeys: setUnlocked, setRoleMessage: setMessage, confirm });
  return roleBased ? <RoleSlotsPanel category={category} assignments={roles} pendingIds={pending} activeKey={active} unlocked={unlocked} tracks={tracks} disabled={false} message={message} onActivate={activateRole} onTrack={(id) => void toggleRoleTrack(id)} onRemove={removeRoleTrack} onRemovePending={(id) => setPending((ids) => ids.filter((value) => value !== id))} /> : <MouseIdsPanel category={{ ...category, participant_mode: "unordered" }} selected={pending} tracks={tracks} disabled={false} navigationActive={false} focusIndex={0} onFocusIndex={() => {}} onExitNavigation={() => {}} onToggle={(id) => setPending((ids) => ids.includes(id) ? ids.filter((value) => value !== id) : [...ids, id])} />;
}

it("人工角色编辑保留前序解锁、跨角色移位确认、取消保留和移除回待分配", async () => {
  await act(async () => root.render(<ManualParticipants />));
  expect(host.querySelectorAll<HTMLButtonElement>(".role-slot")[1].disabled).toBe(true);
  await click(".mouse-id-item");
  await click(".role-slot", 1);
  confirm.mockResolvedValueOnce(false);
  await click(".mouse-id-item");
  expect(host.querySelector(".role-track-chip")?.textContent).toContain("发起鼠");
  await click(".mouse-id-item");
  expect(host.querySelector(".role-track-chip")?.textContent).toContain("接受鼠");
  await click(".role-slot-chips .role-track-chip");
  expect(host.querySelector(".pending-role-tracks")?.textContent).toContain("Track 12");
  expect(host.querySelectorAll<HTMLButtonElement>(".role-slot")[1].disabled).toBe(false);
});

it("人工无序参与鼠列表与已选项相互同步，移除不触发角色逻辑", async () => {
  await act(async () => root.render(<ManualParticipants roleBased={false} />));
  await click(".mouse-id-item");
  expect(host.querySelectorAll(".mouse-id-item.selected")).toHaveLength(1);
  await click(".mouse-chip");
  expect(host.querySelectorAll(".mouse-id-item.selected")).toHaveLength(0);
  expect(confirm).not.toHaveBeenCalled();
});

it("共享ID控件人工仍有抑制与撤销，判断受限版只保留Split/Merge与身份撤销", async () => {
  const props = { tracks, selected: [12], frame: 10, search: "", showAll: true, busy: false, canRevertIdentity: true, onSearch: vi.fn(), onShowAll: vi.fn(), onToggle: vi.fn(), onSplit: vi.fn(), onMerge: vi.fn(), onRevertIdentity: vi.fn() };
  await act(async () => root.render(<IdentityPanel {...props} />));
  expect(host.textContent).toContain("忽略整个 track");
  await act(async () => root.render(<IdentityPanel {...props} restricted />));
  expect(host.textContent).not.toContain("忽略整个 track");
  expect(host.textContent).not.toContain("Ctrl+Z");
  expect(host.textContent).toContain("撤销上一次 Split / Merge");
  const split = [...host.querySelectorAll("button")].find((button) => button.textContent === "从当前帧 Split")!;
  await act(async () => split.click()); expect(props.onSplit).toHaveBeenCalledOnce();
});
