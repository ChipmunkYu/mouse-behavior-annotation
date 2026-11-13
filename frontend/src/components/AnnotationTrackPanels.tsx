import { useEffect, useRef } from "react";
import type { Category, CorrectedTrackSummary, DetectionSuppression } from "../api/types";
import type { OverlayFrameData } from "./DetectionOverlay";
import { Card } from "./ui";
import { isRoleAccessible } from "../utils/roleNavigation";

export function MouseIdsPanel({ tracks, selected, category, disabled, navigationActive, focusIndex, onFocusIndex, onExitNavigation, onToggle }: {
  tracks: CorrectedTrackSummary[];
  selected: number[];
  category: Category | null;
  disabled: boolean;
  navigationActive: boolean;
  focusIndex: number;
  onFocusIndex: (index: number) => void;
  onExitNavigation: () => void;
  onToggle: (id: number) => void;
}) {
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const min = category?.mouse_count_min ?? 1;
  const max = category?.mouse_count_max ?? null;
  const valid = selected.length >= min && (max == null || selected.length <= max);
  const rule = max == null ? `至少 ${min} 个对象` : min === max ? `恰好 ${min} 个对象` : `${min}–${max} 个对象`;
  useEffect(() => {
    if (navigationActive) itemRefs.current[focusIndex]?.scrollIntoView({ block: "nearest" });
  }, [focusIndex, navigationActive]);
  return <Card title="参与对象" className={`mouse-ids-panel${navigationActive ? " keyboard-nav" : ""}`} extra={<span className={valid ? "mouse-count valid" : "mouse-count"}>{selected.length} / {rule}</span>}>
    {navigationActive ? <div className="participant-nav-status" role="status"><span>键盘选择中：↑/↓ 移动，Enter 选择，T 退出</span><button type="button" className="btn-link" onClick={onExitNavigation}>退出 [T / Esc]</button></div> : null}
    {selected.length ? <div className="selected-mice">{selected.map((id) => <button key={id} disabled={disabled} className="mouse-chip selected" onClick={() => onToggle(id)}>track ID {id} ×</button>)}</div> : null}
    <div className="mouse-id-list">{tracks.map((track, index) => <button ref={(node) => { itemRefs.current[index] = node; }} data-participant-item key={track.display_track_id} disabled={disabled} className={`${selected.includes(track.display_track_id) ? "mouse-id-item selected" : "mouse-id-item"}${navigationActive && focusIndex === index ? " keyboard-focused" : ""}`} onClick={() => { onFocusIndex(index); onToggle(track.display_track_id); }}><b>track ID {track.display_track_id}</b><span>{track.visible_in_current_frame ? "当前可见" : `${track.first_frame ?? "?"}–${track.last_frame ?? "?"}`}</span></button>)}</div>
    {!valid && category ? <div className="mouse-rule-warning">“{category.name}”需要{rule}，当前选择不符合规则。</div> : null}
  </Card>;
}

export function RoleSlotsPanel({ category, assignments, pendingIds, activeKey, unlocked, tracks, disabled, message, onActivate, onTrack, onRemove, onRemovePending }: {
  category: Category; assignments: Record<string, number[]>; pendingIds: number[]; activeKey: string | null; unlocked: Set<string>; tracks: CorrectedTrackSummary[]; disabled: boolean; message: string | null;
  onActivate: (key: string) => void; onTrack: (id: number) => void; onRemove: (key: string, id: number) => void; onRemovePending: (id: number) => void;
}) {
  const roles = [...category.role_definitions].sort((a, b) => a.role_sort_order - b.role_sort_order);
  const trackRole = new Map<number, string>(); roles.forEach((r) => (assignments[r.key] ?? []).forEach((id) => trackRole.set(id, r.name)));
  return <Card title="参与对象角色" className="role-slots-panel" extra={<span className="role-slot-legend">按角色分配 · 不会自动切换</span>}>
    {pendingIds.length ? <div className="pending-role-tracks" role="status"><b>待分配：</b>先选择角色槽位，再点击 Track。{pendingIds.map((id) => <button type="button" key={id} disabled={disabled} className="role-track-chip" onClick={() => onRemovePending(id)}>Track {id}<span aria-hidden="true"> ×</span><span className="visually-hidden">从待分配中移除</span></button>)}</div> : null}
    <div className="role-slots" role="list" aria-label="参与对象角色槽位">{roles.map((role) => { const ids = assignments[role.key] ?? []; const complete = ids.length >= role.min_count && (role.max_count == null || ids.length <= role.max_count); const accessible = isRoleAccessible(roles, assignments, unlocked, role.key); return <button type="button" role="listitem" key={role.key} disabled={disabled || !accessible} title={accessible ? `切换到“${role.name}”` : "请先完成所有前序角色的最少数量"} className={`role-slot${activeKey === role.key ? " active" : ""}${complete ? " complete" : " incomplete"}${!accessible ? " locked" : ""}`} onClick={() => onActivate(role.key)} aria-pressed={activeKey === role.key} aria-label={`${role.name}，${complete ? "已完成" : "未完成"}，已选 ${ids.length}`}><span className="role-slot-index">{accessible ? (complete ? "✓" : "!") : "🔒"}</span><span className="role-slot-main"><b>{role.name}</b><small>{ids.length} / {role.max_count == null ? `至少 ${role.min_count}` : role.min_count === role.max_count ? `${role.min_count}` : `${role.min_count}–${role.max_count}`} · {complete ? "已完成" : accessible ? "可点击切换" : "前序完成后可进入"}</small></span>{activeKey === role.key ? <span className="role-active-label">当前</span> : null}</button>; })}</div>
    <div className="role-slot-chips">{roles.map((role) => (assignments[role.key] ?? []).map((id) => <button type="button" key={`${role.key}-${id}`} disabled={disabled} className="role-track-chip" onClick={() => onRemove(role.key, id)}><b>{role.name}</b> · Track {id}<span aria-hidden="true"> ×</span><span className="visually-hidden">移除</span></button>))}{Object.values(assignments).every((ids) => ids.length === 0) ? <span className="muted">先选择角色槽位，再点击视频框或下方 Track。</span> : null}</div>
    <div className="mouse-id-list">{tracks.map((track) => { const assigned = trackRole.get(track.display_track_id); const pending = pendingIds.includes(track.display_track_id); return <button type="button" key={track.display_track_id} disabled={disabled || !activeKey} className={`mouse-id-item${assigned || pending ? " selected" : ""}`} onClick={() => onTrack(track.display_track_id)}><b>Track {track.display_track_id}</b><span>{assigned ? `已分配：${assigned}` : pending ? "待分配 · 点击加入当前角色" : track.visible_in_current_frame ? "当前可见 · 点击加入" : `${track.first_frame ?? "?"}–${track.last_frame ?? "?"}`}</span></button>; })}</div>
    {message ? <div className="mouse-rule-warning" role="status">{message}</div> : null}
  </Card>;
}

export function IdentityPanel({ tracks, selected, frame, search, showAll, busy, suppressions = [], canRevertSuppression = false, canRevertIdentity, canUndoLatest = false, undoBoundary = "", navigationActive = false, focusIndex = 0, onFocusIndex = () => {}, onExitNavigation = () => {}, onSearch, onShowAll, onToggle, onSplit, onMerge, onSuppressTrack, onUndoLatest, onRevertSuppression, onRevertIdentity, restricted = false }: {
  tracks: CorrectedTrackSummary[]; selected: number[]; frame: number; search: string; showAll: boolean; busy: boolean; suppressions?: DetectionSuppression[]; canRevertSuppression?: boolean; canRevertIdentity: boolean;
  canUndoLatest?: boolean; undoBoundary?: string; navigationActive?: boolean; focusIndex?: number; restricted?: boolean;
  onFocusIndex?: (index: number) => void; onExitNavigation?: () => void;
  onSearch: (s: string) => void; onShowAll: (v: boolean) => void; onToggle: (id: number) => void; onSplit: () => void; onMerge: () => void; onSuppressTrack?: () => void; onUndoLatest?: () => void; onRevertSuppression?: (id?: number) => void; onRevertIdentity: () => void;
}) {
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);
  useEffect(() => {
    if (navigationActive) itemRefs.current[focusIndex]?.scrollIntoView({ block: "nearest" });
  }, [focusIndex, navigationActive]);
  return <Card title="track 修正" className={`identity-panel${navigationActive ? " keyboard-nav" : ""}`} extra={<span className="identity-frame mono">帧 {frame}</span>}>
    {navigationActive ? <div className="participant-nav-status" role="status"><span>键盘导航中：↑/↓ 移动，Enter 选择，T 退出</span><button type="button" className="btn-link" onClick={onExitNavigation}>退出 [T / Esc]</button></div> : null}
    <div className="identity-filter"><div className="segmented"><button className={!showAll ? "active" : ""} onClick={() => onShowAll(false)}>当前帧可见</button><button className={showAll ? "active" : ""} onClick={() => onShowAll(true)}>全部 track ID</button></div><input className="input" placeholder="搜索 track ID" aria-label="搜索 track ID" value={search} onChange={(e) => onSearch(e.target.value)} /></div>
    <div className="identity-track-list">{tracks.map((t, index) => <button ref={(node) => { itemRefs.current[index] = node; }} key={t.display_track_id} className={`${selected.includes(t.display_track_id) ? "identity-track selected" : "identity-track"}${navigationActive && focusIndex === index ? " keyboard-focused" : ""}`} onClick={() => { onFocusIndex(index); onToggle(t.display_track_id); }}><span className="track-id mono">track ID {t.display_track_id}</span><span>{t.visible_in_current_frame ? "● 当前可见" : `${t.first_frame ?? "?"} → ${t.last_frame ?? "?"}`}</span><span className="flex-spacer" /><span>{t.detection_count} 框</span></button>)}</div>
    <div className="identity-actions"><button className="btn btn-primary" disabled={busy || selected.length !== 1} onClick={onSplit}>从当前帧 Split</button><button className="btn btn-primary" disabled={busy || selected.length < 2} onClick={onMerge}>Merge 所选</button></div>
    <div className="identity-danger-zone">{!restricted ? <><button className="btn btn-sm btn-danger" disabled={busy || selected.length !== 1} onClick={onSuppressTrack}>忽略整个 track [Delete]</button><button className="btn btn-sm" disabled={busy || !canUndoLatest} onClick={onUndoLatest}>撤销上一次 track 修正 [Ctrl+Z]</button><button className="btn btn-sm" disabled={busy || !canRevertSuppression} onClick={() => onRevertSuppression?.()}>撤销上一次忽略</button></> : null}<button className="btn btn-sm" disabled={busy || !canRevertIdentity} onClick={onRevertIdentity}>撤销上一次 Split / Merge</button></div>
    {undoBoundary ? <div className="frame-preview">{undoBoundary}</div> : null}
    {!restricted && suppressions.length ? <div className="identity-track-list">{suppressions.map((s) => <div key={s.id} className="identity-track"><span>忽略记录 #{s.id}</span><span>{s.frozen_detection_count} 个检测</span><span className="flex-spacer" /><button className="btn btn-sm" disabled={busy} onClick={() => onRevertSuppression?.(s.id)}>撤销</button></div>)}</div> : null}
  </Card>;
}


export function tracksForOverlayFrame(
  items: CorrectedTrackSummary[],
  frameData: Pick<OverlayFrameData, "status" | "detections">,
  selectedTrackIds: number[],
  showAll: boolean,
  search: string,
): CorrectedTrackSummary[] {
  const complete = frameData.status === "complete";
  const visibleIds = new Set(frameData.detections.map((item) => item.display_track_id));
  const withVisibility = items.map((item) => ({
    ...item,
    visible_in_current_frame: complete ? visibleIds.has(item.display_track_id) : null,
  }));
  return showAll || search || !complete
    ? withVisibility
    : withVisibility.filter((item) => item.visible_in_current_frame || selectedTrackIds.includes(item.display_track_id));
}
