import { describe, expect, it } from "vitest";
import { canPlayVideo, canRequestClipThumbnail, clipItemKey, effectiveClipMediaStatus, playbackStatusLabel, shouldPollClipMedia } from "./mediaCapabilities";

describe("media capability DTO states", () => {
  it("keeps legacy and submission identity spaces distinct", () => {
    expect(clipItemKey({ item_key: "legacy:7" })).not.toBe(
      clipItemKey({ item_key: "submission:7" })
    );
  });

  it("only enables video playback for ready", () => {
    expect(canPlayVideo("ready")).toBe(true);
    expect(canPlayVideo("pending")).toBe(false);
    expect(canPlayVideo("failed")).toBe(false);
    expect(canPlayVideo("unavailable")).toBe(false);
    expect(playbackStatusLabel("pending")).toBe("处理中");
  });

  it("polls only unfinished clip media states", () => {
    expect(shouldPollClipMedia("pending")).toBe(true);
    expect(shouldPollClipMedia("processing")).toBe(true);
    expect(shouldPollClipMedia("stale")).toBe(true);
    expect(shouldPollClipMedia("ready")).toBe(false);
    expect(shouldPollClipMedia("failed")).toBe(false);
    expect(effectiveClipMediaStatus("ready", null)).toBe("pending");
    expect(effectiveClipMediaStatus("ready", 42)).toBe("ready");
    expect(canRequestClipThumbnail("ready", null)).toBe(false);
    expect(canRequestClipThumbnail("ready", 42)).toBe(true);
  });
});
