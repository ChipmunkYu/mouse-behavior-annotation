import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import Timeline from "./Timeline";

const props = {
  duration: 10,
  currentTime: 2,
  annotations: [
    { id: 1, category_id: 10, start_time: 1, end_time: 3 },
    { id: 2, category_id: 20, start_time: 2, end_time: 4 },
  ],
  categoryById: new Map(),
  onSeek: vi.fn(),
};

describe("Timeline focus styling", () => {
  it("keeps the existing interval markup when focus is not provided", () => {
    const html = renderToStaticMarkup(<Timeline {...props} />);
    expect(html.match(/class="timeline-interval"/g)).toHaveLength(2);
    expect(html).not.toContain("is-focused");
    expect(html).not.toContain("is-muted");
  });

  it("marks one interval as focused without making intervals interactive", () => {
    const html = renderToStaticMarkup(<Timeline {...props} focusedAnnotationId={2} />);
    expect(html).toContain('class="timeline-interval is-focused"');
    expect(html).toContain('class="timeline-interval is-muted"');
    expect(html).not.toContain("<button");
  });
});
