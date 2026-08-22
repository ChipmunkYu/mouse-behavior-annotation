/** 时间/日期格式化工具。 */

/** 完整格式：mm:ss.mmm 或 h:mm:ss.mmm（小时 > 0 时），用于状态栏/列表精确显示。 */
export function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) seconds = 0;
  const totalMs = Math.round(seconds * 1000);
  const ms = totalMs % 1000;
  const totalSec = Math.floor(totalMs / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  const pad = (n: number, len = 2) => String(n).padStart(len, "0");
  const base = h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  return `${base}.${pad(ms, 3)}`;
}

/** 紧凑格式：m:ss 或 h:mm:ss，用于时间轴刻度。 */
export function formatTimeShort(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) seconds = 0;
  const totalSec = Math.floor(seconds);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/** 时长字段为 null/undefined 时显示占位符。 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return "—";
  return formatTimeShort(seconds);
}

/** 日期时间：YYYY-MM-DD HH:mm。 */
export function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 秒 → 帧（fps 未知时按 DEFAULT_FPS 估算，调用方需自行保证一致性）。 */
export function timeToFrame(time: number, fps: number | null | undefined): number {
  const f = fps && fps > 0 ? fps : 30;
  return Math.max(0, Math.round(time * f));
}

/** 双端 inclusive 帧区间的规范时间边界。帧是权威，时间只由帧与 FPS 派生。 */
export function frameToStartTime(frame: number, fps: number): number {
  return Math.max(0, Math.trunc(frame)) / fps;
}

export function frameToEndTime(frame: number, fps: number): number {
  return (Math.max(0, Math.trunc(frame)) + 1) / fps;
}

export function clampFrame(frame: number, frameCount: number | null | undefined): number {
  const normalized = Math.max(0, Math.trunc(frame));
  return frameCount && frameCount > 0 ? Math.min(normalized, frameCount - 1) : normalized;
}

/** 文件大小：B / KB / MB / GB（≥100 时取整，否则保留 1 位小数）。 */
export function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = bytes;
  let i = -1;
  do {
    v /= 1024;
    i += 1;
  } while (v >= 1024 && i < units.length - 1);
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}
