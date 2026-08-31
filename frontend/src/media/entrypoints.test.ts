import { describe, expect, it } from "vitest";
import previewSource from "../components/VideoPreviewDialog.tsx?raw";
import clipsSource from "../pages/ClipsPage.tsx?raw";
import reviewSource from "../pages/ReviewPage.tsx?raw";
import annotateSource from "../pages/AnnotatePage.tsx?raw";
import hookSource from "./useMediaSource.ts?raw";

const entries = [
  ["Preview", previewSource],
  ["Clips", clipsSource],
  ["Review", reviewSource],
  ["Annotate", annotateSource],
] as const;

describe("media entry points", () => {
  it.each(entries)("%s delegates media acquisition to useMediaSource", (_entry, source) => {
    expect(source).toContain("useMediaSource({");
    expect(source).not.toMatch(/\bfetch\s*\(/);
    expect(source).not.toMatch(/\bRange\b/);
    expect(source).not.toContain("isNativeMediaEnabled");
  });

  it("the shared hook always selects the authenticated complete-blob GET", () => {
    expect(hookSource).toContain("fetchBlob: fetchVideoStreamBlob");
    expect(hookSource).not.toContain("fetchVideoStreamTicket");
    expect(hookSource).not.toContain("isNativeMediaEnabled");
  });
});
