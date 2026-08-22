import { useEffect, useRef, useState, type ChangeEvent, type DragEvent } from "react";
import { completeImportBatch, createImportBatch, listVideos, uploadBatchFile, uploadVideo } from "../api";
import type { AssigneeDirectoryItem, ImportFileRole, Video, VideoImportBatch } from "../api/types";
import { formatFileSize } from "../utils/format";

const VIDEO_ACCEPT = ".mp4,.mov,.avi,.mkv,.webm,.m4v,.wmv,.mpeg,.mpg";
const VIDEO_EXT = new Set(["mp4", "mov", "avi", "mkv", "webm", "m4v", "wmv", "mpeg", "mpg"]);
type Stage = "idle" | "uploading" | "success" | "error" | "cancelled";
type Slot = { file: File | null; stage: Stage; percent: number; error: string | null };
const emptySlot = (): Slot => ({ file: null, stage: "idle", percent: 0, error: null });
const STAGE_LABELS: Record<Stage, string> = {
  idle: "待上传",
  uploading: "上传中",
  success: "已上传",
  error: "上传失败",
  cancelled: "已取消",
};
const BATCH_STATUS_LABELS: Record<string, string> = {
  created: "已创建",
  uploading: "上传中",
  validating: "校验中",
  processing: "处理中",
  ready: "已就绪",
  video_only: "仅视频",
  completed: "已完成",
  failed: "校验失败",
};

interface Props {
  projectId: number;
  onUploaded: (video: Video) => void;
  onEnterAnnotation: (video: Video) => void;
  onClose: () => void;
  canManage: boolean;
  assignees: AssigneeDirectoryItem[];
}

function isVideo(file: File) {
  const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
  return VIDEO_EXT.has(ext);
}

export default function VideoUploadPanel({ projectId, onUploaded, onEnterAnnotation, onClose, canManage, assignees }: Props) {
  const [mode, setMode] = useState<"video" | "batch">("video");
  const [file, setFile] = useState<File | null>(null);
  const [stage, setStage] = useState<Stage>("idle");
  const [percent, setPercent] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [successVideo, setSuccessVideo] = useState<Video | null>(null);
  const [batch, setBatch] = useState<VideoImportBatch | null>(null);
  const [slots, setSlots] = useState<Record<ImportFileRole, Slot>>({ video: emptySlot(), tracks: emptySlot(), metadata: emptySlot() });
  const [batchBusy, setBatchBusy] = useState(false);
  const abortRef = useRef(new AbortController());
  const inputRef = useRef<HTMLInputElement>(null);
  const [assigneeId, setAssigneeId] = useState("");

  useEffect(() => () => abortRef.current.abort(), []);

  function chooseSingle(next: File | null) {
    if (!next) return;
    if (!isVideo(next)) { setError("不支持的视频格式"); return; }
    setFile(next); setStage("idle"); setError(null); setSuccessVideo(null);
  }

  async function startSingle() {
    if (!file) return;
    abortRef.current = new AbortController();
    setStage("uploading"); setError(null); setPercent(0);
    try {
      const video = await uploadVideo(projectId, file, { signal: abortRef.current.signal, onProgress: (p) => setPercent(p.percent), assigneeMembershipId: assigneeId ? Number(assigneeId) : null });
      setSuccessVideo(video); setStage("success"); onUploaded(video);
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") { setStage("cancelled"); setError("上传已取消，文件仍保留，可重试。"); }
      else { setStage("error"); setError(err instanceof Error ? err.message : "上传失败"); }
    }
  }

  function assign(role: ImportFileRole, next: File | null) {
    if (!next) return;
    if (role === "video" && !isVideo(next)) { setError("视频槽文件格式不受支持"); return; }
    if (role === "tracks" && next.name.toLowerCase() !== "tracks.jsonl") { setError("检测槽请选择 tracks.jsonl"); return; }
    if (role === "metadata" && next.name.toLowerCase() !== "metadata.json") { setError("元数据槽请选择 metadata.json"); return; }
    setSlots((prev) => ({ ...prev, [role]: { file: next, stage: "idle", percent: 0, error: null } }));
    setError(null);
  }

  function assignDropped(files: FileList) {
    Array.from(files).forEach((f) => {
      const lower = f.name.toLowerCase();
      if (lower === "tracks.jsonl") assign("tracks", f);
      else if (lower === "metadata.json") assign("metadata", f);
      else if (isVideo(f)) assign("video", f);
    });
  }

  async function uploadSlot(role: ImportFileRole, batchId: number) {
    const slot = slots[role];
    if (!slot.file) throw new Error(`${role} 文件尚未选择`);
    setSlots((p) => ({ ...p, [role]: { ...p[role], stage: "uploading", error: null } }));
    try {
      const updated = await uploadBatchFile(projectId, batchId, role, slot.file, {
        signal: abortRef.current.signal,
        onProgress: (p) => setSlots((old) => ({ ...old, [role]: { ...old[role], percent: p.percent } })),
      });
      setBatch(updated);
      setSlots((p) => ({ ...p, [role]: { ...p[role], stage: "success", percent: 100 } }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "上传失败";
      setSlots((p) => ({ ...p, [role]: { ...p[role], stage: "error", error: message } }));
      throw err;
    }
  }

  async function startBatch() {
    if (!slots.video.file || !slots.tracks.file || !slots.metadata.file) { setError("请先补齐三个文件槽"); return; }
    setBatchBusy(true); setError(null); abortRef.current = new AbortController();
    try {
      const created = batch ?? await createImportBatch(projectId);
      setBatch(created);
      for (const role of ["video", "tracks", "metadata"] as ImportFileRole[]) {
        if (slots[role].stage !== "success") await uploadSlot(role, created.id);
      }
      const completed = await completeImportBatch(projectId, created.id, assigneeId ? Number(assigneeId) : null);
      if (completed.status === "failed") {
        setError("文件配对校验失败，请修正文件并新建批次重试。");
        const ve = (completed as unknown as Record<string, unknown>).validation_errors as Record<string, unknown> ?? null;
        setBatch({ ...completed, validation_errors: ve } as VideoImportBatch);
        return;
      }
      setBatch(completed as VideoImportBatch);
      if (completed.created_video_id) {
        const videos = await listVideos(projectId);
        const video = videos.find((v) => v.id === completed.created_video_id);
        if (video) { setSuccessVideo(video); onUploaded(video); }
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      const apiErr = err as { status?: number; message?: string };
      if (apiErr.status === 400) {
        setError("批次校验失败或之前已失败，请新建批次重试。");
        return;
      }
      setError(err instanceof Error ? err.message : "批次上传失败");
    }
    finally { setBatchBusy(false); }
  }

  async function retrySlot(role: ImportFileRole) {
    if (!batch) { await startBatch(); return; }
    setBatchBusy(true); setError(null);
    try { await uploadSlot(role, batch.id); }
    catch { /* slot already displays the error */ }
    finally { setBatchBusy(false); }
  }

  return (
    <section className="card upload-panel" aria-label="上传视频">
      <div className="card-header"><div className="card-title">上传视频</div><button className="btn btn-ghost btn-sm" onClick={onClose}>✕</button></div>
      <div className="card-body">
        <div className="upload-mode-tabs" role="tablist">
          <button className={mode === "video" ? "active" : ""} onClick={() => setMode("video")}>仅视频</button>
          <button className={mode === "batch" ? "active" : ""} onClick={() => setMode("batch")}>三文件导入批次</button>
        </div>
        {canManage ? <div className="upload-assignee"><label htmlFor="upload-assignee">负责人</label><select id="upload-assignee" className="select" value={assigneeId} onChange={(e) => setAssigneeId(e.target.value)} disabled={stage === "uploading" || batchBusy}><option value="">未分配</option>{assignees.map((m) => <option key={m.membership_id} value={m.membership_id}>{m.username}</option>)}</select><span>上传完成时设置，之后可在视频库改派。</span></div> : null}
        {mode === "video" ? (
          <>
            <input ref={inputRef} className="visually-hidden" type="file" accept={VIDEO_ACCEPT} onChange={(e) => { chooseSingle(e.target.files?.[0] ?? null); e.target.value = ""; }} />
            <button className="dropzone" onClick={() => inputRef.current?.click()} onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); chooseSingle(e.dataTransfer.files[0]); }}>
              <span className="dropzone-icon">↑</span><span className="dropzone-title">{file ? file.name : "点击选择或拖放视频"}</span><span className="dropzone-hint">{file ? formatFileSize(file.size) : "支持 mp4 / mov / avi / mkv / webm 等格式"}</span>
            </button>
            {stage === "uploading" ? <Progress percent={percent} /> : null}
            <div className="upload-actions">
              {stage !== "success" ? <button className="btn btn-primary" disabled={!file || stage === "uploading"} onClick={() => void startSingle()}>{stage === "error" || stage === "cancelled" ? "重试" : "上传"}</button> : null}
              {stage === "uploading" ? <button className="btn btn-danger" onClick={() => abortRef.current.abort()}>取消</button> : null}
              {successVideo ? <button className="btn btn-primary" disabled={successVideo.status === "needs_transcode"} onClick={() => onEnterAnnotation(successVideo)}>进入行为标注 →</button> : null}
            </div>
          </>
        ) : (
          <>
            <div className="batch-drop" onDragOver={(e: DragEvent) => e.preventDefault()} onDrop={(e: DragEvent) => { e.preventDefault(); assignDropped(e.dataTransfer.files); }}>
              原始视频 + tracks.jsonl + metadata.json；可将三个文件一起拖到这里，系统会按文件名自动分配
            </div>
            <div className="batch-slots">
              {(["video", "tracks", "metadata"] as ImportFileRole[]).map((role) => <BatchSlot key={role} role={role} slot={slots[role]} busy={batchBusy} onChoose={(f) => assign(role, f)} onRetry={() => void retrySlot(role)} />)}
            </div>
            <div className="upload-actions">
              <button className="btn btn-primary" disabled={batchBusy || !slots.video.file || !slots.tracks.file || !slots.metadata.file} onClick={() => void startBatch()}>{batchBusy ? "上传与校验中…" : batch ? "继续并完成校验" : "开始批次上传"}</button>
              {successVideo ? <button className="btn" onClick={() => onEnterAnnotation(successVideo)}>进入行为标注 →</button> : null}
              {batch ? <span className={`batch-indicator ${batch.status}`}>{BATCH_STATUS_LABELS[batch.status] ?? "未知状态"}</span> : null}
            </div>
            {batch?.validation_errors ? <pre className="validation-errors">{JSON.stringify(batch.validation_errors, null, 2)}</pre> : null}
          </>
        )}
        {error ? <div className="error-box" role="alert">⚠ {error}</div> : null}
        {successVideo ? <div className="ok-box">✓ 已创建视频：{successVideo.filename}</div> : null}
      </div>
    </section>
  );
}

function Progress({ percent }: { percent: number }) {
  return <div className="upload-progress"><div className="upload-progress-track"><div className="upload-progress-fill" style={{ width: `${percent}%` }} /></div><div className="upload-progress-meta mono"><span>{percent}%</span></div></div>;
}

function BatchSlot({ role, slot, busy, onChoose, onRetry }: { role: ImportFileRole; slot: Slot; busy: boolean; onChoose: (file: File | null) => void; onRetry: () => void }) {
  const labels = { video: "原始视频", tracks: "tracks.jsonl", metadata: "metadata.json" };
  const accept = role === "video" ? VIDEO_ACCEPT : role === "tracks" ? ".jsonl" : ".json";
  function changed(e: ChangeEvent<HTMLInputElement>) { onChoose(e.target.files?.[0] ?? null); e.target.value = ""; }
  return <div className={`batch-slot ${slot.stage}`}>
    <div className="batch-slot-head"><b>{labels[role]}</b><span className="batch-status-dot" title={STAGE_LABELS[slot.stage]} aria-label={STAGE_LABELS[slot.stage]} /></div>
    {slot.file ? <><div className="upload-file-name">{slot.file.name}</div><div className="upload-file-size mono">{formatFileSize(slot.file.size)}</div>{slot.stage === "uploading" || slot.stage === "success" ? <Progress percent={slot.percent} /> : null}</> : <div className="upload-note">尚未选择</div>}
    <div className="batch-slot-actions"><label className="btn btn-sm">{slot.file ? "更换" : "选择"}<input className="visually-hidden" type="file" accept={accept} disabled={busy} onChange={changed} /></label>{slot.stage === "error" ? <button className="btn btn-sm" onClick={onRetry}>重试</button> : null}</div>
    {slot.error ? <div className="slot-error">{slot.error}</div> : null}
  </div>;
}
