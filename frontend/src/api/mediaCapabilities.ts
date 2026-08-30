import type { ClipItem, Video } from "./types";

export function canPlayVideo(status: Video["playback_status"]): boolean {
  return status === "ready";
}

export function playbackStatusLabel(status: Video["playback_status"]): string {
  return status === "ready" ? "可播放" : status === "pending" ? "处理中" : status === "failed" ? "处理失败" : "不可播放";
}

export function shouldPollClipMedia(status: ClipItem["media_status"]): boolean {
  return status === "pending" || status === "processing" || status === "stale";
}

export function effectiveClipMediaStatus(
  status: ClipItem["media_status"],
  clipId: ClipItem["clip_id"]
): ClipItem["media_status"] {
  return clipId == null ? "pending" : status;
}

export function canRequestClipThumbnail(
  status: ClipItem["media_status"],
  clipId: ClipItem["clip_id"]
): clipId is number {
  return clipId != null && effectiveClipMediaStatus(status, clipId) === "ready";
}

export function clipItemKey(clip: Pick<ClipItem, "item_key">): string {
  return clip.item_key;
}
