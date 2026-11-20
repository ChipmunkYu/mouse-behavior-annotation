/**
 * 导出工作台 /projects/:projectId/export（批次 6）：
 * - 统计摘要：可导出总数 / 已生成 / 缺失；独立显示当前驻留素材数，不作单调进度条
 *   （缺失 = 视频片段尚未生成；导出任务的后台 worker 可在打包前补生成）
 * - 导出范围：按类别多选 chips（复用片段库 categories 计数接口 + 类别 API 取色），
 *   全部 = 不传 category_ids；「开始导出 ZIP」按钮仅 owner / admin 可见
 * - 导出内容预览：每独立片段目录的四文件结构；本页只发起并监控
 *   后台导出任务，不在浏览器请求内同步调用 ffmpeg
 * - 导出任务：发起后轮询 export/status 直至任务落定（与媒体面板同规则，4s 一次），
 *   处理中显示 Job 进度 / 状态；成功提供下载入口 + 7 天保留提醒；409 冲突提示
 *   「上一个导出仍在进行中」
 * - 下载：与视频流同理用带 Bearer 的请求拉取 blob，文件名以 Content-Disposition 为准
 * - 1366×768：左右双列（统计/范围/任务 | 内容预览），窄屏自动堆叠为单列
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  createExport,
  fetchExportDownload,
  getExportStatus,
  listCategories,
  listClipCategories,
  listProjects,
  type PreparedExportStatus as ExportStatus,
  type ExportJob,
} from "../api";
import { ApiError } from "../api/client";
import type {
  Category,
  ClipCategoryCount,
  Job,
  Project,
  SourceScope,
} from "../api/types";
import { EXPORT_RETENTION_DAYS, JOB_LABELS, ROLE_LABELS } from "../api/types";
import { Card, EmptyState, Loading } from "../components/ui";
import { formatDate } from "../utils/format";

/** 导出任务轮询间隔（与媒体生成面板一致）。 */
const POLL_INTERVAL_MS = 4000;

const PHASE_LABELS: Record<NonNullable<ExportJob["phase"]>, string> = {
  queued: "等待导出", building: "准备素材与打包", validating: "校验压缩包",
  publishing: "发布导出包", cleanup: "清理临时文件", completed: "导出完成",
  failed: "导出失败", cancelled: "导出已取消",
};

function jobFailureMessage(job: ExportJob): string {
  if (job.error_code === "storage_insufficient") return "存储空间不足，请联系管理员释放空间后重试。";
  if (job.error_code === "cleanup_incomplete") return "临时文件清理未完成，请联系管理员处理后重试。";
  return "处理未完成，请重试；如仍失败，请联系管理员并提供任务 ID。";
}

function PreparationSummary({ preparation }: { preparation: ExportStatus["preparation"] }) {
  if (!preparation) return <p className="export-tree-note">当前服务未提供素材准备状态；仍可发起导出。</p>;
  const labels = {
    empty: "暂无待准备素材", pending: "等待后台准备", preparing: "正在后台准备",
    partial: preparation.ready_count > 0 ? "部分素材已就绪" : "素材将在导出时准备", ready: "当前素材已就绪", failed: "素材准备失败",
  };
  const waiting = Math.max(0, preparation.total_count - preparation.ready_count - preparation.failed_count - preparation.deferred_count);
  return (
    <section className="export-preparation" aria-label="导出素材准备状态">
      <div className={`media-headline ${preparation.state === "failed" ? "warn" : preparation.state === "ready" ? "ok" : "muted"}`} role="status">
        {labels[preparation.state]}
      </div>
      <dl className="export-preparation-counts">
        <div><dt>已就绪素材</dt><dd className="mono">{preparation.ready_count} / {preparation.total_count}</dd></div>
        <div><dt>待准备</dt><dd className="mono">{waiting}</dd></div>
        <div><dt>容量延后</dt><dd className="mono">{preparation.deferred_count}</dd></div>
        <div><dt>准备失败</dt><dd className="mono">{preparation.failed_count}</dd></div>
      </dl>
      {preparation.deferred_count > 0 ? <p className="export-tree-note">部分素材因缓存容量限制延后准备，导出时会尝试按需生成。</p> : null}
      {preparation.failed_count > 0 || preparation.state === "failed" ? <p className="export-tree-note">部分素材准备失败，导出时会重试；若导出仍失败，请联系管理员。</p> : null}
      <p className="export-tree-note">此处统计当前驻留的导出素材，不等同于视频片段数。缓存可能被回收，数量会变化；无需等待全部就绪即可导出。</p>
    </section>
  );
}

/** 任务是否仍在进行（决定是否继续轮询）。 */
function isJobBusy(job: Job | null | undefined): boolean {
  return job != null && (job.status === "queued" || job.status === "running");
}

/* ================= 统计摘要 ================= */

function SummaryCard({
  status,
  colorByCategoryName,
}: {
  status: ExportStatus | null;
  colorByCategoryName: Map<string, string>;
}) {
  if (status == null) {
    return (
      <Card title="导出统计">
        <Loading text="加载导出统计…" />
      </Card>
    );
  }

  const { exportable_count, ready_count, missing_count, missing_clips } = status;

  let headline = "暂无审核通过的标注可导出";
  let tone = "muted";
  if (exportable_count > 0) {
    if (missing_count === 0) {
      headline = "视频片段已生成；导出素材准备状态见下方";
      tone = "ok";
    } else {
      headline = `部分视频片段待生成（${missing_count}），导出任务将在后台尝试补齐`;
      tone = "warn";
    }
  }

  return (
    <Card title="导出统计">
      <div className="export-stats">
        <div className="export-stat total">
          <span className="export-stat-num mono">{exportable_count}</span>
          <span className="export-stat-label">可导出总数（审核通过的行为标注）</span>
        </div>
        <div className="export-stat ok">
          <span className="export-stat-num mono">{ready_count}</span>
          <span className="export-stat-label">已生成视频片段</span>
        </div>
        <div className={missing_count > 0 ? "export-stat danger" : "export-stat"}>
          <span className="export-stat-num mono">{missing_count}</span>
          <span className="export-stat-label">缺失视频片段（待生成）</span>
        </div>
      </div>

      <div className={`media-headline ${tone}`} role="status">
        {headline}
      </div>

      <PreparationSummary preparation={status.preparation} />

      {missing_count > 0 && missing_clips.length > 0 ? (
        <div className="export-missing">
          <div className="export-missing-head">缺失视频片段（{missing_count}）</div>
          <div className="export-missing-list" aria-label="缺失视频片段列表">
            {missing_clips.map((m) => (
              <div key={m.annotation_id} className="export-missing-row">
                <span
                  className="swatch"
                  style={{ background: colorByCategoryName.get(m.category_name) ?? "var(--text-3)" }}
                />
                <span className="name" title={m.category_name}>
                  {m.category_name}
                </span>
                <span className="video" title={m.video_filename}>
                  {m.video_filename}
                </span>
                <span className="aid mono">行为标注 #{m.annotation_id}</span>
              </div>
            ))}
          </div>
          <div className="export-missing-hint">
            这些视频片段尚未生成。发起导出后，后台任务会尝试补齐缺失的审核通过视频片段；
            本页面只创建并监控任务，不会同步调用 ffmpeg。
          </div>
        </div>
      ) : null}
    </Card>
  );
}

/* ================= 导出范围 ================= */

function ScopeCard({
  project,
  counts,
  categoryById,
  selected,
  onToggle,
  onSelectAll,
  exporting,
  busy,
  onExport,
  exportError,
}: {
  project: Project | null;
  counts: ClipCategoryCount[];
  categoryById: Map<number, Category>;
  selected: number[];
  onToggle: (id: number) => void;
  onSelectAll: () => void;
  exporting: boolean;
  busy: boolean;
  onExport: () => void;
  exportError: string | null;
}) {
  const canExport = project != null && (project.role === "owner" || project.role === "admin");
  const selectedCount = counts
    .filter((c) => selected.includes(c.category_id))
    .reduce((sum, c) => sum + c.count, 0);
  const scopeLabel =
    selected.length === 0
      ? "全部类别"
      : `已选 ${selected.length} 个类别（覆盖 ${selectedCount} 条行为标注）`;

  return (
    <Card
      title="导出范围"
      extra={<span className="export-scope-tag">{scopeLabel}</span>}
    >
      <div className="chip-row" role="group" aria-label="按类别选择导出范围">
        <button
          type="button"
          className={selected.length === 0 ? "chip active" : "chip"}
          onClick={onSelectAll}
          aria-pressed={selected.length === 0}
          title="导出全部类别"
        >
          全部类别
        </button>
        {counts.map((cc) => {
          const active = selected.includes(cc.category_id);
          return (
            <button
              key={cc.category_id}
              type="button"
              className={active ? "chip active" : "chip"}
              onClick={() => onToggle(cc.category_id)}
              aria-pressed={active}
              title={`${cc.category_name}：${cc.count} 条审核通过的行为标注，点击切换是否导出`}
            >
              <span
                className="swatch"
                style={{ background: categoryById.get(cc.category_id)?.color ?? "var(--text-3)" }}
              />
              {cc.category_name}
              <span className="chip-count">{cc.count}</span>
            </button>
          );
        })}
      </div>

      <div className="export-actions">
        {canExport ? (
          <button
            type="button"
            className="btn btn-primary"
            disabled={exporting || busy}
            onClick={onExport}
            title={busy ? "已有导出任务进行中，请等待完成" : "在后台准备并打包所选类别的审核通过片段，无需等待素材缓存就绪"}
          >
            {exporting ? "发起中…" : busy ? "导出进行中…" : "开始导出 ZIP"}
          </button>
        ) : (
          <span className="export-role-note">
            仅项目所有者和管理员可发起导出；你当前角色为「
            {project ? (ROLE_LABELS[project.role] ?? "未知角色") : "—"}」。
          </span>
        )}
        {busy ? (
          <span className="export-role-note">上一个导出仍在进行中，请等待其完成。</span>
        ) : null}
      </div>

      {exportError ? (
        <div className="error-box" style={{ marginTop: 8 }} role="alert">
          ⚠ {exportError}
        </div>
      ) : null}
    </Card>
  );
}

/* ================= 导出内容预览 ================= */

const EXPORT_FILES: Array<{ key: string; desc: string }> = [
  { key: "clip.mp4", desc: "独立片段视频" },
  { key: "annotation.json", desc: "该片段的行为标注" },
  { key: "tracks.json", desc: "该片段的轨迹数据" },
  { key: "metadata.json", desc: "该片段的来源与版本信息" },
];

function PreviewCard({
  project,
  pid,
}: {
  project: Project | null;
  pid: number;
}) {
  const rootName = (project?.name ?? `project-${pid}`).replace(/[\\/:*?"<>|]/g, "_");

  const treeLines = [
    `${rootName}/`,
    `└── {独立片段目录}/`,
    `    ├── clip.mp4`,
    `    ├── annotation.json`,
    `    ├── tracks.json`,
    `    └── metadata.json`,
  ];

  return (
    <Card title="导出内容预览">
      <pre className="export-tree mono" role="img" aria-label="导出 ZIP 目录结构预览">
        {treeLines.join("\n")}
      </pre>
      <div className="export-tree-note">
        每个独立片段目录包含以下四个文件。导出时复用可用素材，缺失素材按需准备。
        「{'{…}'}」为占位符，实际目录名由系统生成。
      </div>

      <div className="export-fields-head">
        每个片段的文件
      </div>
      <div className="export-fields">
        {EXPORT_FILES.map((f) => (
          <div key={f.key}>
            <code>{f.key}</code>
            <span className="desc">{f.desc}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}

/* ================= 导出任务 ================= */

function JobPanel({
  status,
  pid,
  downloading,
  downloadError,
  onDownload,
}: {
  status: ExportStatus | null;
  pid: number;
  downloading: boolean;
  downloadError: string | null;
  onDownload: () => void;
}) {
  if (status == null) {
    return (
      <>
        <Loading text="加载导出任务…" />
        {downloadError ? (
          <div className="error-box" style={{ marginTop: 8 }} role="alert">
            ⚠ {downloadError}
          </div>
        ) : null}
      </>
    );
  }
  const job = status.latest_job;
  if (job == null) {
    return (
      <>
        <EmptyState
          compact
          title="暂无导出记录"
          hint="选择导出范围后点击「开始导出 ZIP」，任务进度与下载入口将在这里显示"
        />
        {downloadError ? (
          <div className="error-box" style={{ marginTop: 8 }} role="alert">
            ⚠ {downloadError}
          </div>
        ) : null}
      </>
    );
  }

  if (job.status === "queued" || job.status === "running") {
    return (
      <>
        <div className="media-head" role="status">
          <span className="media-loading">
            <span className="spinner" aria-hidden="true" /> {job.phase ? PHASE_LABELS[job.phase] ?? "导出处理中…" : "导出处理中…"}
          </span>
          <span className="flex-spacer" />
          <span className={`media-job ${job.status}`}>
            <span className="dot" aria-hidden="true" />
            {JOB_LABELS[job.status] ?? job.status}
          </span>
        </div>
        <div
          className="media-progress"
          role="progressbar"
          aria-label="导出任务进度"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={job.progress}
        >
          <div className="media-progress-fill" style={{ width: `${job.progress}%` }} />
        </div>
        <div className="media-meta">
          <span className="mono">{job.progress}%</span>
          <span>任务在后台执行，离开本页面不会中断；完成后可返回此处下载</span>
        </div>
      </>
    );
  }

  if (job.status === "succeeded") {
    const expiry =
      job.expires_at != null
        ? `服务器保留至 ${formatDate(job.expires_at)}（约 ${EXPORT_RETENTION_DAYS} 天）`
        : `服务器保留 ${EXPORT_RETENTION_DAYS} 天`;
    return (
      <>
        <div className="ok-box" role="status">
          ✓ 导出完成：片段视频、行为标注、轨迹与来源信息已打包为 ZIP。
        </div>
        <div className="export-download-actions">
          <button
            type="button"
            className="btn btn-primary"
            disabled={downloading}
            onClick={onDownload}
          >
            {downloading ? "下载中…" : "下载导出 ZIP"}
          </button>
        </div>
        {downloadError ? (
          <div className="error-box" style={{ marginTop: 8 }} role="alert">
            ⚠ {downloadError}
          </div>
        ) : null}
        <div className="export-expiry">
          <span aria-hidden="true">⏳</span>
          <span>
            下载文件名以后端下发的 Content-Disposition 为准（通常为{" "}
            <code className="mono">project-{String(pid)}-export-*.zip</code>）；导出包{expiry}，
            过期后需重新发起导出，请及时下载保存。
          </span>
        </div>
      </>
    );
  }

  if (job.status === "failed") {
    return (
      <>
        <div className="media-error" role="alert">
          <div>⚠ 导出失败：{jobFailureMessage(job)}</div>
          <div className="media-meta">
            <span>任务 <span className="mono">#{job.id}</span></span>
            <span>阶段：{job.phase ? PHASE_LABELS[job.phase] ?? "导出失败" : "导出失败"}</span>
            <span>进度 <span className="mono">{job.progress}%</span></span>
            <span>创建于 {formatDate(job.created_at)}</span>
            {job.started_at ? <span>开始于 {formatDate(job.started_at)}</span> : null}
            {job.finished_at ? <span>结束于 {formatDate(job.finished_at)}</span> : null}
          </div>
          <div>请将任务 ID 和错误信息提供给管理员排查。</div>
        </div>
        {downloadError ? (
          <div className="error-box" style={{ marginTop: 8 }} role="alert">
            ⚠ {downloadError}
          </div>
        ) : null}
      </>
    );
  }

  return (
    <>
      <div className="media-error" role="alert">
        ⚠ 导出已取消
      </div>
      {downloadError ? (
        <div className="error-box" style={{ marginTop: 8 }} role="alert">
          ⚠ {downloadError}
        </div>
      ) : null}
    </>
  );
}

/* ================= 主页面 ================= */

export default function ExportPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const pid = Number(projectId);

  const [project, setProject] = useState<Project | null>(null);
  const [source, setSource] = useState<SourceScope>("manual");
  const scopeRef = useRef("");
  scopeRef.current = `${pid}:${source}`;
  const [categories, setCategories] = useState<Category[]>([]);
  const [counts, setCounts] = useState<ClipCategoryCount[]>([]);
  const [status, setStatus] = useState<ExportStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [statusError, setStatusError] = useState<string | null>(null);

  const [selected, setSelected] = useState<number[]>([]);
  const [exporting, setExporting] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const categoryById = useMemo(
    () => new Map(categories.map((c) => [c.id, c] as const)),
    [categories]
  );
  const colorByCategoryName = useMemo(() => {
    const m = new Map<string, string>();
    for (const c of categories) if (c.color) m.set(c.name, c.color);
    return m;
  }, [categories]);

  const busy = isJobBusy(status?.latest_job);
  const preparationBusy = status?.preparation?.state === "pending" || status?.preparation?.state === "preparing";
  const polling = busy || preparationBusy;

  /* ---------- 状态与元数据加载 ---------- */
  const loadStatus = useCallback(
    async (silent = false) => {
      const requestedScope = `${pid}:${source}`;
      if (!silent) setLoading(true);
      try {
        const st = await getExportStatus(pid, source);
        if (scopeRef.current !== requestedScope) return;
        if ((st.source ?? "manual") !== source) throw new ApiError(409, "导出来源与当前选择不一致，请刷新；不会提供其他来源的下载。");
        setStatus(st);
        setStatusError(null);
      } catch (err) {
        if (scopeRef.current !== requestedScope) return;
        if (!silent) {
          setStatusError("加载导出状态失败，请刷新重试；请确认当前导出来源。");
        }
      } finally {
        if (!silent && scopeRef.current === requestedScope) setLoading(false);
      }
    },
    [pid, source]
  );

  const loadMeta = useCallback(async () => {
    const requestedScope = `${pid}:${source}`;
    try {
      const [projs, cnts, cats] = await Promise.all([
        listProjects(),
        listClipCategories(pid, source),
        listCategories(pid),
      ]);
      if (scopeRef.current !== requestedScope) return;
      setProject(projs.find((p) => p.id === pid) ?? null);
      setCounts(cnts);
      setCategories(cats);
    } catch {
      // 元数据失败不阻塞状态加载；界面自动退化为无颜色 / 无计数
    }
  }, [pid, source]);

  useEffect(() => {
    setSelected([]);
    setStatus(null);
    setExportError(null);
    setDownloadError(null);
    setNotice(null);
    void loadMeta();
    void loadStatus();
  }, [pid, loadMeta, loadStatus]);

  /* ---------- 仅导出或素材准备活动期间轮询 ---------- */
  useEffect(() => {
    if (!polling) return;
    let alive = true;
    let timer: number | undefined;
    const tick = async () => {
      try {
        await loadStatus(true);
      } catch {
        // 轮询失败静默保留旧数据，下一次 tick 再试
      }
      if (!alive) return;
      timer = window.setTimeout(tick, POLL_INTERVAL_MS);
    };
    timer = window.setTimeout(tick, POLL_INTERVAL_MS);
    return () => {
      alive = false;
      if (timer != null) window.clearTimeout(timer);
    };
  }, [polling, loadStatus]);

  /* ---------- 范围选择 ---------- */
  const toggleCategory = useCallback((id: number) => {
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  }, []);
  const selectAll = useCallback(() => setSelected([]), []);

  /* ---------- 发起导出 ---------- */
  const handleExport = useCallback(async () => {
    if (exporting || busy) return;
    setExporting(true);
    setExportError(null);
    setNotice(null);
    try {
      const job = await createExport(pid, {
        source,
        category_ids: selected.length > 0 ? selected : undefined,
      });
      // 先用返回的 Job 立即渲染任务卡，轮询随后接管最新状态
      setStatus((prev) =>
        prev
          ? { ...prev, latest_job: job, source, category_ids: selected }
          : { latest_job: job, source, category_ids: selected, exportable_count: 0, ready_count: 0, missing_count: 0, missing_clips: [] }
      );
      setNotice("导出任务已发起，后台将准备素材并打包。");
      void loadStatus(true);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setExportError("上一个导出仍在进行中，请等待其完成后再发起新的导出。");
        // 拉取一次状态：若后端已登记该任务，则自动接管进度与下载入口
        void loadStatus(true);
      } else {
        setExportError("发起导出失败，请重试；如仍失败，请联系管理员。");
      }
    } finally {
      setExporting(false);
    }
  }, [pid, exporting, busy, selected, loadStatus, source]);

  /* ---------- 下载导出包（Bearer blob，与视频流同理） ---------- */
  const handleDownload = useCallback(async () => {
    if (downloading) return;
    setDownloading(true);
    setDownloadError(null);
    try {
      const { blob, filename } = await fetchExportDownload(pid, source);
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      // 延迟回收 object URL，确保下载请求已完成携带 blob
      window.setTimeout(() => URL.revokeObjectURL(link.href), 1000);
      setNotice(`已开始下载「${filename}」，导出包 7 天内有效，请及时保存。`);
    } catch (err) {
      setDownloadError(err instanceof Error ? err.message : "下载导出包失败");
    } finally {
      setDownloading(false);
    }
  }, [pid, downloading, source]);
  return (
    <div className="container export-page">
      <div className="page-header">
        <div>
          <div style={{ fontSize: 12, color: "var(--text-3)", marginBottom: 2 }}>
            <Link to="/projects">项目</Link> / {project?.name ?? `#${pid}`}
          </div>
          <h1>导出</h1>
          <div className="sub">
            后台补齐并打包审核通过的行为标注与视频片段；仅项目所有者和管理员可发起，导出包保留 7 天
          </div>
        </div>
        <div className="page-header-actions">
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => void loadStatus()}
            disabled={loading}
          >
            刷新
          </button>
        </div>
      </div>

      {notice ? (
        <div className="ok-box" role="status">✓ {notice}</div>
      ) : null}
      {statusError ? <div className="error-box" role="alert">⚠ {statusError}</div> : null}
      <label className="field">导出来源<select className="select" aria-label="导出来源" value={source} disabled={exporting || downloading} onChange={(event) => { setStatus(null); setSource(event.target.value as SourceScope); }}><option value="manual">人工</option><option value="heuristic">启发式</option><option value="all">全部</option></select><span>默认仅人工；选择全部才会混合。统计、任务与下载使用同一来源范围。</span></label>

      <div className="export-body">
        <div className="export-col">
          <SummaryCard status={status} colorByCategoryName={colorByCategoryName} />
          <ScopeCard
            project={project}
            counts={counts}
            categoryById={categoryById}
            selected={selected}
            onToggle={toggleCategory}
            onSelectAll={selectAll}
            exporting={exporting}
            busy={busy}
            onExport={() => void handleExport()}
            exportError={exportError}
          />
          <Card title="导出任务">
            {status?.latest_job ? <p className="field-hint">此任务来源：{({ manual: "人工", heuristic: "启发式", all: "全部" })[status.source ?? "manual"]} · 类别：{status.category_ids?.length ? status.category_ids.map((id) => categoryById.get(id)?.name ?? `#${id}`).join("、") : "全部类别"}</p> : null}
            <JobPanel
              status={status}
              pid={pid}
              downloading={downloading}
              downloadError={downloadError}
              onDownload={() => void handleDownload()}
            />
          </Card>
        </div>
        <div className="export-col">
          <PreviewCard project={project} pid={pid} />
        </div>
      </div>
    </div>
  );
}
