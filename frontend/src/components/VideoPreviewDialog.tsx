import { useCallback, useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { fetchVideoStreamUrl } from "../api";
import type { Video } from "../api/types";

interface VideoPreviewDialogProps {
  video: Pick<Video, "id" | "filename">;
  onClose: () => void;
}

type PreviewState = "loading" | "ready" | "error";

export default function VideoPreviewDialog({ video, onClose }: VideoPreviewDialogProps) {
  const [streamUrl, setStreamUrl] = useState<string | null>(null);
  const [state, setState] = useState<PreviewState>("loading");
  const [error, setError] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  const focusRestoreFrameRef = useRef<number | null>(null);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (focusRestoreFrameRef.current !== null) cancelAnimationFrame(focusRestoreFrameRef.current);
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeButtonRef.current?.focus();

    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      onCloseRef.current();
    };
    window.addEventListener("keydown", onKeyDown);

    return () => {
      window.removeEventListener("keydown", onKeyDown);
      focusRestoreFrameRef.current = requestAnimationFrame(() => {
        focusRestoreFrameRef.current = null;
        if (previouslyFocused?.isConnected) previouslyFocused.focus();
      });
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;

    setStreamUrl(null);
    setState("loading");
    setError(null);

    fetchVideoStreamUrl(video.id)
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        objectUrl = url;
        setStreamUrl(url);
        setState("ready");
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setState("error");
        setError(reason instanceof Error ? reason.message : "视频加载失败，请稍后重试。");
      });

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [video.id]);

  const keepFocusInside = useCallback((event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Tab") return;
    const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
      "button:not([disabled]), video[controls], [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])",
    );
    if (!focusable?.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }, []);

  const titleId = `video-preview-title-${video.id}`;
  const statusId = `video-preview-status-${video.id}`;

  return <div className="modal-overlay video-preview-overlay" onClick={onClose}>
    <div
      ref={dialogRef}
      className="modal video-preview-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={state !== "ready" ? statusId : undefined}
      onClick={(event) => event.stopPropagation()}
      onKeyDown={keepFocusInside}
    >
      <header className="video-preview-header">
        <div className="video-preview-heading">
          <span>原始视频预览</span>
          <h2 id={titleId} title={video.filename}>{video.filename}</h2>
        </div>
        <button ref={closeButtonRef} type="button" className="btn btn-sm video-preview-close" onClick={onClose} aria-label="关闭视频预览">关闭 <span aria-hidden="true">×</span></button>
      </header>

      <div className="video-preview-stage">
        {state === "loading" ? <div id={statusId} className="video-preview-message" role="status"><span className="spinner" aria-hidden="true"/>正在加载视频…</div> : null}
        {error ? <div id={statusId} className="video-preview-message video-preview-error" role="alert">{error}</div> : null}
        {streamUrl ? <video
          key={streamUrl}
          className="video-preview-player"
          src={streamUrl}
          controls
          playsInline
          preload="metadata"
          onError={() => {
            setStreamUrl(null);
            setState("error");
            setError("当前视频无法在浏览器中播放，请检查文件格式。");
          }}
        /> : null}
      </div>
      <footer className="video-preview-footer"><span>仅预览原始视频，不会领取任务或进入标注。</span><button type="button" className="btn" onClick={onClose}>关闭预览</button></footer>
    </div>
  </div>;
}
