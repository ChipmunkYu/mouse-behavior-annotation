import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { MediaLoadProgress } from "./MediaLoadProgress";

describe("MediaLoadProgress", () => {
  it("renders percentage, actual bytes, and determinate ARIA values when length is known", () => {
    const html = renderToStaticMarkup(<MediaLoadProgress
      state={{ status: "downloading", generation: 1, readyReason: null, loadedBytes: 512, totalBytes: 1024 }}
      onCancel={vi.fn()}
    />);

    expect(html).toContain("50%");
    expect(html).toContain("512 B / 1.0 KB");
    expect(html).toContain('role="progressbar"');
    expect(html).toContain('aria-valuenow="50"');
    expect(html).toContain('aria-valuetext="视频已加载 50%"');
  });

  it("uses accessible indeterminate semantics when Content-Length is absent", () => {
    const html = renderToStaticMarkup(<MediaLoadProgress
      state={{ status: "downloading", generation: 1, readyReason: null, loadedBytes: 4096, totalBytes: null }}
      onCancel={vi.fn()}
    />);

    expect(html).toContain("已加载 4.0 KB");
    expect(html).toContain("is-indeterminate");
    expect(html).toContain('aria-valuetext="正在加载视频，暂无法计算进度"');
    expect(html).not.toContain("aria-valuenow");
  });

  it("disappears after media becomes ready", () => {
    const html = renderToStaticMarkup(<MediaLoadProgress
      state={{ status: "ready", generation: 1, readyReason: "initial", contentLength: 8, blobSize: 8 }}
      onCancel={vi.fn()}
    />);
    expect(html).toBe("");
  });
});
