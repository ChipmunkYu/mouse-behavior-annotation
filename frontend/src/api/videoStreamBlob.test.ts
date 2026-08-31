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
});
