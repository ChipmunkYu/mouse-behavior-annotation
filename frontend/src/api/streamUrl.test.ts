import { describe, expect, it } from "vitest";
import {
  assertValidVideoStreamUrl,
  InvalidVideoStreamUrlError,
} from "./streamUrl";

describe("stream-ticket URL validation", () => {
  it.each([
    [1, "/api/videos/1/stream"],
    [42, "/api/videos/42/stream"],
    ["1", "/api/videos/1/stream"],
    ["9007199254740993", "/api/videos/9007199254740993/stream"],
  ])("accepts the exact stream path for canonical ID %s", (videoId, url) => {
    expect(() => assertValidVideoStreamUrl(videoId, url)).not.toThrow();
  });

  it.each([
    [1, "https://example.test/api/videos/1/stream", "absolute URL"],
    [1, "https://evil.test/api/videos/1/stream", "cross-origin URL"],
    [1, "//evil.test/api/videos/1/stream", "protocol-relative URL"],
    [1, "/api/videos/1/stream?ticket=secret", "query"],
    [1, "/api/videos/1/stream#fragment", "fragment"],
    [1, "/api/videos/%31/stream", "encoded ID"],
    [1, "/api/videos/1%2fstream", "encoded slash"],
    [1, "/api/videos/1%5cstream", "encoded backslash"],
    [1, "/api/videos/../1/stream", "path traversal"],
    [1, "/api/videos/1/../1/stream", "path traversal after ID"],
    [1, "/api/videos/1/stream/", "trailing slash"],
    [1, "/api/videos/2/stream", "different ID"],
    [1, " /api/videos/1/stream", "leading whitespace"],
    [1, "/api/videos/1/stream\n", "trailing whitespace"],
    [1, "/api//videos/1/stream", "changed separators"],
  ])("rejects %s as %s", (videoId, url) => {
    expect(() => assertValidVideoStreamUrl(videoId, url)).toThrow(InvalidVideoStreamUrlError);
  });

  it.each([
    0,
    -1,
    1.5,
    Number.NaN,
    Number.POSITIVE_INFINITY,
    Number.MAX_SAFE_INTEGER + 1,
    "",
    "0",
    "-1",
    "+1",
    "01",
    "1.0",
    "1e3",
    " 1",
    "1 ",
    "1/2",
  ])("rejects invalid requested video ID %s", (videoId) => {
    expect(() =>
      assertValidVideoStreamUrl(videoId, `/api/videos/${videoId}/stream`)
    ).toThrow(InvalidVideoStreamUrlError);
  });

  it("does not include a malicious URL in a serialized validation error", () => {
    const maliciousUrl = "https://evil.test/steal?secret=do-not-echo";
    let error: unknown;

    try {
      assertValidVideoStreamUrl(1, maliciousUrl);
    } catch (caught) {
      error = caught;
    }

    expect(error).toBeInstanceOf(InvalidVideoStreamUrlError);
    const serialized = JSON.stringify(error, Object.getOwnPropertyNames(error as object));
    expect(serialized).not.toContain(maliciousUrl);
    expect(String(error)).not.toContain(maliciousUrl);
  });
});
