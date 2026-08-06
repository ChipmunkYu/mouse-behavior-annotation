/**
 * 导出中心 /projects/:projectId/export：
 * - 选择导出范围（全部已通过片段 / 按行为类别）
 * - 预览将生成的目录结构与 annotations.json 摘要
 * - 模拟后台导出任务：进度 → 完成 / 失败；任务保留 7 天提示
 * - 「下载演示包」明确禁用：不声称真实 ffmpeg 已执行
 * 演示模式：任务由本地定时器推进；真实后端尚未接入。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { createExportTask, getExportPreview, listCategories, listExportTasks, listProjects } from "../api";
import type { Category, Project } from "../api/types";
import { Card, EmptyState, Loading, StatusBadge } from "../components/ui";
import type { ExportPreview, ExportScope, ExportTask } from "../demo/types";
import { DEMO_MODE } from "../demo/mode";
import { formatDate } from "../utils/format";

export default function ExportPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const pid = Number(projectId);

  const [project, setProject] = useState<Project | null>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  const [preview, setPreview] = useState<ExportPreview | null>(null);
  const [tasks, setTasks] = useState<ExportTask[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const [scopeCategory, setScopeCategory] = useState<number | "all">("all");
  const [startedId, setStartedId] = useState<number | null>(null);

  const scope: ExportScope = useMemo(
    () => ({ category_id: scopeCategory === "all" ? null : scopeCategory, approved_only: true }),
    [scopeCategory]
  );

  const loadPreview = useCallback(async () => {
    if (!pid) return;
    try {
      setPreview(await getExportPreview(pid, scope));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载导出预览失败");
    }
  }, [pid, scope]);

  const loadTasks = useCallback(async () => {
    try {
      setTasks(await listExportTasks());
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载导出任务失败");
    }
  }, []);

  useEffect(() => {
    if (!pid) return;
    let alive = true;
    Promise.all([listProjects(), listCategories(pid)])
      .then(([projs, cats]) => {
        if (!alive) return;
        setProject(projs.find((p) => p.id === pid) ?? null);
        setCategories(cats);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "加载数据失败"));
    void loadPreview();
    void loadTasks();
    return () => {
      alive = false;
    };
  }, [pid, loadPreview, loadTasks]);

  // 轮询任务进度（模拟后台任务持续推进）
  useEffect(() => {
    const iv = window.setInterval(() => {
      void loadTasks();
    }, 700);
    return () => window.clearInterval(iv);
  }, [loadTasks]);

  async function handleStart() {
    setCreating(true);
    setError(null);
    try {
      const task = await createExportTask(pid, scope);
      setStartedId(task.id);
      await loadTasks();
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建导出任务失败");
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="container">
      <div className="page-header">
        <div>
          <div style={{ fontSize: 12, color: "var(--text-3)", marginBottom: 2 }}>
            <Link to="/projects">项目</Link> / {project?.name ?? `#${pid}`} / 导出中心
          </div>
          <h1>分类导出中心</h1>
          <div className="sub">按行为类别导出审核通过的片段，生成目录 + annotations.json</div>
        </div>
        <span className="retention-chip" title="导出目录与文件在服务器保留 7 天，到期自动清理（演示模式为界面提示）">
          ⏳ 保留 7 天
        </span>
      </div>

      {DEMO_MODE ? (
        <div className="muted-box" style={{ marginBottom: 12 }}>
          演示模式：导出任务进度与目录结构为本地模拟，<b>不会真正运行 ffmpeg，也不会生成真实文件</b>。
        </div>
      ) : null}
      {error ? <div className="error-box" role="alert">⚠ {error}</div> : null}

      <div className="export-grid">
        {/* 范围选择 + 预览 */}
        <section className="export-left">
          <Card title="导出范围">
            <div className="card-body">
              <div className="field">
                <label htmlFor="export-category">行为类别</label>
                <select
                  id="export-category"
                  className="select"
                  value={scopeCategory === "all" ? "all" : String(scopeCategory)}
                  onChange={(e) => setScopeCategory(e.target.value === "all" ? "all" : Number(e.target.value))}
                >
                  <option value="all">全部已通过片段（14 类可用）</option>
                  {categories.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.group} / {c.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>片段范围</label>
                <div className="radio-row">
                  <label className="radio-label">
                    <input type="radio" checked={scope.approved_only} readOnly />
                    仅审核通过片段
                  </label>
                </div>
                <div className="frame-preview">
                  摘要：将导出 <b>{preview?.clip_count ?? "—"}</b> 个片段，覆盖{" "}
                  <b>{preview?.video_count ?? "—"}</b> 个视频，合计约{" "}
                  <b>{(preview?.total_seconds ?? 0).toFixed(0)}s</b>
                </div>
              </div>
              <div className="actions">
                <button type="button" className="btn btn-primary" disabled={creating || (preview?.clip_count ?? 0) === 0} onClick={() => void handleStart()}>
                  {creating ? "创建中…" : "开始导出（演示）"}
                </button>
              </div>
            </div>
          </Card>

          <Card title="将生成的目录结构">
            <div className="card-body">
              {preview ? (
                <>
                  <pre className="export-tree" aria-label="导出目录结构预览">{preview.tree}</pre>
                  <div className="export-note">├── annotations.json 摘要：</div>
                  <table className="mini-table">
                    <thead>
                      <tr>
                        <th>行为</th>
                        <th>片段数</th>
                      </tr>
                    </thead>
                    <tbody>
                      {preview.by_category.map((c) => (
                        <tr key={c.category_id}>
                          <td>
                            <span className="swatch" style={{ background: c.color ?? "var(--text-3)" }} aria-hidden="true" />
                            {c.name}
                          </td>
                          <td className="mono">{c.count}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {preview.summary.sample.length > 0 ? (
                    <div className="export-note" style={{ marginTop: 8 }}>
                      事件样例（前 {preview.summary.sample.length} 条）：
                    </div>
                  ) : null}
                  {preview.summary.sample.map((ev, i) => (
                    <pre key={i} className="export-sample mono">
                      {JSON.stringify(ev)}
                    </pre>
                  ))}
                </>
              ) : (
                <Loading text="生成预览…" />
              )}
            </div>
          </Card>
        </section>

        {/* 导出任务列表 */}
        <section className="export-right">
          <Card
            title={`导出任务（${tasks?.length ?? 0}）`}
            extra={
              <button type="button" className="btn btn-sm" onClick={() => void loadTasks()}>
                刷新
              </button>
            }
          >
            <div className="card-body">
              {tasks === null ? (
                <Loading text="加载任务…" />
              ) : tasks.length === 0 ? (
                <EmptyState compact title="暂无导出任务" hint="在上方选择范围后点击「开始导出」" />
              ) : (
                <div className="export-task-list">
                  {tasks.map((t) => (
                    <div key={t.id} className="export-task">
                      <div className="export-task-top">
                        <span className="export-task-name" title={t.name}>
                          {t.name}
                        </span>
                        <StatusBadge value={t.status} />
                      </div>
                      <div className="export-task-meta">
                        <span>任务 #{t.id}</span>
                        <span>·</span>
                        <span className="mono">{t.clip_count} 个片段</span>
                        <span>·</span>
                        <span>{t.status === "running" ? `进度 ${t.progress}%` : formatDate(t.created_at)}</span>
                      </div>
                      {t.status === "running" ? (
                        <div
                          className="upload-progress"
                          role="progressbar"
                          aria-valuemin={0}
                          aria-valuemax={100}
                          aria-valuenow={t.progress}
                          aria-label={`导出任务进度 ${t.progress}%`}
                        >
                          <div className="upload-progress-track">
                            <div className="upload-progress-fill" style={{ width: `${t.progress}%` }} />
                          </div>
                        </div>
                      ) : null}
                      {t.status === "failed" && t.error ? (
                        <div className="error-box" role="alert">{t.error}</div>
                      ) : null}
                      {t.status === "completed" ? (
                        <div className="export-task-foot">
                          <span className="export-note">完成于 {formatDate(t.completed_at ?? t.created_at)}</span>
                          <button
                            type="button"
                            className="btn btn-sm"
                            disabled
                            title="演示模式不提供真实下载；后端接入后将从服务器导出目录下载（保留 7 天）"
                          >
                            下载演示包（仅演示）
                          </button>
                        </div>
                      ) : null}
                      {startedId === t.id && t.status === "running" ? (
                        <div className="muted-box" style={{ marginTop: 6 }}>
                          任务已创建，正在模拟后台处理…（演示环境不会真正运行 ffmpeg）
                        </div>
                      ) : null}
                      <div className="export-retain mono">保留至 {formatDate(t.expires_at)}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </Card>
        </section>
      </div>
    </div>
  );
}
