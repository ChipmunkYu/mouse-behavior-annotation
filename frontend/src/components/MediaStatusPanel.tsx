/**
 * 媒体（片段）生成状态面板（批次 4）。
 *
 * - 完整面板（full）：总数 / 就绪 / 处理中 / 待处理 / 失败统计、aria 进度条、
 *   最近任务状态与错误摘要；仅 approved 显示统计，非 approved 说明“审核后生成”。
 *   `retryable` 为 true（审核工作台）时失败显示「重试生成」；标注工作台只读。
 * - 轮询规则：仅在存在未完成任务（pending / processing 或任务排队 / 运行中）时
 *   定时拉取 media-status；任务落定（完成 / 失败）或拉取失败即停止。
 *   组件卸载（刷新 / 离开页面 / 切换视频）即清理定时器，不产生后台轮询。
 * - 概要（summary）：视频库 approved 卡片用，仅挂载时拉取一次，不轮询。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { generateMedia, getMediaStatus } from "../api";
import type { MediaStatus } from "../api/types";
import { JOB_LABELS } from "../api/types";

const POLL_INTERVAL_MS = 4000;
/** 批准后任务尚未落库时允许的最大空闲轮询次数（防无限轮询）。 */
const MAX_IDLE_POLLS = 4;

/** 是否仍有未完成任务（决定是否继续轮询）。 */
function isBusy(s: MediaStatus): boolean {
  if (s.processing > 0 || s.pending > 0) return true;
  const j = s.latest_job;
  return j != null && (j.status === "queued" || j.status === "running");
}

export function MediaStatusPanel({
  projectId,
  videoId,
  workflowStatus,
  retryable = false,
}: {
  projectId: number;
  videoId: number;
  workflowStatus: string;
  /** 审核工作台失败时提供「重试生成」；标注工作台为只读，不提供任何操作。 */
  retryable?: boolean;
}) {
  const [status, setStatus] = useState<MediaStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const [refreshTick, setRefreshTick] = useState(0);
  const idlePollsRef = useRef(0);

  const approved = workflowStatus === "approved";

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    async function poll() {
      let s: MediaStatus | null = null;
      try {
        s = await getMediaStatus(projectId, videoId);
        if (cancelled) return;
        setStatus(s);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        // 拉取失败不再重试，避免高频请求；保留旧数据或显示错误
        setError(err instanceof Error ? err.message : "加载媒体状态失败");
        return;
      }
      if (cancelled || s == null) return;

      if (isBusy(s)) {
        idlePollsRef.current = 0;
        timer = window.setTimeout(poll, POLL_INTERVAL_MS);
      } else if (approved && s.total === 0 && s.latest_job == null) {
        // 通过后任务可能尚未落库：短暂等待几轮
        if (idlePollsRef.current < MAX_IDLE_POLLS) {
          idlePollsRef.current += 1;
          timer = window.setTimeout(poll, POLL_INTERVAL_MS);
        } else {
          idlePollsRef.current = 0;
        }
      } else {
        idlePollsRef.current = 0;
      }
    }

    void poll();
    return () => {
      cancelled = true;
      if (timer != null) window.clearTimeout(timer);
    };
  }, [projectId, videoId, approved, refreshTick]);

  const handleRefresh = useCallback(() => {
    idlePollsRef.current = 0;
    setRefreshTick((t) => t + 1);
  }, []);

  const handleRetry = useCallback(async () => {
    if (generating) return;
    setGenerating(true);
    setError(null);
    try {
      await generateMedia(projectId, videoId);
      idlePollsRef.current = 0;
      setRefreshTick((t) => t + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "触发视频片段生成失败");
    } finally {
      setGenerating(false);
    }
  }, [projectId, videoId, generating]);

  // 非 approved：只说明“审核后生成”，不展示统计
  if (!approved) {
    return (
      <div className="media-note" role="status">
        ◌ 该视频审核通过后会自动开始生成视频片段，届时在此查看生成进度与结果。
      </div>
    );
  }

  const total = status?.total ?? 0;
  const ready = status?.ready ?? 0;
  const processing = status?.processing ?? 0;
  const pending = status?.pending ?? 0;
  const failed = status?.failed ?? 0;
  const job = status?.latest_job ?? null;
  const percent =
    total > 0 ? Math.round((ready / total) * 100) : Math.max(0, Math.min(100, job?.progress ?? 0));

  let headline = "视频片段生成完成";
  let headlineTone = "ok";
  if (status != null) {
    const busy = isBusy(status);
    if (failed > 0) {
      headline = `部分视频片段生成失败（${failed}）`;
      headlineTone = "danger";
    } else if (busy) {
      headline = "视频片段生成已排队 / 处理中";
      headlineTone = "warn";
    } else if (total === 0 && job == null) {
      headline = "暂无视频片段生成记录";
      headlineTone = "muted";
    } else if (ready < total) {
      headline = "视频片段生成进行中";
      headlineTone = "warn";
    } else {
      headline = "视频片段生成完成";
      headlineTone = "ok";
    }
  }

  let failedError: string | null = null;
  if (failed > 0) {
    const base = `${failed} 个视频片段生成失败`;
    failedError =
      job?.error && job.error.length > 0
        ? `${base}：${job.error}`
        : retryable
          ? `${base}，可点击「重试生成」`
          : `${base}，可在审核工作台重试`;
  }

  return (
    <div className="media-panel">
      <div className="media-head">
        {status == null && error == null ? (
          <span className="media-loading">
            <span className="spinner" aria-hidden="true" /> 加载中…
          </span>
        ) : null}
        {status != null ? (
          <span className={`media-headline ${headlineTone}`} role="status">
            {headline}
          </span>
        ) : null}
        <span className="flex-spacer" />
        <button type="button" className="btn-link" onClick={handleRefresh} disabled={generating}>
          刷新
        </button>
      </div>

      {error != null ? (
        <div className="media-error" role="alert">
          ⚠ {error}
        </div>
      ) : null}

      {status != null ? (
        <>
          <div className="media-stats">
            <span className="media-stat">
              总数 <b>{total}</b>
            </span>
            <span className="media-stat ok">
              就绪 <b>{ready}</b>
            </span>
            <span className="media-stat warn">
              处理中 <b>{processing}</b>
            </span>
            <span className="media-stat">
              待处理 <b>{pending}</b>
            </span>
            {failed > 0 ? (
              <span className="media-stat danger">
                失败 <b>{failed}</b>
              </span>
            ) : null}
          </div>

          <div
            className="media-progress"
            role="progressbar"
            aria-label="视频片段生成进度"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
          >
            <div className="media-progress-fill" style={{ width: `${percent}%` }} />
          </div>

          <div className="media-meta">
            <span className="mono">{percent}%</span>
            {job != null ? (
              <span className={`media-job ${job.status}`} title={job.error ?? undefined}>
                <span className="dot" aria-hidden="true" />
                {JOB_LABELS[job.status] ?? job.status}
              </span>
            ) : (
              <span className="media-job idle">暂无任务</span>
            )}
          </div>

          {failedError != null ? (
            <div className="media-error" role="alert">
              ⚠ {failedError}
            </div>
          ) : null}

          {failed > 0 && retryable ? (
            <div className="media-actions">
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => void handleRetry()}
                disabled={generating}
              >
                {generating ? "重试中…" : "↻ 重试生成"}
              </button>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

/**
 * 视频库 approved 卡片行内概要：挂载时拉取一次，不轮询、不引入高频请求。
 * 拉取失败或尚无片段时静默不显示；详情可在审核 / 标注页查看。
 */
export function MediaStatusSummary({
  projectId,
  videoId,
}: {
  projectId: number;
  videoId: number;
}) {
  const [status, setStatus] = useState<MediaStatus | null>(null);
  const [hidden, setHidden] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getMediaStatus(projectId, videoId)
      .then((s) => {
        if (!cancelled) setStatus(s);
      })
      .catch(() => {
        if (!cancelled) setHidden(true);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, videoId]);

  if (hidden || status == null || status.total === 0) return null;

  const { total, ready, processing, pending, failed } = status;
  let cls = "ok";
  let label = `视频片段：就绪 ${ready}/${total}`;
  if (failed > 0) {
    cls = "danger";
    label = `视频片段：失败 ${failed} · 就绪 ${ready}/${total}`;
  } else if (processing > 0 || pending > 0) {
    cls = "warn";
    label = `视频片段：处理中 ${processing + pending} · 就绪 ${ready}/${total}`;
  }
  return (
    <span className={`media-summary ${cls}`} title="详情请进入审核工作台或行为标注工作台查看">
      {label}
    </span>
  );
}
