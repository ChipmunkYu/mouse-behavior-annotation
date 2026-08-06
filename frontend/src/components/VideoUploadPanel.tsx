/**
 * 视频上传面板（紧凑对话区）：
 * - 主操作：选择 / 拖放视频文件 → 上传（真实进度）→ 成功刷新列表并可进入标注
 * - 不设前端大小上限，明确受服务器磁盘空间约束
 * - 支持取消（xhr.abort）；取消后保留文件可重试或移除
 * - 失败友好提示：507 磁盘不足走专属文案，其余提取后端 detail
 * - 键盘可达：dropzone 为原生按钮；面板挂载时聚焦、关闭时焦点回到触发元素
 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
} from "react";
import { uploadVideo } from "../api";
import type { Video } from "../api/types";
import { formatFileSize } from "../utils/format";

const ACCEPT = ".mp4,.mov,.avi,.mkv,.webm,.m4v,.wmv,.mpeg,.mpg";
const ACCEPTED_EXT = new Set(["mp4", "mov", "avi", "mkv", "webm", "m4v", "wmv", "mpeg", "mpg"]);

type Stage = "idle" | "uploading" | "success" | "error" | "cancelled";

interface VideoUploadPanelProps {
  projectId: number;
  /** 上传成功（拿到 201 Video）后回调，用于刷新视频列表 */
  onUploaded: (video: Video) => void;
  /** 成功后可进入标注（needs_transcode 时不会触发） */
  onEnterAnnotation: (video: Video) => void;
  onClose: () => void;
}

function isAccepted(file: File): boolean {
  const dot = file.name.lastIndexOf(".");
  if (dot < 0) return false;
  return ACCEPTED_EXT.has(file.name.slice(dot + 1).toLowerCase());
}

function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

export default function VideoUploadPanel({
  projectId,
  onUploaded,
  onEnterAnnotation,
  onClose,
}: VideoUploadPanelProps) {
  const [file, setFile] = useState<File | null>(null);
  const [stage, setStage] = useState<Stage>("idle");
  const [progress, setProgress] = useState<{ loaded: number; total: number; percent: number } | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [successVideo, setSuccessVideo] = useState<Video | null>(null);
  const [dropActive, setDropActive] = useState(false);

  const inputRef = useRef<HTMLInputElement>(null);
  const dropzoneRef = useRef<HTMLButtonElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  /* 挂载：记录触发元素；关闭（卸载）：中断上传、焦点归还。 */
  useEffect(() => {
    mountedRef.current = true;
    restoreFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dropzoneRef.current?.focus();
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
      restoreFocusRef.current?.focus();
    };
  }, []);

  const closePanel = useCallback(() => {
    abortRef.current?.abort();
    onClose();
  }, [onClose]);

  /* Escape 关闭面板（上传中会同时取消）。 */
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") closePanel();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [closePanel]);

  const reset = useCallback(() => {
    setFile(null);
    setStage("idle");
    setProgress(null);
    setErrorMsg(null);
    setSuccessVideo(null);
    setDropActive(false);
  }, []);

  function pickFile(f: File | null | undefined) {
    if (!f) return;
    if (!isAccepted(f)) {
      setErrorMsg(`不支持的文件类型：${f.name}。仅支持 mp4 / mov / avi / mkv / webm / m4v / wmv / mpeg / mpg`);
      return;
    }
    setFile(f);
    setStage("idle");
    setProgress(null);
    setErrorMsg(null);
    setSuccessVideo(null);
  }

  function handleInputChange(e: ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0] ?? null;
    e.target.value = ""; // 允许再次选择同一文件
    pickFile(f);
  }

  function handleDrop(e: DragEvent) {
    e.preventDefault();
    setDropActive(false);
    if (stage === "uploading") return; // 上传中不允许更换文件
    pickFile(e.dataTransfer.files?.[0] ?? null);
  }

  async function startUpload() {
    if (!file) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setStage("uploading");
    setErrorMsg(null);
    setProgress({ loaded: 0, total: file.size, percent: 0 });
    try {
      const video = await uploadVideo(projectId, file, {
        signal: controller.signal,
        onProgress: (p) => {
          if (mountedRef.current) setProgress(p);
        },
      });
      if (!mountedRef.current) return;
      setSuccessVideo(video);
      setStage("success");
      setProgress(null);
      onUploaded(video);
    } catch (err) {
      if (!mountedRef.current) return;
      if (isAbortError(err)) {
        setStage("cancelled");
        setErrorMsg("上传已取消，文件仍保留，可重新上传或移除。");
      } else {
        setStage("error");
        setErrorMsg(err instanceof Error ? err.message : "上传失败，请重试");
      }
    } finally {
      abortRef.current = null;
    }
  }

  function cancelUpload() {
    abortRef.current?.abort();
  }

  const uploading = stage === "uploading";
  const percent = progress?.percent ?? 0;

  return (
    <section className="card upload-panel" aria-label="上传视频">
      <div className="card-header">
        <div className="card-title">上传视频</div>
        <button type="button" className="btn btn-ghost btn-sm" onClick={closePanel} aria-label="关闭上传面板">
          ✕
        </button>
      </div>
      <div className="card-body">
        {/* 隐藏的文件输入始终挂载，供 dropzone 与「更换文件」共用 */}
        <input
          ref={inputRef}
          type="file"
          className="visually-hidden"
          accept={ACCEPT}
          onChange={handleInputChange}
          tabIndex={-1}
          aria-hidden="true"
        />

        {!file ? (
          <button
            ref={dropzoneRef}
            type="button"
            className={dropActive ? "dropzone dragging" : "dropzone"}
            onClick={() => inputRef.current?.click()}
            onDragOver={(e) => {
              e.preventDefault();
              setDropActive(true);
            }}
            onDragLeave={() => setDropActive(false)}
            onDrop={handleDrop}
          >
            <span className="dropzone-icon" aria-hidden="true">
              ↑
            </span>
            <span className="dropzone-title">点击选择或拖放视频文件</span>
            <span className="dropzone-hint">支持 mp4 / mov / avi / mkv / webm / m4v / wmv / mpeg / mpg</span>
            <span className="dropzone-hint">不设大小上限，实际受服务器磁盘空间约束</span>
          </button>
        ) : (
          <div className="upload-file-row">
            <span className="upload-file-icon" aria-hidden="true">
              ▣
            </span>
            <div className="upload-file-info">
              <div className="upload-file-name" title={file.name}>
                {file.name}
              </div>
              <div className="upload-file-size mono">{formatFileSize(file.size)}</div>
            </div>
            {!uploading ? (
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => inputRef.current?.click()}
                disabled={uploading}
              >
                更换
              </button>
            ) : null}
          </div>
        )}

        {/* 进度条 */}
        {stage === "uploading" && progress ? (
          <div
            className="upload-progress"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
            aria-label={`上传进度 ${percent}%`}
          >
            <div className="upload-progress-track">
              <div className="upload-progress-fill" style={{ width: `${percent}%` }} />
            </div>
            <div className="upload-progress-meta mono">
              <span>{percent}%</span>
              <span>
                {formatFileSize(progress.loaded)} / {formatFileSize(progress.total)}
              </span>
            </div>
          </div>
        ) : null}

        {/* 状态信息 */}
        {stage === "success" && successVideo ? (
          <div className="ok-box" role="status">
            <b>上传成功</b>：{successVideo.filename}
            {successVideo.status === "needs_transcode"
              ? "（已上传，待转码后可在浏览器中播放）"
              : ""}
          </div>
        ) : null}
        {stage === "error" && errorMsg ? (
          <div className="error-box" role="alert">
            {errorMsg}
          </div>
        ) : null}
        {stage === "cancelled" && errorMsg ? (
          <div className="muted-box" role="status">
            {errorMsg}
          </div>
        ) : null}

        {/* 操作区 */}
        <div className="upload-actions">
          {stage === "idle" && file ? (
            <>
              <button type="button" className="btn btn-primary" onClick={() => void startUpload()}>
                上传
              </button>
              <button type="button" className="btn" onClick={reset}>
                移除文件
              </button>
            </>
          ) : null}

          {uploading ? (
            <>
              <button type="button" className="btn btn-danger" onClick={cancelUpload}>
                取消上传
              </button>
              <span className="upload-note">上传中请勿关闭页面</span>
            </>
          ) : null}

          {stage === "success" && successVideo ? (
            <>
              {successVideo.status === "needs_transcode" ? (
                <span className="upload-note">已上传 · 待转码，转码完成后即可进入标注</span>
              ) : (
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => onEnterAnnotation(successVideo)}
                >
                  进入标注 →
                </button>
              )}
              <button type="button" className="btn" onClick={closePanel}>
                ✕ 完成
              </button>
            </>
          ) : null}

          {stage === "error" ? (
            <>
              <button type="button" className="btn btn-primary" onClick={() => void startUpload()}>
                重试
              </button>
              <button type="button" className="btn" onClick={reset}>
                移除文件
              </button>
            </>
          ) : null}

          {stage === "cancelled" ? (
            <>
              <button type="button" className="btn btn-primary" onClick={() => void startUpload()}>
                重新上传
              </button>
              <button type="button" className="btn" onClick={reset}>
                移除文件
              </button>
            </>
          ) : null}
        </div>
      </div>
    </section>
  );
}
