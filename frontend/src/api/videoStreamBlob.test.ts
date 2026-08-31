// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchVideoStreamBlob } from "./index";

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("fetchVideoStreamBlob", () => {
  it("performs one authenticated full GET and returns Content-Length with the completed blob", async () => {
    localStorage.setItem("mba_token", "test-token");
    const responseBlob = new Blob(["complete-video"]);
    const blob = vi.fn().mockResolvedValue(responseBlob);
    const fetchMock = vi.fn();
    // Response.blob is instrumented so completion timing and the single conversion are explicit.
    fetchMock.mockResolvedValueOnce(Object.assign(new Response(null, {
      status: 200,
      headers: { "Content-Length": String(responseBlob.size) },
    }), { blob }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await fetchVideoStreamBlob(7);

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/videos\/7\/stream$/);
    expect(init.method).toBeUndefined();
    expect(new Headers(init.headers).get("Range")).toBeNull();
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer test-token");
    expect(blob).toHaveBeenCalledOnce();
    expect(result).toEqual({ blob: responseBlob, contentLength: responseBlob.size });
  });

  it("preserves the stable 409 detail for controller classification", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: "DISPLAY_PROXY_PENDING" }),
      { status: 409, headers: { "Content-Type": "application/json" } },
    )));

    await expect(fetchVideoStreamBlob(8)).rejects.toMatchObject({
      status: 409,
      detail: "DISPLAY_PROXY_PENDING",
    });
  });

  it("reads the response stream into one blob and reports actual byte progress", async () => {
    const encoder = new TextEncoder();
    const chunks = [encoder.encode("abc"), encoder.encode("defg")];
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        chunks.forEach((chunk) => controller.enqueue(chunk));
        controller.close();
      },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(stream, {
      status: 200,
      headers: { "Content-Length": "7", "Content-Type": "video/mp4" },
    })));
    const progress = vi.fn();

    const result = await fetchVideoStreamBlob(9, undefined, progress);

    expect(await result.blob.text()).toBe("abcdefg");
    expect(result.blob.type).toBe("video/mp4");
    expect(progress.mock.calls.map(([value]) => value)).toEqual([
      { loaded: 0, total: 7 },
      { loaded: 3, total: 7 },
      { loaded: 7, total: 7 },
    ]);
  });

  it("rejects a streamed response that does not match Content-Length", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("short", {
      status: 200,
      headers: { "Content-Length": "10" },
    })));

    await expect(fetchVideoStreamBlob(10)).rejects.toMatchObject({
      status: 502,
      message: "视频下载不完整，请重试",
    });
  });
});
