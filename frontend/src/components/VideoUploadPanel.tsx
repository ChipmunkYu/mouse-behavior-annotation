import { useRef, useState, type ChangeEvent, type DragEvent } from "react";
import type { AssigneeDirectoryItem, ImportFileRole } from "../api/types";
import { useUploadManager } from "../upload/UploadManagerContext";
import { formatFileSize } from "../utils/format";

const VIDEO_ACCEPT = ".mp4,.mov,.avi,.mkv,.webm,.m4v,.wmv,.mpeg,.mpg";
const VIDEO_EXT = new Set(["mp4", "mov", "avi", "mkv", "webm", "m4v", "wmv", "mpeg", "mpg"]);
const EMPTY_SLOTS: Record<ImportFileRole, File | null> = { video: null, tracks: null, metadata: null, annotation: null };

export function droppedBatchFiles(files: File[], heuristic: boolean, current: Record<ImportFileRole, File | null>): Record<ImportFileRole, File | null> {
  const next = { ...current };
  for (const file of files) {
    const name = file.name.toLowerCase();
    const role: ImportFileRole | null = name === (heuristic ? "tracks.json" : "tracks.jsonl") ? "tracks"
      : name === "metadata.json" ? "metadata" : heuristic && name === "annotation.json" ? "annotation"
      : (heuristic ? name === "clip.mp4" : isVideo(file)) ? "video" : null;
    if (!role) throw new Error(`无法分槽：${file.name}`);
    if (next[role]) throw new Error(`重复文件槽：${role}，请分别添加每个事件，不会覆盖已选文件`);
    next[role] = file;
  }
  return next;
}

interface Props {
  projectId: number;
  projectName: string;
  canManage: boolean;
  assignees: AssigneeDirectoryItem[];
}

function isVideo(file: File) {
  return VIDEO_EXT.has(file.name.split(".").pop()?.toLowerCase() ?? "");
}

export default function VideoUploadPanel({ projectId, projectName, canManage, assignees }: Props) {
  const { enqueueVideo, enqueueBatch } = useUploadManager();
  const [mode, setMode] = useState<"video" | "batch" | "heuristic">("batch");
  const [file, setFile] = useState<File | null>(null);
  const [slots, setSlots] = useState<Record<ImportFileRole, File | null>>(EMPTY_SLOTS);
  const [assigneeId, setAssigneeId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const adding = useRef(false);
  const [queueBusy, setQueueBusy] = useState(false);

  function changeMode(next: typeof mode) {
    if (mode === next || adding.current) return;
    setMode(next); setSlots(EMPTY_SLOTS); setError(null); setNotice(null);
  }

  function chooseSingle(next: File | null) {
    if (!next) return;
    if (!isVideo(next)) { setError("不支持的视频格式"); return; }
    setFile(next); setError(null); setNotice(null);
  }

  function assign(role: ImportFileRole, next: File | null) {
    if (adding.current) return;
    if (!next) return;
    if (role === "video" && !isVideo(next)) { setError("视频槽文件格式不受支持"); return; }
    if (mode === "heuristic" && next.name.toLowerCase() !== ({ video: "clip.mp4", tracks: "tracks.json", metadata: "metadata.json", annotation: "annotation.json" })[role]) { setError(`请选择该槽对应的四文件：${role}`); return; }
    if (role === "tracks" && next.name.toLowerCase() !== (mode === "heuristic" ? "tracks.json" : "tracks.jsonl")) { setError("检测槽文件名不符"); return; }
    if (role === "metadata" && next.name.toLowerCase() !== "metadata.json") { setError("元数据槽请选择 metadata.json"); return; }
    setSlots((current) => ({ ...current, [role]: next })); setError(null); setNotice(null);
  }

  function assignDropped(files: FileList) {
    if (adding.current) return;
    try { setSlots(droppedBatchFiles(Array.from(files), mode === "heuristic", slots)); setError(null); setNotice(null); }
    catch (error) { setError(error instanceof Error ? error.message : "文件分槽失败"); }
  }

  function queueSingle() {
    if (!file) return;
    enqueueVideo({ projectId, projectName, file, assigneeMembershipId: assigneeId ? Number(assigneeId) : null });
    setFile(null); setNotice("已加入上传队列，可继续添加文件。"); setError(null);
  }

  async function queueBatch() {
    if (adding.current) return;
    if (!slots.video || !slots.tracks || !slots.metadata) { setError("请先补齐三个文件槽"); return; }
    if (mode === "heuristic" && !slots.annotation) { setError("请补齐 annotation.json"); return; }
    adding.current = true; setQueueBusy(true);
    try {
    let displayName: string | undefined;
    if (mode === "heuristic" && slots.annotation && slots.annotation.size <= 1024 * 1024) {
      try {
        const evidence: unknown = JSON.parse(await slots.annotation.text());
        if (evidence && typeof evidence === "object" && "event_id" in evidence && typeof evidence.event_id === "string") displayName = evidence.event_id.slice(0, 160);
      } catch { /* 展示名不是导入 authority；坏 JSON 交由单项预验报告。 */ }
    }
    enqueueBatch({
      projectId, projectName, assigneeMembershipId: assigneeId ? Number(assigneeId) : null,
      source: mode === "heuristic" ? "heuristic" : "manual", displayName,
      files: { video: slots.video, tracks: slots.tracks, metadata: slots.metadata, ...(mode === "heuristic" && slots.annotation ? { annotation: slots.annotation } : {}) },
    });
    setSlots(EMPTY_SLOTS); setNotice("批次已加入队列，可继续添加下一个事件。"); setError(null);
    } finally { adding.current = false; setQueueBusy(false); }
  }

  return <section className="card upload-panel" aria-label="上传面板">
    <div className="card-body">
      <div className="upload-mode-tabs" role="tablist" aria-label="上传方式">
        <button type="button" role="tab" disabled={queueBusy} aria-selected={mode === "video"} className={mode === "video" ? "active" : ""} onClick={() => changeMode("video")}>仅视频</button>
        <button type="button" role="tab" disabled={queueBusy} aria-selected={mode === "batch"} className={mode === "batch" ? "active" : ""} onClick={() => changeMode("batch")}>三文件导入批次</button>
        <button type="button" role="tab" disabled={queueBusy} aria-selected={mode === "heuristic"} className={mode === "heuristic" ? "active" : ""} onClick={() => changeMode("heuristic")}>启发式结果</button>
      </div>
      {canManage ? <div className="upload-assignee"><label htmlFor="upload-assignee">负责人</label><select id="upload-assignee" className="select" value={assigneeId} onChange={(event) => setAssigneeId(event.target.value)}><option value="">未分配</option>{assignees.map((member) => <option key={member.membership_id} value={member.membership_id}>{member.username}</option>)}</select><span>任务完成时设置，之后可在视频库改派。</span></div> : null}
      {mode === "video" ? <div role="tabpanel">
        <input ref={inputRef} className="visually-hidden" type="file" accept={VIDEO_ACCEPT} onChange={(event) => { chooseSingle(event.target.files?.[0] ?? null); event.target.value = ""; }} />
        <button type="button" className="dropzone" onClick={() => inputRef.current?.click()} onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); chooseSingle(event.dataTransfer.files[0]); }}>
          <span className="dropzone-icon">↑</span><span className="dropzone-title">{file ? file.name : "点击选择或拖放视频"}</span><span className="dropzone-hint">{file ? formatFileSize(file.size) : "支持 mp4 / mov / avi / mkv / webm 等格式"}</span>
        </button>
        <div className="upload-actions"><button type="button" className="btn btn-primary" disabled={!file} onClick={queueSingle}>加入上传队列</button></div>
      </div> : <div role="tabpanel">
        <div className="batch-drop" onDragOver={(event: DragEvent) => event.preventDefault()} onDrop={(event: DragEvent) => { event.preventDefault(); assignDropped(event.dataTransfer.files); }}>{mode === "heuristic" ? "一个事件：clip.mp4 + annotation.json + tracks.json + metadata.json；四文件一起拖入或分别选择。加入队列后继续添加下一事件。" : "原始视频 + tracks.jsonl + metadata.json；可将三个文件一起拖到这里，系统会按文件名自动分配"}</div>
        <div className="batch-slots">{(mode === "heuristic" ? ["tracks", "metadata", "annotation", "video"] as ImportFileRole[] : ["tracks", "metadata", "video"] as ImportFileRole[]).map((role) => <BatchFileSlot key={`${mode}-${role}`} role={role} heuristic={mode === "heuristic"} file={slots[role]} onChoose={(next) => assign(role, next)} />)}</div>
        <div className="upload-actions"><button type="button" className="btn btn-primary" disabled={queueBusy || !slots.video || !slots.tracks || !slots.metadata || (mode === "heuristic" && !slots.annotation)} onClick={() => void queueBatch()}>{queueBusy ? "加入中…" : "加入上传队列"}</button></div>
      </div>}
      {error ? <div className="error-box" role="alert">⚠ {error}</div> : null}{notice ? <div className="ok-box" role="status">✓ {notice}</div> : null}
    </div>
  </section>;
}

function BatchFileSlot({ role, file, onChoose, heuristic }: { role: ImportFileRole; file: File | null; heuristic: boolean; onChoose: (file: File | null) => void }) {
  const labels = { tracks: heuristic ? "tracks.json" : "tracks.jsonl", metadata: "metadata.json", annotation: "annotation.json", video: heuristic ? "clip.mp4" : "原始视频" };
  const accept = role === "video" ? (heuristic ? ".mp4" : VIDEO_ACCEPT) : role === "tracks" && !heuristic ? ".jsonl" : ".json";
  function changed(event: ChangeEvent<HTMLInputElement>) { onChoose(event.target.files?.[0] ?? null); event.target.value = ""; }
  return <div className="batch-slot"><div className="batch-slot-head"><b>{labels[role]}</b></div>{file ? <><div className="upload-file-name">{file.name}</div><div className="upload-file-size mono">{formatFileSize(file.size)}</div></> : <div className="upload-note">尚未选择</div>}<div className="batch-slot-actions"><label className="btn btn-sm">{file ? "更换" : "选择"}<input className="visually-hidden" type="file" accept={accept} onChange={changed} /></label></div></div>;
}
