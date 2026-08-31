import type { MediaControllerState } from "../media";

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

export function MediaLoadProgress({
  state,
  onCancel,
  id,
}: {
  state: MediaControllerState;
  onCancel: () => void;
  id?: string;
}) {
  if (state.status !== "downloading" && state.status !== "idle") return null;

  const loaded = state.status === "downloading" ? state.loadedBytes : 0;
  const total = state.status === "downloading" ? state.totalBytes : null;
  const determinate = total !== null && total > 0;
  const percent = determinate ? Math.min(100, Math.round((loaded / total) * 100)) : null;
  const detail = determinate
    ? `${percent}% · ${formatBytes(loaded)} / ${formatBytes(total)}`
    : loaded > 0
      ? `已加载 ${formatBytes(loaded)}`
      : "正在连接视频资源…";

  return (
    <div id={id} className="media-status-overlay media-load-overlay">
      <div className="media-load-card">
        <div className="media-load-heading">
          <span className="media-load-pulse" aria-hidden="true" />
          <span>正在加载视频</span>
          <strong className="mono">{percent === null ? "加载中" : `${percent}%`}</strong>
        </div>
        <div
          className={`media-load-track${determinate ? "" : " is-indeterminate"}`}
          role="progressbar"
          aria-label="视频加载进度"
          aria-valuemin={determinate ? 0 : undefined}
          aria-valuemax={determinate ? 100 : undefined}
          aria-valuenow={percent ?? undefined}
          aria-valuetext={determinate ? `视频已加载 ${percent}%` : "正在加载视频，暂无法计算进度"}
        >
          <span className="media-load-fill" style={determinate ? { width: `${percent}%` } : undefined} />
        </div>
        <div className="media-load-meta">
          <span>{detail}</span>
          {state.status === "downloading" ? <button type="button" className="btn btn-sm" onClick={onCancel}>取消</button> : null}
        </div>
      </div>
    </div>
  );
}
