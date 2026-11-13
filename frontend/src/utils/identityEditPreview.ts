import { checkIdentityEdit } from "../api";
import type { IdentityEditCheckRequest } from "../api/types";

/** Both editors stop at conflicts before presenting any commit action. */
export async function identityEditPreview(pid: number, vid: number, request: IdentityEditCheckRequest) {
  const check = await checkIdentityEdit(pid, vid, request);
  if (check.conflicts?.length) throw new Error(`${check.message ?? "角色分配冲突"}：${check.conflicts.map((c) => `标注 #${c.annotation_id} · 帧 ${c.start_frame}–${c.end_frame} / ${c.start_time}–${c.end_time} 秒 · ${c.role_name ?? c.role_key} · Track ${c.track_id}`).join("；")}`);
  if (check.conflict_frames?.length) throw new Error(`Merge 冲突：帧 ${check.conflict_frames.slice(0, 12).join("、")} 同一 track ID 将对应多个框，请先处理误检框`);
  return check;
}
