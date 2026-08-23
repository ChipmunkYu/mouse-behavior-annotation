import { useEffect, useRef, useState } from "react";
import type { UploadTaskPhase, UploadTaskStatus } from "./types";
import { useUploadManager } from "./UploadManagerContext";

const PHASE_LABEL: Record<UploadTaskPhase, string> = {
  queued: "等待上传", video: "上传视频", tracks: "上传 tracks", metadata: "上传 metadata",
  completing: "校验处理中", success: "上传成功", failed: "上传失败", cleanup: "清理批次",
};

const STATUS_CLASS: Record<UploadTaskStatus, string> = {
  queued: "muted", running: "active", success: "success", failed: "error", cancelling: "active",
};

export default function UploadTaskTray() {
  const { tasks, retry, cancel, confirm, confirmAllSuccess } = useUploadManager();
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const previousTaskIds = useRef<Set<string>>(new Set());
  const active = tasks.filter((task) => ["queued", "running", "cancelling"].includes(task.status)).length;
  const failed = tasks.filter((task) => task.status === "failed").length;
  const successes = tasks.filter((task) => task.status === "success").length;

  useEffect(() => {
    const currentTaskIds = new Set(tasks.map((task) => task.taskId));
    const hasNewTask = tasks.some((task) => !previousTaskIds.current.has(task.taskId));
    previousTaskIds.current = currentTaskIds;
    if (hasNewTask) setOpen(true);
  }, [tasks]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open]);

  return <div className="upload-tray-root">
    <button ref={triggerRef} type="button" className={`btn btn-sm upload-tray-trigger${tasks.length ? " has-tasks" : ""}${failed ? " has-failures" : ""}`} aria-expanded={open} aria-controls="upload-task-tray" onClick={() => setOpen((value) => !value)}>
      <span className="upload-trigger-icon" aria-hidden="true">↑</span>
      <span>上传任务栏</span>
      <span className="visually-hidden">，{active} 个活跃，{failed} 个失败，{successes} 个成功</span>
      {active ? <span aria-hidden="true" className="upload-count active">活跃 {active}</span> : null}
      {failed ? <span aria-hidden="true" className="upload-count failed">失败 {failed}</span> : null}
    </button>
    <div className="visually-hidden" role="status" aria-live="polite" aria-atomic="true">上传任务栏：{active} 个活跃，{failed} 个失败，{successes} 个成功</div>
    {open ? <aside id="upload-task-tray" className="upload-task-tray" aria-label="上传任务栏">
      <div className="upload-tray-head">
        <div><b>上传任务栏</b><span>{tasks.length ? `共 ${tasks.length} 项 · 切换页面不会中断` : "暂无上传任务"}</span></div>
        <div className="upload-tray-head-actions">
          {successes ? <button type="button" className="btn btn-sm" onClick={confirmAllSuccess}>一键确认成功（{successes}）</button> : null}
          <button type="button" className="btn btn-ghost btn-sm upload-tray-collapse" onClick={() => { setOpen(false); triggerRef.current?.focus(); }} aria-label="收起上传任务栏">收起</button>
        </div>
      </div>
      <div className="upload-task-list">
        {tasks.length === 0 ? <div className="upload-tray-empty"><b>还没有上传任务</b><span>任务加入队列后会自动展开并持续显示进度。</span></div> : tasks.map((task) => <article key={task.taskId} className={`upload-task-row ${STATUS_CLASS[task.status]}`} aria-label={`${task.name}，${PHASE_LABEL[task.phase]}`}>
          <div className="upload-task-title"><b title={task.name}>{task.kind === "batch" ? "三文件批次 · " : ""}{task.name}</b><span className="upload-task-phase">{PHASE_LABEL[task.phase]}</span></div>
          <div className="upload-task-project"><span>项目</span>{task.projectName}</div>
          {(task.status === "running" || task.status === "queued") ? <div className="upload-progress-line">
            <div className="upload-task-progress" role="progressbar" aria-label={`${task.name} 总上传进度`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={task.progress} aria-valuetext={`${task.progress}%`}><div style={{ width: `${task.progress}%` }} /></div>
            <b aria-hidden="true">{task.progress}%</b>
          </div> : null}
          {task.kind === "batch" && task.status === "running" && ["tracks", "metadata", "video"].includes(task.phase) ? <div className="upload-task-detail">当前文件 {task.slots?.[task.phase as "tracks" | "metadata" | "video"].percent ?? 0}% · 批次总进度 {task.progress}%</div> : null}
          {task.status === "cancelling" ? <div className="upload-task-detail" role="status">正在取消并清理服务端批次，请稍候…</div> : null}
          {task.error ? <div className="upload-task-error" role="alert">{task.error}</div> : null}
          <div className="upload-task-actions">
            {task.status === "success" ? <button type="button" className="btn btn-sm" onClick={() => confirm(task.taskId)}>确认</button> : null}
            {task.status === "failed" ? <><button type="button" className="btn btn-primary btn-sm" onClick={() => void retry(task.taskId)}>重试</button><button type="button" className="btn btn-danger btn-sm" onClick={() => void cancel(task.taskId)}>{task.kind === "batch" && task.batchId != null ? "取消并删除批次" : "取消"}</button></> : null}
            {["queued", "running"].includes(task.status) ? <button type="button" className="btn btn-danger btn-sm" onClick={() => void cancel(task.taskId)}>{task.kind === "batch" && task.batchId != null ? "取消并清理" : "取消"}</button> : null}
            {task.status === "cancelling" ? <span aria-hidden="true">清理中…</span> : null}
          </div>
        </article>)}
      </div>
    </aside> : null}
  </div>;
}
