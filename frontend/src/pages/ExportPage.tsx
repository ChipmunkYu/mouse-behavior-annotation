/**
 * 导出工作台 /projects/:projectId/export（批次 6）：
 * - 统计摘要：可导出总数 / 就绪数 / 缺失数 + aria 进度条；缺失片段明细列表
 *   （缺失 = 视频片段尚未生成；导出任务的后台 worker 可在打包前补生成）
 * - 导出范围：按类别多选 chips（复用片段库 categories 计数接口 + 类别 API 取色），
 *   全部 = 不传 category_ids；「开始导出 ZIP」按钮仅 owner / admin 可见
 * - 导出内容预览：目标目录结构树 + annotations.json 字段摘要；本页只发起并监控
 *   后台导出任务，不在浏览器请求内同步调用 ffmpeg
 * - 导出任务：发起后轮询 export/status 直至任务落定（与媒体面板同规则，4s 一次），
 *   处理中显示 Job 进度 / 状态；成功提供下载入口 + 7 天保留提醒；409 冲突提示
 *   「上一个导出仍在进行中」
 * - 下载：与视频流同理用带 Bearer 的请求拉取 blob，文件名以 Content-Disposition 为准
 * - 1366×768：左右双列（统计/范围/任务 | 内容预览），窄屏自动堆叠为单列
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  createExport,
  fetchExportDownload,
  getExportStatus,
  listCategories,
  listClipCategories,
  listProjects,
} from "../api";
import { ApiError } from "../api/client";
import type {
  Category,
  ClipCategoryCount,
  ExportStatus,
  Job,
  Project,
} from "../api/types";
import { EXPORT_RETENTION_DAYS, JOB_LABELS, ROLE_LABELS } from "../api/types";
import { Card, EmptyState, Loading } from "../components/ui";
import { formatDate } from "../utils/format";

/** 导出任务轮询间隔（与媒体生成面板一致）。 */
const POLL_INTERVAL_MS = 4000;

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
  const percent = exportable_count > 0 ? Math.round((ready_count / exportable_count) * 100) : 0;

  let headline = "暂无审核通过的标注可导出";
  let tone = "muted";
  if (exportable_count > 0) {
    if (missing_count === 0) {
      headline = "全部视频片段已就绪，可导出完整包";
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
          <span className="export-stat-label">就绪视频片段（可打包）</span>
        </div>
        <div className={missing_count > 0 ? "export-stat danger" : "export-stat"}>
          <span className="export-stat-num mono">{missing_count}</span>
          <span className="export-stat-label">缺失视频片段（待生成）</span>
        </div>
      </div>

      <div className={`media-headline ${tone}`} role="status">
        {headline}
      </div>

      <div
        className="media-progress"
        style={{ marginTop: 8 }}
        role="progressbar"
        aria-label="导出就绪进度"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
      >
        <div className="media-progress-fill" style={{ width: `${percent}%` }} />
      </div>
      <div className="media-meta" style={{ marginTop: 6 }}>
        <span className="mono">{percent}% 就绪</span>
        <span>就绪视频片段占可导出行为标注的比例</span>
      </div>

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
            title={busy ? "已有导出任务进行中，请等待完成" : "在后台补齐并打包所选类别的审核通过视频片段与 annotations.json"}
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

/** annotations.json 字段摘要（与标注工作台单视频统一事件 JSON 格式一致）。 */
const ANNOTATIONS_JSON_FIELDS: Array<{ key: string; desc: string }> = [
  { key: "annotation_id", desc: "行为事件唯一 ID" },
  { key: "video_id", desc: "源视频 ID" },
  { key: "clip_file", desc: "对应视频片段在 ZIP 内的相对路径（必填）" },
  { key: "behavior", desc: "行为类别名称" },
  { key: "mouse_ids", desc: "参与对象对应的 track ID 数组" },
  { key: "start_time / end_time", desc: "起止时间（秒）" },
  { key: "start_frame / end_frame", desc: "起止帧号" },
  { key: "confidence", desc: "可信度" },
  { key: "review_status", desc: "标注审核状态（approved）" },
  { key: "annotator", desc: "标注者用户名" },
  { key: "reviewer", desc: "审核者用户名" },
  { key: "detection_import_revision", desc: "检测导入版本快照" },
  { key: "identity_revision", desc: "track 修正版本快照" },
  { key: "crop_region", desc: "空间标注区域（若有）" },
];

function PreviewCard({
  project,
  pid,
  status,
}: {
  project: Project | null;
  pid: number;
  status: ExportStatus | null;
}) {
  const rootName = (project?.name ?? `project-${pid}`).replace(/[\\/:*?"<>|]/g, "_");
  const ready = status?.ready_count ?? 0;
  const missing = status?.missing_count ?? 0;
  const exportable = status?.exportable_count ?? 0;

  const treeLines = [
    `${rootName}/`,
    `├── annotations.json             # ${exportable} 条审核通过的行为事件元数据`,
    `├── clips/                       # 按分组 / 类别组织的 ${ready} 个视频片段`,
    `│   └── {分组}/{类别}/clip_{annotation_id}.mp4`,
    `├── corrected_tracks/`,
    `│   ├── manifest.json`,
    `│   └── video_{id}/import_{revision}/identity_{revision}/`,
    `│       └── tracks.corrected.jsonl`,
    `└── README.txt                   # 导出说明与固定版本统计`,
  ];

  return (
    <Card title="导出内容预览">
      <pre className="export-tree mono" role="img" aria-label="导出 ZIP 目录结构预览">
        {treeLines.join("\n")}
      </pre>
      <div className="export-tree-note">
        <b>目标内容预览：</b>导出任务会打包审核通过的行为标注、视频片段与修正后 track 结果；
        {missing > 0 ? `后台 worker 可在打包前尝试补齐当前缺失的 ${missing} 个视频片段。` : "当前视频片段均已就绪。"}
        本页面只创建并监控任务，不会同步调用 ffmpeg。「{'{…}'}」为占位符，实际文件名由后端按行为标注生成。
      </div>

      <div className="export-fields-head">
        <span className="mono">annotations.json</span> 字段摘要
      </div>
      <div className="export-fields">
        {ANNOTATIONS_JSON_FIELDS.map((f) => (
          <div key={f.key}>
            <code>{f.key}</code>
            <span className="desc">{f.desc}</span>
          </div>
        ))}
      </div>
      <div className="export-tree-note">字段以统一行为事件 JSON 格式为准，与行为标注工作台的单视频导出保持一致。</div>
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
        <div className="media-head">
          <span className="media-loading">
            <span className="spinner" aria-hidden="true" /> 导出处理中…
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
          ✓ 导出完成：视频片段与 annotations.json 已打包为 ZIP。
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

  return (
    <>
      <div className="media-error" role="alert">
        ⚠ 导出{job.status === "failed" ? "失败" : "已取消"}
        {job.error ? `：${job.error}` : "，请稍后重试"}
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

  /* ---------- 状态与元数据加载 ---------- */
  const loadStatus = useCallback(
    async (silent = false) => {
      if (!silent) setLoading(true);
      try {
        const st = await getExportStatus(pid);
        setStatus(st);
        setStatusError(null);
      } catch (err) {
        if (!silent) {
          setStatusError(err instanceof Error ? err.message : "加载导出状态失败");
        }
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [pid]
  );

  const loadMeta = useCallback(async () => {
    try {
      const [projs, cnts, cats] = await Promise.all([
        listProjects(),
        listClipCategories(pid),
        listCategories(pid),
      ]);
      setProject(projs.find((p) => p.id === pid) ?? null);
      setCounts(cnts);
      setCategories(cats);
    } catch {
      // 元数据失败不阻塞状态加载；界面自动退化为无颜色 / 无计数
    }
  }, [pid]);

  useEffect(() => {
    setSelected([]);
    setExportError(null);
    setDownloadError(null);
    setNotice(null);
    void loadMeta();
    void loadStatus();
  }, [pid, loadMeta, loadStatus]);

  /* ---------- 导出中轮询（仅任务进行时，规则与媒体面板一致） ---------- */
  useEffect(() => {
    if (!busy) return;
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
  }, [busy, loadStatus]);

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
        category_ids: selected.length > 0 ? selected : undefined,
      });
      // 先用返回的 Job 立即渲染任务卡，轮询随后接管最新状态
      setStatus((prev) =>
        prev
          ? { ...prev, latest_job: job }
          : { latest_job: job, exportable_count: 0, ready_count: 0, missing_count: 0, missing_clips: [] }
      );
      setNotice("导出任务已发起，正在后台打包，请稍候…");
      void loadStatus(true);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setExportError("上一个导出仍在进行中，请等待其完成后再发起新的导出。");
        // 拉取一次状态：若后端已登记该任务，则自动接管进度与下载入口
        void loadStatus(true);
      } else {
        setExportError(err instanceof Error ? err.message : "发起导出失败");
      }
    } finally {
      setExporting(false);
    }
  }, [pid, exporting, busy, selected, loadStatus]);

  /* ---------- 下载导出包（Bearer blob，与视频流同理） ---------- */
  const handleDownload = useCallback(async () => {
    if (downloading) return;
    setDownloading(true);
    setDownloadError(null);
    try {
      const { blob, filename } = await fetchExportDownload(pid);
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
  }, [pid, downloading]);
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
          <PreviewCard project={project} pid={pid} status={status} />
        </div>
      </div>
    </div>
  );
}
