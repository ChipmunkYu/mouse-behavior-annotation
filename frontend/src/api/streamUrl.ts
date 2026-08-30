const CANONICAL_POSITIVE_INTEGER = /^[1-9]\d*$/;

/** Validation failures deliberately carry no untrusted values. */
export class InvalidVideoStreamUrlError extends Error {
  constructor() {
    super("Invalid video stream URL");
    this.name = "InvalidVideoStreamUrlError";
  }
}

function canonicalVideoId(videoId: number | string): string | null {
  if (typeof videoId === "number") {
    if (!Number.isSafeInteger(videoId) || videoId <= 0) return null;
    return String(videoId);
  }
  return CANONICAL_POSITIVE_INTEGER.test(videoId) ? videoId : null;
}

/**
 * Accept only the exact, clean, same-origin-relative stream path for the
 * requested canonical positive integer video ID.
 */
export function assertValidVideoStreamUrl(
  requestedVideoId: number | string,
  responseUrl: unknown
): asserts responseUrl is string {
  const videoId = canonicalVideoId(requestedVideoId);
  if (videoId === null || responseUrl !== `/api/videos/${videoId}/stream`) {
    throw new InvalidVideoStreamUrlError();
  }
}
