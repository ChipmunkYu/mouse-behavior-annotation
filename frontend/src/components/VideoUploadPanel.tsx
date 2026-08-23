import { useRef, useState, type ChangeEvent, type DragEvent } from "react";
import type { AssigneeDirectoryItem, ImportFileRole } from "../api/types";
import { useUploadManager } from "../upload/UploadManagerContext";
import { formatFileSize } from "../utils/format";

const VIDEO_ACCEPT = ".mp4,.mov,.avi,.mkv,.webm,.m4v,.wmv,.mpeg,.mpg";
const VIDEO_EXT = new Set(["mp4", "mov", "avi", "mkv", "webm", "m4v", "wmv", "mpeg", "mpg"]);
const EMPTY_SLOTS: Record<ImportFileRole, File | null> = { video: null, tracks: null, metadata: null };

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
  const [mode, setMode] = useState<"video" | "batch">("batch");
  const [file, setFile] = useState<File | null>(null);
  const [slots, setSlots] = useState<Record<ImportFileRole, File | null>>(EMPTY_SLOTS);
  const [assigneeId, setAssigneeId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  function chooseSingle(next: File | null) {
    if (!next) return;
    if (!isVideo(next)) { setError("不支持的视频格式"); return; }
    setFile(next); setError(null); setNotice(null);
  }

  function assign(role: ImportFileRole, next: File | null) {
    if (!next) return;
    if (role === "video" && !isVideo(next)) { setError("视频槽文件格式不受支持"); return; }
    if (role === "tracks" && next.name.toLowerCase() !== "tracks.jsonl") { setError("检测槽请选择 tracks.jsonl"); return; }
    if (role === "metadata" && next.name.toLowerCase() !== "metadata.json") { setError("元数据槽请选择 metadata.json"); return; }
    setSlots((current) => ({ ...current, [role]: next })); setError(null); setNotice(null);
  }

  function assignDropped(files: FileList) {
    Array.from(files).forEach((next) => {
      const name = next.name.toLowerCase();
      if (name === "tracks.jsonl") assign("tracks", next);
      else if (name === "metadata.json") assign("metadata", next);
      else if (isVideo(next)) assign("video", next);
    });
  }

  function queueSingle() {
    if (!file) return;
    enqueueVideo({ projectId, projectName, file, assigneeMembershipId: assigneeId ? Number(assigneeId) : null });
    setFile(null); setNotice("已加入上传队列，可继续添加文件。"); setError(null);
  }

  function queueBatch() {
    if (!slots.video || !slots.tracks || !slots.metadata) { setError("请先补齐三个文件槽"); return; }
    enqueueBatch({
      projectId, projectName, assigneeMembershipId: assigneeId ? Number(assigneeId) : null,
      files: { video: slots.video, tracks: slots.tracks, metadata: slots.metadata },
    });
    setSlots(EMPTY_SLOTS); setNotice("三文件批次已加入队列，可继续添加。"); setError(null);
  }

  return <section className="card upload-panel" aria-label="上传面板">
    <div className="card-body">
      <div className="upload-mode-tabs" role="tablist" aria-label="上传方式">
        <button type="button" role="tab" aria-selected={mode === "video"} className={mode === "video" ? "active" : ""} onClick={() => setMode("video")}>仅视频</button>
        <button type="button" role="tab" aria-selected={mode === "batch"} className={mode === "batch" ? "active" : ""} onClick={() => setMode("batch")}>三文件导入批次</button>
      </div>
      {canManage ? <div className="upload-assignee"><label htmlFor="upload-assignee">负责人</label><select id="upload-assignee" className="select" value={assigneeId} onChange={(event) => setAssigneeId(event.target.value)}><option value="">未分配</option>{assignees.map((member) => <option key={member.membership_id} value={member.membership_id}>{member.username}</option>)}</select><span>任务完成时设置，之后可在视频库改派。</span></div> : null}
      {mode === "video" ? <div role="tabpanel">
        <input ref={inputRef} className="visually-hidden" type="file" accept={VIDEO_ACCEPT} onChange={(event) => { chooseSingle(event.target.files?.[0] ?? null); event.target.value = ""; }} />
        <button type="button" className="dropzone" onClick={() => inputRef.current?.click()} onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); chooseSingle(event.dataTransfer.files[0]); }}>
          <span className="dropzone-icon">↑</span><span className="dropzone-title">{file ? file.name : "点击选择或拖放视频"}</span><span className="dropzone-hint">{file ? formatFileSize(file.size) : "支持 mp4 / mov / avi / mkv / webm 等格式"}</span>
        </button>
        <div className="upload-actions"><button type="button" className="btn btn-primary" disabled={!file} onClick={queueSingle}>加入上传队列</button></div>
      </div> : <div role="tabpanel">
        <div className="batch-drop" onDragOver={(event: DragEvent) => event.preventDefault()} onDrop={(event: DragEvent) => { event.preventDefault(); assignDropped(event.dataTransfer.files); }}>原始视频 + tracks.jsonl + metadata.json；可将三个文件一起拖到这里，系统会按文件名自动分配</div>
        <div className="batch-slots">{(["tracks", "metadata", "video"] as ImportFileRole[]).map((role) => <BatchFileSlot key={role} role={role} file={slots[role]} onChoose={(next) => assign(role, next)} />)}</div>
        <div className="upload-actions"><button type="button" className="btn btn-primary" disabled={!slots.video || !slots.tracks || !slots.metadata} onClick={queueBatch}>加入上传队列</button></div>
      </div>}
      {error ? <div className="error-box" role="alert">⚠ {error}</div> : null}{notice ? <div className="ok-box" role="status">✓ {notice}</div> : null}
    </div>
  </section>;
}

function BatchFileSlot({ role, file, onChoose }: { role: ImportFileRole; file: File | null; onChoose: (file: File | null) => void }) {
  const labels = { tracks: "tracks.jsonl", metadata: "metadata.json", video: "原始视频" };
  const accept = role === "video" ? VIDEO_ACCEPT : role === "tracks" ? ".jsonl" : ".json";
  function changed(event: ChangeEvent<HTMLInputElement>) { onChoose(event.target.files?.[0] ?? null); event.target.value = ""; }
  return <div className="batch-slot"><div className="batch-slot-head"><b>{labels[role]}</b></div>{file ? <><div className="upload-file-name">{file.name}</div><div className="upload-file-size mono">{formatFileSize(file.size)}</div></> : <div className="upload-note">尚未选择</div>}<div className="batch-slot-actions"><label className="btn btn-sm">{file ? "更换" : "选择"}<input className="visually-hidden" type="file" accept={accept} onChange={changed} /></label></div></div>;
}
