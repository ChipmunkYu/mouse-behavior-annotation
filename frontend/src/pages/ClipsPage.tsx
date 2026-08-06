/**
 * 片段库 /projects/:projectId/clips：
 * - 从审核通过的标注派生的跨视频行为片段（来源：src/demo 数据层）
 * - 类别计数 + 类别 / 源视频 / 搜索筛选 + 分页
 * - 点击卡片在顶部共享预览区播放（一次只渲染一个，不批量加载视频）
 * - 可跳回源标注位置（带 ?t= 参数）
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { listCategories, listClips, listProjects } from "../api";
import type { Category, Project } from "../api/types";
import { Card, EmptyState, Loading, StatusBadge } from "../components/ui";
import DemoMouseStage from "../demo/DemoMouseStage";
import type { Clip } from "../demo/types";
import { DEMO_MODE } from "../demo/mode";
import { formatTimeShort } from "../utils/format";

const PAGE_SIZE = 12;

export default function ClipsPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const pid = Number(projectId);
  const navigate = useNavigate();

  const [project, setProject] = useState<Project | null>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  const [clips, setClips] = useState<Clip[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [categoryFilter, setCategoryFilter] = useState<number | "all">("all");
  const [videoFilter, setVideoFilter] = useState<string>("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);

  // 共享预览区：一次仅一个片段（不批量加载视频）
  const [selectedClip, setSelectedClip] = useState<Clip | null>(null);
  const [previewTime, setPreviewTime] = useState(0);
  const [previewPlaying, setPreviewPlaying] = useState(false);

  const load = useCallback(async () => {
    if (!pid) return;
    try {
      const [projs, cats, clipData] = await Promise.all([
        listProjects(),
        listCategories(pid),
        listClips(pid),
      ]);
      setProject(projs.find((p) => p.id === pid) ?? null);
      setCategories(cats);
      setClips(clipData);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载片段失败");
      setClips([]);
    }
  }, [pid]);

  useEffect(() => {
    void load();
  }, [load]);

  // 类别计数（全部片段）
  const categoryCounts = useMemo(() => {
    const map = new Map<number, number>();
    for (const c of clips ?? []) map.set(c.category_id, (map.get(c.category_id) ?? 0) + 1);
    return map;
  }, [clips]);

  const videoOptions = useMemo(() => {
    const set = new Set<string>((clips ?? []).map((c) => c.video_filename));
    return [...set].sort();
  }, [clips]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (clips ?? []).filter((c) => {
      if (categoryFilter !== "all" && c.category_id !== categoryFilter) return false;
      if (videoFilter && c.video_filename !== videoFilter) return false;
      if (q && !c.video_filename.toLowerCase().includes(q) && !c.category_name.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [clips, categoryFilter, videoFilter, query]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const pageItems = filtered.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  function changeFilter() {
    setPage(1);
    setSelectedClip(null);
    setPreviewPlaying(false);
  }

  function openClip(clip: Clip) {
    setSelectedClip(clip);
    setPreviewTime(0);
    setPreviewPlaying(false);
  }

  return (
    <div className="container">
      <div className="page-header">
        <div>
          <div style={{ fontSize: 12, color: "var(--text-3)", marginBottom: 2 }}>
            <Link to="/projects">项目</Link> / {project?.name ?? `#${pid}`} / 片段库
          </div>
          <h1>跨视频行为片段库</h1>
          <div className="sub">
            共 {clips?.length ?? 0} 个已通过片段 · 展示 {filtered.length} 个 · 仅收录审核通过的标注
          </div>
        </div>
        <div className="page-header-actions">
          <button type="button" className="btn btn-sm" onClick={() => void load()}>
            刷新
          </button>
          {selectedClip ? (
            <button
              type="button"
              className="btn btn-sm btn-primary"
              onClick={() => {
                navigate(`/projects/${pid}/annotate/${selectedClip.video_id}?t=${selectedClip.start_time.toFixed(1)}`);
                setPreviewPlaying(false);
              }}
            >
              跳到源标注位置 →
            </button>
          ) : null}
        </div>
      </div>

      {error ? <div className="error-box" role="alert">⚠ {error}</div> : null}
      {DEMO_MODE ? (
        <div className="muted-box" style={{ marginBottom: 12 }}>
          演示模式：片段由本地模拟数据中的「审核通过标注」实时派生；缩略图为 SVG 占位，未批量加载任何视频。
        </div>
      ) : null}

      {/* 类别计数 chips */}
      {clips && clips.length > 0 ? (
        <div className="chip-row" role="group" aria-label="按行为类别筛选">
          <button
            type="button"
            className={categoryFilter === "all" ? "chip active" : "chip"}
            onClick={() => {
              setCategoryFilter("all");
              changeFilter();
            }}
          >
            全部 <b>{clips.length}</b>
          </button>
          {[...categoryCounts.entries()]
            .sort((a, b) => a[0] - b[0])
            .map(([cid, count]) => {
              const cat = categories.find((c) => c.id === cid);
              return (
                <button
                  key={cid}
                  type="button"
                  className={categoryFilter === cid ? "chip active" : "chip"}
                  onClick={() => {
                    setCategoryFilter(cid);
                    changeFilter();
                  }}
                >
                  <span className="swatch" style={{ background: cat?.color ?? "var(--text-3)" }} aria-hidden="true" />
                  {cat?.name ?? `类别 #${cid}`} <b>{count}</b>
                </button>
              );
            })}
        </div>
      ) : null}

      {/* 筛选工具条 */}
      <div className="video-toolbar">
        <input
          className="input search"
          type="search"
          value={query}
          placeholder="按源视频 / 行为名称搜索…"
          onChange={(e) => {
            setQuery(e.target.value);
            changeFilter();
          }}
        />
        <select
          className="select"
          style={{ width: 180 }}
          value={videoFilter}
          onChange={(e) => {
            setVideoFilter(e.target.value);
            changeFilter();
          }}
        >
          <option value="">全部源视频</option>
          {videoOptions.map((v) => (
            <option key={v} value={v}>
              {v}
            </option>
          ))}
        </select>
        <select
          className="select"
          style={{ width: 150 }}
          value={categoryFilter === "all" ? "all" : String(categoryFilter)}
          onChange={(e) => {
            setCategoryFilter(e.target.value === "all" ? "all" : Number(e.target.value));
            changeFilter();
          }}
          aria-label="按类别筛选"
        >
          <option value="all">全部类别</option>
          {categories.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
        <span className="flex-spacer" />
      </div>

      {clips === null ? (
        <Loading />
      ) : filtered.length === 0 ? (
        <Card>
          <EmptyState
            title={clips.length === 0 ? "暂无已通过片段" : "没有匹配的片段"}
            hint={
              clips.length === 0
                ? "视频经审核「通过」后，其标注会自动进入片段库。"
                : "调整筛选条件或搜索关键词"
            }
          />
        </Card>
      ) : (
        <>
          {/* 共享预览区（一次仅渲染一个片段） */}
          {selectedClip ? (
            <Card className="clip-preview-card">
              <div className="card-header">
                <div className="card-title">
                  <span className="swatch" style={{ background: selectedClip.category_color ?? "var(--text-3)" }} aria-hidden="true" />
                  {selectedClip.category_name} · {selectedClip.video_filename}
                </div>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => {
                    setSelectedClip(null);
                    setPreviewPlaying(false);
                  }}
                  aria-label="关闭预览"
                >
                  ✕
                </button>
              </div>
              <div className="card-body">
                <div className="demo-stage-box clip">
                  <DemoMouseStage
                    duration={selectedClip.duration}
                    currentTime={previewTime}
                    playing={previewPlaying}
                    fps={30}
                    seed={selectedClip.video_id}
                    loop={{ start: 0, end: selectedClip.duration }}
                    badge="片段预览 · 演示画面"
                    onTimeUpdate={setPreviewTime}
                    onTogglePlay={() => setPreviewPlaying((p) => !p)}
                  />
                </div>
                <div className="clip-preview-meta">
                  <span>
                    源视频 <b>{selectedClip.video_filename}</b>
                  </span>
                  <span>
                    起止 <b className="mono">{formatTimeShort(selectedClip.start_time)} – {formatTimeShort(selectedClip.end_time)}</b>
                  </span>
                  <span>
                    时长 <b className="mono">{selectedClip.duration.toFixed(1)}s</b>
                  </span>
                  <span>
                    帧 <b className="mono">{selectedClip.start_frame} → {selectedClip.end_frame}</b>
                  </span>
                  <span>
                    标注者 <b>{selectedClip.annotator ?? "—"}</b>
                  </span>
                  <StatusBadge value={selectedClip.review_status} tone="ok" />
                </div>
              </div>
            </Card>
          ) : null}

          <div className="clip-grid">
            {pageItems.map((c) => (
              <button
                key={c.id}
                type="button"
                className={selectedClip?.id === c.id ? "clip-card active" : "clip-card"}
                onClick={() => openClip(c)}
                title="点击在顶部预览区播放该片段"
              >
                <img className="clip-thumb" src={c.thumb} alt="" aria-hidden="true" />
                <div className="clip-body">
                  <div className="clip-cat">
                    <span className="swatch" style={{ background: c.category_color ?? "var(--text-3)" }} aria-hidden="true" />
                    <span className="name" title={c.category_name}>
                      {c.category_name}
                    </span>
                    <StatusBadge value={c.review_status} tone="ok" />
                  </div>
                  <div className="clip-src" title={c.video_filename}>
                    {c.video_filename}
                  </div>
                  <div className="clip-meta">
                    <span className="mono">{formatTimeShort(c.start_time)} – {formatTimeShort(c.end_time)}</span>
                    <span>·</span>
                    <span className="mono">{c.duration.toFixed(1)}s</span>
                    <span>·</span>
                    <span>{c.annotator ?? "—"}</span>
                  </div>
                </div>
              </button>
            ))}
          </div>

          {/* 分页 */}
          <div className="pager">
            <span className="pager-info">
              共 <b>{filtered.length}</b> 个片段 · 第 <b>{safePage}</b> / {totalPages} 页
            </span>
            <div className="pager-buttons">
              <button type="button" className="btn btn-sm" disabled={safePage <= 1} onClick={() => setPage((p) => p - 1)}>
                ← 上一页
              </button>
              <button type="button" className="btn btn-sm" disabled={safePage >= totalPages} onClick={() => setPage((p) => p + 1)}>
                下一页 →
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
