/**
 * 时间轴（标注工作台 / 审核工作台共用）：
 * - 按 duration 显示标注区间与当前播放头，点击 / 键盘（tabindex）可跳转
 * - 标注区间按类别着色，类别色仅用于区分行为
 */
import { useMemo, type MouseEvent } from "react";
import type { Category } from "../api/types";
import { formatTime, formatTimeShort } from "../utils/format";

export default function Timeline({
  duration,
  currentTime,
  annotations,
  categoryById,
  draftStartTime,
  draftEndTime,
  draftStartFrame,
  draftEndFrame,
  draftColor,
  onSeek,
}: {
  duration: number;
  currentTime: number;
  annotations: Array<{ id: number; category_id: number; start_time: number; end_time: number }>;
  categoryById: Map<number, Category>;
  draftStartTime?: number | null;
  draftEndTime?: number | null;
  draftStartFrame?: number | null;
  draftEndFrame?: number | null;
  draftColor?: string | null;
  onSeek: (t: number) => void;
}) {
  const ticks = useMemo(() => Array.from({ length: 11 }, (_, i) => (duration * i) / 10), [duration]);
  const draftStartPercent = draftStartTime == null ? null : Math.min(100, Math.max(0, (draftStartTime / duration) * 100));
  const draftStartsNearEnd = draftStartPercent != null && draftStartPercent >= 88;

  function handleSeek(e: MouseEvent<HTMLDivElement>) {
    const rect = e.currentTarget.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    onSeek(frac * duration);
  }

  return (
    <div
      className="timeline"
      onClick={handleSeek}
      title="点击时间轴跳转"
      role="slider"
      tabIndex={0}
      aria-label="时间轴"
      aria-valuemin={0}
      aria-valuemax={Math.max(0, Math.round(duration))}
      aria-valuenow={Math.round(Math.min(currentTime, duration))}
      aria-valuetext={`当前 ${formatTime(currentTime)}；草稿开始 ${draftStartTime == null ? "未设置" : `${formatTime(draftStartTime)}${draftStartFrame == null ? "" : `，帧 ${draftStartFrame}`}`}；结束 ${draftEndTime == null ? "未设置" : `${formatTime(draftEndTime)}${draftEndFrame == null ? "" : `，帧 ${draftEndFrame} inclusive`}`}`}
      onKeyDown={(e) => {
        if (e.key === "ArrowLeft") {
          e.preventDefault();
          onSeek(Math.max(0, currentTime - 1));
        } else if (e.key === "ArrowRight") {
          e.preventDefault();
          onSeek(Math.min(duration, currentTime + 1));
        }
      }}
    >
      <div className="timeline-lanes">
        {annotations.map((a) => {
          const cat = categoryById.get(a.category_id);
          const left = Math.min(100, Math.max(0, (a.start_time / duration) * 100));
          const width = Math.min(100 - left, Math.max(0.15, ((a.end_time - a.start_time) / duration) * 100));
          return (
            <div
              key={a.id}
              className="timeline-interval"
              style={{ left: `${left}%`, width: `${width}%`, background: cat?.color ?? "var(--text-3)" }}
              title={`${cat?.name ?? "未知类别"}：${formatTimeShort(a.start_time)} – ${formatTimeShort(a.end_time)}`}
            />
          );
        })}
        {draftStartTime != null && draftEndTime != null && draftEndTime > draftStartTime ? (
          <div
            className="timeline-draft-interval"
            style={{
              left: `${Math.min(100, Math.max(0, (draftStartTime / duration) * 100))}%`,
              width: `${Math.min(100 - Math.min(100, Math.max(0, (draftStartTime / duration) * 100)), Math.max(0.15, ((draftEndTime - draftStartTime) / duration) * 100))}%`,
              borderColor: draftColor ?? "var(--text-2)",
              background: `color-mix(in srgb, ${draftColor ?? "var(--text-2)"} 18%, transparent)`,
            }}
            aria-hidden="true"
          />
        ) : null}
        {draftStartTime != null ? (
          <div
            className={`timeline-draft-start${draftStartsNearEnd ? " near-end" : ""}`}
            style={{ left: `${draftStartPercent}%`, color: draftColor ?? "var(--text-2)" }}
            aria-hidden="true"
          >
            <span>开始</span>
          </div>
        ) : null}
        <div
          className="playhead"
          style={{ left: `${Math.min(100, Math.max(0, (currentTime / duration) * 100))}%` }}
        />
      </div>
      <div className="timeline-ticks">
        {ticks.map((t, i) => (
          <span key={i} className="timeline-tick" style={{ left: `${(t / duration) * 100}%` }}>
            {formatTimeShort(t)}
          </span>
        ))}
      </div>
    </div>
  );
}
