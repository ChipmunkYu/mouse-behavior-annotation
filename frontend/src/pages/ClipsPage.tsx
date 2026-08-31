/**
 * 片段库 /projects/:projectId/clips（批次 5）：
 * - 类别计数 chips 筛选 + 搜索框（按文件名 / 类别名，作用于当前服务端分页页）+ 视频选择器
 * - 片段卡片：缩略图通过项目 ID 与片段 ID 拉取，失败时回退 SVG 占位、
 *   类别颜色、视频文件名、起止时间、时长、审核状态、标注者、片段生成状态 chip
 * - 顶部共享预览区：点击片段后在预览区加载源视频 blob 并跳转到 start_time，
 *   播放范围限制在 [start_time, end_time]（到点自动暂停），一次只播放一个片段；
 *   「跳转到标注」回到 /projects/:pid/annotate/:vid?t=start（标注工作台已支持 ?t= 定位）
 * - 分页（page / page_size，默认 20）；仅当当前页存在“待生成”片段时才轮询刷新，
 *   离开页面 / 切换筛选即停止，绝不批量预加载视频
 * - 键盘与焦点：卡片为按钮（Enter/Space 选择预览），筛选 / 分页均为原生控件
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  fetchClipThumbnailUrl,
  listCategories,
  listClipCategories,
  listClips,
  listProjects,
  listVideos,
} from "../api";
import type {
  Category,
  ClipCategoryCount,
  ClipItem,
  ClipListParams,
  Project,
  Video,
} from "../api/types";
import { DEFAULT_PAGE_SIZE } from "../api/types";
import { Card, EmptyState, ErrorBox, Loading, StatusBadge } from "../components/ui";
import { formatDate, formatDuration, formatTime, formatTimeShort } from "../utils/format";
import { ParticipantSummary } from "../components/ParticipantSummary";
import { MediaLoadProgress } from "../components/MediaLoadProgress";
import { useMediaSource } from "../media";
import { canRequestClipThumbnail, clipItemKey, effectiveClipMediaStatus, shouldPollClipMedia } from "../api/mediaCapabilities";

/** 待生成片段存在时自动刷新列表的间隔。 */
const POLL_INTERVAL_MS = 5000;

/** 片段生成状态完全以后端 media_status 为准。 */
function ClipStatusChip({ status }: { status: ClipItem["media_status"] }) {
  const ready = status === "ready";
  const failed = status === "failed";
  return (
    <span className={ready ? "badge badge-ok" : failed ? "badge badge-danger" : "badge badge-muted"}>
      {ready ? "视频片段已生成" : failed ? "视频片段生成失败" : status === "processing" ? "视频片段生成中" : status === "stale" ? "视频片段待更新" : "视频片段待生成"}
    </span>
  );
}

/** 标注审核状态徽标：与审核工作台一致，明确着色（pending 灰 / approved 绿 / rejected 红）。 */
function ReviewStatusBadge({ value }: { value: string }) {
  const tone = value === "approved" ? "ok" : value === "rejected" ? "danger" : undefined;
  return <StatusBadge value={value} tone={tone} />;
}

/** 缩略图仅在媒体就绪后按项目 ID + Clip ID 请求。 */
function ClipThumb({ projectId, clip, color }: { projectId: number; clip: ClipItem; color?: string | null }) {
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    let objectUrl: string | null = null;
    if (!canRequestClipThumbnail(clip.media_status, clip.clip_id)) {
      setUrl(null);
      return;
    }
    setUrl(null);
    fetchClipThumbnailUrl(projectId, clip.clip_id).then((u) => {
      if (!alive) {
        if (u) URL.revokeObjectURL(u);
        return;
      }
      objectUrl = u;
      setUrl(u);
    });
    return () => {
      alive = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [projectId, clip.clip_id, clip.media_status]);

  if (url) {
    return (
      <img
        src={url}
        alt={`${clip.category_name} 片段缩略图`}
        loading="lazy"
        onError={() => {
          URL.revokeObjectURL(url);
          setUrl(null);
        }}
      />
    );
  }
  return <ThumbPlaceholder color={color} />;
}

/**
 * SVG 缩略图占位：纯变量色绘制（深色 / 浅色主题都清晰），顶部细条复用类别色，
 * 透明背景下同样可读（描边使用 --text-3 / --border-strong）。
 */
function ThumbPlaceholder({ color }: { color?: string | null }) {
  return (
    <svg
      className="clip-thumb-svg"
      viewBox="0 0 160 90"
      preserveAspectRatio="xMidYMid slice"
      role="img"
      aria-label="片段缩略图（暂无）"
    >
      <rect x="8" y="8" width="144" height="74" rx="5" fill="none" stroke="var(--border-strong)" strokeDasharray="5 5" />
      <rect x="56" y="32" width="48" height="26" rx="3" fill="none" stroke="var(--text-3)" strokeWidth="2.5" />
      <path d="M66 38v14l12-7z" fill="var(--text-3)" />
      <circle cx="26" cy="20" r="2" fill="var(--border-strong)" />
      <circle cx="40" cy="20" r="2" fill="var(--border-strong)" />
      <circle cx="54" cy="20" r="2" fill="var(--border-strong)" />
      <circle cx="26" cy="70" r="2" fill="var(--border-strong)" />
      <circle cx="40" cy="70" r="2" fill="var(--border-strong)" />
      <circle cx="54" cy="70" r="2" fill="var(--border-strong)" />
      {color ? <rect x="0" y="0" width="160" height="5" fill={color} /> : null}
    </svg>
  );
}

/** 预览区播放范围条：高亮 [start, end] 区间与播放头，点击 / 键盘可自由跳转。 */
function ClipRangeBar({
  duration,
  start,
  end,
  currentTime,
  color,
  onSeek,
}: {
  duration: number;
  start: number;
  end: number;
  currentTime: number;
  color: string;
  onSeek: (t: number) => void;
}) {
  const pct = useCallback(
    (t: number) => (duration > 0 ? Math.min(100, Math.max(0, (t / duration) * 100)) : 0),
    [duration]
  );
  const ticks = useMemo(() => Array.from({ length: 5 }, (_, i) => (duration * i) / 4), [duration]);
  const segLeft = pct(start);
  const segWidth = Math.max(0.4, pct(end) - pct(start));

  return (
    <div
      className="clip-range"
      role="slider"
      tabIndex={0}
      aria-label="标注区间"
      aria-valuemin={0}
      aria-valuemax={Math.max(0, Math.round(duration))}
      aria-valuenow={Math.round(Math.min(currentTime, duration))}
      aria-valuetext={formatTimeShort(currentTime)}
      title="点击跳转 (←/→ 步进 1 秒)"
      onClick={(e) => {
        const rect = e.currentTarget.getBoundingClientRect();
        const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
        onSeek(frac * duration);
      }}
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
      <div className="clip-range-track">
        <div
          className="clip-range-seg"
          style={{ left: `${segLeft}%`, width: `${segWidth}%`, background: color }}
        />
        <span className="clip-range-seg-label" style={{ left: `${segLeft}%` }}>
          {formatTimeShort(start)}
        </span>
        <span className="clip-range-seg-label" style={{ left: `${segLeft + segWidth}%` }}>
          {formatTimeShort(end)}
        </span>
        <div className="clip-range-playhead" style={{ left: `${pct(currentTime)}%` }} />
      </div>
      <div className="clip-range-ticks">
        {ticks.map((t, i) => (
          <span key={i} className="clip-range-tick" style={{ left: `${pct(t)}%` }}>
            {formatTimeShort(t)}
          </span>
        ))}
      </div>
    </div>
  );
}

export default function ClipsPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const pid = Number(projectId);

  const videoRef = useRef<HTMLVideoElement>(null);
  // 预览播放范围限制读取最新选中项（内联事件处理器每次渲染重建，此处仅用于 step/toggle 辅助）
  const selectedRef = useRef<ClipItem | null>(null);
  const previousSelectionRef = useRef<{ itemKey: string; videoId: number } | null>(null);

  const [project, setProject] = useState<Project | null>(null);
  const [videos, setVideos] = useState<Video[] | null>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  const [counts, setCounts] = useState<ClipCategoryCount[]>([]);

  const [items, setItems] = useState<ClipItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [pages, setPages] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 筛选与分页（全部类型化，服务端只认已声明的参数）
  const [categoryFilter, setCategoryFilter] = useState<number | null>(null);
  const [videoFilter, setVideoFilter] = useState<number | null>(null);
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [refreshTick, setRefreshTick] = useState(0);

  // 预览区（一次只播放一个）
  const [selected, setSelected] = useState<ClipItem | null>(null);
  const [duration, setDuration] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(true);
  const [rangeMsg, setRangeMsg] = useState<string | null>(null);

  selectedRef.current = selected;
  const handleMediaReady = useCallback((reason: "initial" | "retry-restored", element: HTMLVideoElement) => {
    setDuration(element.duration);
    if (reason !== "initial") return;
    const target = Math.min(selectedRef.current?.start_time ?? 0, element.duration || 0);
    element.currentTime = target;
    setCurrentTime(target);
    void element.play().catch(() => {
      /* 自动播放被浏览器拦截时保持暂停，等待用户点击 */
    });
  }, []);
  const media = useMediaSource({ videoId: selected?.video_id ?? null, surface: "clips", videoRef, onReady: handleMediaReady });

  const categoryById = useMemo(
    () => new Map(categories.map((c) => [c.id, c] as const)),
    [categories]
  );

  /* ---------- 元数据（项目 / 视频 / 完整类别含颜色 / 片段类别计数） ---------- */
  const loadMeta = useCallback(async () => {
    try {
      const [projs, vids, cats, cnts] = await Promise.all([
        listProjects(),
        listVideos(pid),
        listCategories(pid),
        listClipCategories(pid),
      ]);
      setProject(projs.find((p) => p.id === pid) ?? null);
      setVideos(vids);
      setCategories(cats);
      setCounts(cnts);
    } catch {
      // 元数据失败不阻塞列表；缺失时界面自动退化为「全部」chip 与无视频选择
    }
  }, [pid]);

  /* ---------- 片段列表（silent = 轮询静默刷新，不闪 loading / 不改错误态） ---------- */
  const loadClips = useCallback(
    async (params: ClipListParams, opts: { silent?: boolean } = {}) => {
      if (!opts.silent) {
        setLoading(true);
        setError(null);
      }
      try {
        const res = await listClips(pid, params);
        setItems(res.items);
        setTotal(res.total);
        setPages(Math.max(1, res.pages));
        // 筛选/换页后页码超出实际页数（如某类别只有 1 页）：自动回到最后一页，避免停留在空页
        if (res.items.length === 0 && res.total > 0 && res.pages < params.page) {
          setPage(Math.max(1, res.pages));
        }
      } catch (err) {
        if (opts.silent) return; // 轮询失败静默保留旧数据
        setItems([]);
        setError(err instanceof Error ? err.message : "加载片段失败");
      } finally {
        if (!opts.silent) setLoading(false);
      }
    },
    [pid]
  );

  useEffect(() => {
    void loadMeta();
  }, [loadMeta]);

  // 搜索输入防抖（300ms）：避免每次按键都发请求，输入变化回到第 1 页
  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedQuery(query.trim()), 300);
    return () => window.clearTimeout(t);
  }, [query]);

  // 切换项目时重置筛选、分页与预览
  useEffect(() => {
    setSelected(null);
    setQuery("");
    setDebouncedQuery("");
    setCategoryFilter(null);
    setVideoFilter(null);
    setPage(1);
    setPageSize(DEFAULT_PAGE_SIZE);
  }, [pid]);

  useEffect(() => {
    void loadClips({
      category_id: categoryFilter,
      video_id: videoFilter,
      search: debouncedQuery,
      page,
      page_size: pageSize,
    });
  }, [loadClips, categoryFilter, videoFilter, debouncedQuery, page, pageSize, refreshTick]);

  /* ---------- 轮询：仅当前页存在「待生成」片段时自动刷新，离开页面即停止 ---------- */
  const hasPending = useMemo(() => (items ?? []).some((c) => shouldPollClipMedia(effectiveClipMediaStatus(c.media_status, c.clip_id))), [items]);

  useEffect(() => {
    if (!hasPending) return;
    let alive = true;
    let timer: number | undefined;
    const tick = () => {
      timer = window.setTimeout(async () => {
        if (!alive) return;
        await loadClips(
          {
            category_id: categoryFilter,
            video_id: videoFilter,
            search: debouncedQuery,
            page,
            page_size: pageSize,
          },
          { silent: true }
        );
        if (alive) tick(); // items 更新后由本 effect 重新评估是否继续
      }, POLL_INTERVAL_MS);
    };
    tick();
    return () => {
      alive = false;
      if (timer != null) window.clearTimeout(timer);
    };
  }, [hasPending, loadClips, categoryFilter, videoFilter, debouncedQuery, page, pageSize]);

  /* ---------- 预览：切换片段时重置页面显示状态；媒体生命周期由公共 controller 管理 ---------- */
  useEffect(() => {
    const previous = previousSelectionRef.current;
    const next = selected
      ? { itemKey: clipItemKey(selected), videoId: selected.video_id }
      : null;
    if (previous?.itemKey === next?.itemKey && previous?.videoId === next?.videoId) return;
    previousSelectionRef.current = next;

    if (!selected) {
      setDuration(0);
      setCurrentTime(0);
      setPlaying(false);
      setMuted(true);
      setRangeMsg(null);
      return;
    }
    setDuration(0);
    setCurrentTime(0);
    setPlaying(false);
    setMuted(true);
    setRangeMsg(null);
    if (previous && previous.itemKey !== clipItemKey(selected) && previous.videoId === selected.video_id) {
      media.reload();
    }
  }, [selected, media.reload]);

  /* ---------- 播放控制 ---------- */
  function togglePlay() {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) void v.play();
    else v.pause();
  }

  function seekTo(t: number) {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = Math.min(Math.max(0, t), v.duration || t);
  }

  function jumpToStart() {
    const s = selectedRef.current;
    if (!s) return;
    seekTo(s.start_time);
  }

  function toggleMute() {
    const v = videoRef.current;
    if (!v) return;
    v.muted = !v.muted;
    setMuted(v.muted);
  }

  function selectClip(c: ClipItem) {
    if (selectedRef.current && clipItemKey(selectedRef.current) === clipItemKey(c)) {
      jumpToStart(); // 重复点击同一片段：回到起点重播，不重新拉流
      return;
    }
    setSelected(c);
  }

  /* ---------- 渲染 ---------- */
  const previewColor = selected
    ? categoryById.get(selected.category_id)?.color ?? "var(--text-3)"
    : "var(--text-3)";
  const inRange =
    selected != null && currentTime >= selected.start_time - 0.05 && currentTime <= selected.end_time + 0.05;
  const filterActive = categoryFilter != null || videoFilter != null || debouncedQuery.length > 0;
  const emptyTotal = (items ?? []).length === 0 && total === 0;

  return (
    <div className="container clips-page">
      <div className="page-header">
        <div>
          <div style={{ fontSize: 12, color: "var(--text-3)", marginBottom: 2 }}>
            <Link to="/projects">项目</Link> / {project?.name ?? `#${pid}`}
          </div>
          <h1>片段库</h1>
          <div className="sub">
            共 {total} 个片段
            {loading && items !== null ? (
              <span className="inline-loading">
                <span className="spinner" aria-hidden="true" /> 刷新中…
              </span>
            ) : null}
          </div>
        </div>
        <div className="page-header-actions">
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => setRefreshTick((t) => t + 1)}
            disabled={loading}
          >
            刷新
          </button>
        </div>
      </div>

      {/* ---------- 顶部共享预览区 ---------- */}
      {selected ? (
        <Card className="clips-preview">
          <div className="preview-head">
            <div className="preview-title">
              <span className="swatch" style={{ background: previewColor }} />
              <b>{selected.category_name}</b>
              <span className="preview-video" title={selected.video_filename}>
                {selected.video_filename}
              </span>
              <span className="preview-times mono">
                {formatTimeShort(selected.start_time)} – {formatTimeShort(selected.end_time)}
                <span className="preview-dur">（时长 {formatDuration(selected.end_time - selected.start_time)}）</span>
              </span>
              <ReviewStatusBadge value={selected.review_status} />
              <ClipStatusChip status={effectiveClipMediaStatus(selected.media_status, selected.clip_id)} />
              <ParticipantSummary mode={selected.category_participant_mode} roles={selected.role_definitions} assignments={selected.participant_roles} mouseIds={selected.mouse_ids} compact />
            </div>
            <div className="preview-actions">
              <Link
                className="btn btn-sm btn-primary"
                to={`/projects/${pid}/annotate/${selected.video_id}?t=${selected.start_time}`}
                title="回到行为标注工作台并定位到该标注区间起点"
              >
                跳转到行为标注 →
              </Link>
              <button type="button" className="btn btn-sm" onClick={() => setSelected(null)}>
                关闭预览
              </button>
            </div>
          </div>

          <div className="preview-body">
            <div className="preview-video-box">
              <video
                  ref={videoRef}
                  className={media.status === "ready" ? "" : "media-player-pending"}
                  muted={muted}
                  controls
                  playsInline
                  preload="metadata"
                  title="点击播放 / 暂停"
                  onClick={togglePlay}
                  onTimeUpdate={(e) => {
                    const v = e.currentTarget;
                    const s = selectedRef.current;
                    setCurrentTime(v.currentTime);
                    if (s && v.currentTime >= s.end_time && !v.paused) {
                      v.pause();
                      v.currentTime = s.end_time;
                      setPlaying(false);
                      setRangeMsg("已播放到片段结束，可点击「回到起点」重播");
                    }
                  }}
                  onPlay={() => {
                    setPlaying(true);
                    setRangeMsg(null);
                  }}
                  onPause={() => setPlaying(false)}
                />
              <MediaLoadProgress state={media} onCancel={media.cancel} />
              {media.status === "pending" || media.status === "failed" || media.status === "cancelled" ? <div className="media-status-overlay"><EmptyState compact title={media.status === "pending" ? "播放资源处理中" : media.status === "cancelled" ? "下载已取消" : "视频下载失败"} hint={media.message} /><button type="button" className="btn btn-sm" onClick={media.reload}>{media.status === "cancelled" ? "重新下载" : "重试"}</button></div> : null}
            </div>

            <div className="preview-controls">
              <button type="button" className="btn btn-sm" onClick={togglePlay} disabled={media.status !== "ready"}>
                {playing ? "⏸ 暂停" : "▶ 播放"}
              </button>
              <button type="button" className="btn btn-sm" onClick={jumpToStart} disabled={media.status !== "ready"}>
                ⟲ 回到起点
              </button>
              <button type="button" className="btn btn-sm" onClick={toggleMute} disabled={media.status !== "ready"}>
                {muted ? "🔇 静音" : "🔊 出声"}
              </button>
              <span className="time-display">
                <b>{formatTime(currentTime)}</b> / {duration > 0 ? formatTime(duration) : "?"}
              </span>
              <span className="flex-spacer" />
              <span className={`range-hint ${inRange ? "in" : ""}`} role="status">
                {rangeMsg ?? (inRange ? "标注区间内播放" : "标注区间外（自由浏览，到点自动停止）")}
              </span>
            </div>

            {duration > 0 ? (
              <ClipRangeBar
                duration={duration}
                start={selected.start_time}
                end={selected.end_time}
                currentTime={currentTime}
                color={previewColor}
                onSeek={seekTo}
              />
            ) : (
              <div className="frame-preview">暂无源视频时长信息，时间范围条不可用</div>
            )}
          </div>
        </Card>
      ) : (
        <Card className="clips-preview clips-preview-empty">
          <EmptyState
            compact
            title="选择片段预览"
            hint="点击下方任意片段，在预览区加载源视频并定位到对应标注区间；一次只播放一个"
          />
        </Card>
      )}

      {/* ---------- 筛选工具栏 ---------- */}
      <div className="clips-toolbar">
        <div className="chip-row" role="group" aria-label="按类别筛选">
          <button
            type="button"
            className={categoryFilter == null ? "chip active" : "chip"}
            onClick={() => {
              setCategoryFilter(null);
              setPage(1);
            }}
            aria-pressed={categoryFilter == null}
          >
            全部类别 <span className="chip-count">{total}</span>
          </button>
          {counts.map((cc) => (
            <button
              key={cc.category_id}
              type="button"
              className={categoryFilter === cc.category_id ? "chip active" : "chip"}
              onClick={() => {
                setCategoryFilter(cc.category_id);
                setPage(1);
              }}
              aria-pressed={categoryFilter === cc.category_id}
              title={`${cc.category_name}（${cc.count} 个片段）`}
            >
              <span
                className="swatch"
                style={{ background: categoryById.get(cc.category_id)?.color ?? "var(--text-3)" }}
              />
              {cc.category_name}
              <span className="chip-count">{cc.count}</span>
            </button>
          ))}
        </div>

        <div className="clips-filters">
          <input
            className="input search"
            type="search"
            value={query}
            maxLength={128}
            placeholder="按文件名 / 类别名搜索…"
            aria-label="按文件名或类别名搜索"
            onChange={(e) => {
              setQuery(e.target.value);
              setPage(1);
            }}
          />
          <select
            className="select video-select"
            value={videoFilter ?? ""}
            aria-label="按视频筛选"
            onChange={(e) => {
              setVideoFilter(e.target.value === "" ? null : Number(e.target.value));
              setPage(1);
            }}
          >
            <option value="">全部视频</option>
            {(videos ?? []).map((v) => (
              <option key={v.id} value={v.id}>
                {v.filename}（#{v.id}）
              </option>
            ))}
          </select>
          <span className="flex-spacer" />
          <span className="clips-note">
            {debouncedQuery
              ? `搜索「${debouncedQuery}」· 共 ${total} 条结果`
              : `每页 ${pageSize} 条 · 服务端分页`}
          </span>
        </div>
      </div>

      {/* ---------- 列表主体 ---------- */}
      {error ? (
        <ErrorBox message={error} />
      ) : items === null ? (
        <Loading />
      ) : emptyTotal && !filterActive ? (
        <Card>
          <EmptyState
            title="暂无片段"
            hint="视频审核通过后会自动生成视频片段，可前往审核工作台或行为标注工作台查看生成进度"
          />
        </Card>
      ) : (items ?? []).length === 0 ? (
        <Card>
          <EmptyState
            title="没有匹配的片段"
            hint="调整搜索关键词或类别 / 视频筛选后重试"
          />
        </Card>
      ) : (
        <div className="clip-grid">
          {(items ?? []).map((c) => (
            <button
              key={clipItemKey(c)}
              type="button"
              className={selected && clipItemKey(selected) === clipItemKey(c) ? "clip-card active" : "clip-card"}
              onClick={() => selectClip(c)}
              aria-pressed={selected ? clipItemKey(selected) === clipItemKey(c) : false}
              title={`${c.category_name} · ${c.video_filename} ${formatTimeShort(c.start_time)} – ${formatTimeShort(c.end_time)}`}
            >
              <span className="clip-thumb">
                <ClipThumb projectId={pid} clip={c} color={categoryById.get(c.category_id)?.color} />
                <span className="clip-thumb-tag mono">
                  {formatDuration(c.end_time - c.start_time)}
                </span>
              </span>
              <span className="clip-body">
                <span className="clip-cat">
                  <span
                    className="swatch"
                    style={{ background: categoryById.get(c.category_id)?.color ?? "var(--text-3)" }}
                  />
                  <span className="name">{c.category_name}</span>
                </span>
                <span className="clip-name" title={c.video_filename}>
                  {c.video_filename}
                </span>
                <span className="clip-times mono">
                  {formatTimeShort(c.start_time)} – {formatTimeShort(c.end_time)}
                  <span className="clip-frames">
                    {" "}
                    · 帧 {c.start_frame}→{c.end_frame}
                  </span>
                </span>
                <span className="clip-meta">
                  <ReviewStatusBadge value={c.review_status} />
                  <ClipStatusChip status={effectiveClipMediaStatus(c.media_status, c.clip_id)} />
                  <span className="clip-annotator" title={c.annotator_name ?? undefined}>
                    {c.annotator_name ?? "—"}
                  </span>
                </span>
                <span className="clip-created">{formatDate(c.created_at)}</span>
                <ParticipantSummary mode={c.category_participant_mode} roles={c.role_definitions} assignments={c.participant_roles} mouseIds={c.mouse_ids} compact />
              </span>
            </button>
          ))}
        </div>
      )}

      {/* ---------- 分页 ---------- */}
      {items !== null && total > 0 ? (
        <div className="pager">
          <span className="pager-info">
            共 {total} 条 · 第 {page} / {pages} 页
          </span>
          <select
            className="select"
            style={{ width: 110 }}
            value={pageSize}
            aria-label="每页条数"
            onChange={(e) => {
              setPageSize(Number(e.target.value));
              setPage(1);
            }}
          >
            {[20, 50, 100].map((n) => (
              <option key={n} value={n}>
                {n} 条/页
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn btn-sm"
            disabled={page <= 1 || loading}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            ← 上一页
          </button>
          <button
            type="button"
            className="btn btn-sm"
            disabled={page >= pages || loading}
            onClick={() => setPage((p) => Math.min(pages, p + 1))}
          >
            下一页 →
          </button>
        </div>
      ) : null}
    </div>
  );
}
