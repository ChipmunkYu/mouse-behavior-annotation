import type { Video } from "../api/types";

function createdAtTimestamp(value: string): number {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : Number.NEGATIVE_INFINITY;
}

/** 按创建时间、视频 ID 倒序返回新数组；无效时间统一视为最早时间。 */
export function sortVideosForNavigation(videos: readonly Video[]): Video[] {
  return videos
    .map((video, index) => ({ video, index }))
    .sort((a, b) => {
      const aTime = createdAtTimestamp(a.video.created_at);
      const bTime = createdAtTimestamp(b.video.created_at);
      if (aTime !== bTime) return aTime > bTime ? -1 : 1;
      const idDifference = b.video.id - a.video.id;
      if (idDifference !== 0) return idDifference;
      return a.index - b.index;
    })
    .map(({ video }) => video);
}

export function getAdjacentVideos(
  sortedVideos: readonly Video[],
  currentVideoId: number
): { previous: Video | null; next: Video | null } {
  const currentIndex = sortedVideos.findIndex((video) => video.id === currentVideoId);
  if (currentIndex < 0) return { previous: null, next: null };
  return {
    previous: sortedVideos[currentIndex - 1] ?? null,
    next: sortedVideos[currentIndex + 1] ?? null,
  };
}
