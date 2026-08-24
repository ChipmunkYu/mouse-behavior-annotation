import { describe, expect, it } from "vitest";
import { parseNativeMediaEnabled, type MediaSurface, type NativeMediaFlags } from "./config";

describe("native media build flags", () => {
  it("requires strict true on both global and per-surface flags", () => {
    const surfaces: MediaSurface[] = ["preview", "clips", "review", "annotate"];
    for (const surface of surfaces) {
      const enabled: NativeMediaFlags = { global: "true", preview: "true", clips: "true", review: "true", annotate: "true" };
      expect(parseNativeMediaEnabled(surface, enabled)).toBe(true);
      expect(parseNativeMediaEnabled(surface, { ...enabled, global: "TRUE" })).toBe(false);
      expect(parseNativeMediaEnabled(surface, { ...enabled, [surface]: "1" })).toBe(false);
      expect(parseNativeMediaEnabled(surface, { ...enabled, [surface]: undefined })).toBe(false);
    }
  });
});
