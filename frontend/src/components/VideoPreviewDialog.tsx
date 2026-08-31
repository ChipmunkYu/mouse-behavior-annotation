import { useCallback, useEffect, useRef, type KeyboardEvent as ReactKeyboardEvent } from "react";
import type { Video } from "../api/types";
import { useMediaSource } from "../media";
import { MediaLoadProgress } from "./MediaLoadProgress";

interface VideoPreviewDialogProps {
  video: Pick<Video, "id" | "filename">;
  onClose: () => void;
}

export default function VideoPreviewDialog({ video, onClose }: VideoPreviewDialogProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  const focusRestoreFrameRef = useRef<number | null>(null);
  const media = useMediaSource({ videoId: video.id, surface: "preview", videoRef });

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
      aria-describedby={media.status !== "ready" ? statusId : undefined}
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
        <video
          ref={videoRef}
          className={`video-preview-player${media.status === "ready" ? "" : " media-player-pending"}`}
          controls
          playsInline
          preload="metadata"
        />
        <MediaLoadProgress id={statusId} state={media} onCancel={media.cancel} />
        {media.status === "pending" || media.status === "failed" || media.status === "cancelled" ? <div id={statusId} className={`video-preview-message media-status-overlay${media.status === "failed" ? " video-preview-error" : ""}`} role={media.status === "failed" ? "alert" : "status"}><span>{media.message}</span><button type="button" className="btn btn-sm" onClick={media.reload}>{media.status === "cancelled" ? "重新下载" : "重试"}</button></div> : null}
      </div>
      <footer className="video-preview-footer"><span>仅预览原始视频，不会领取任务或进入标注。</span><button type="button" className="btn" onClick={onClose}>关闭预览</button></footer>
    </div>
  </div>;
}
