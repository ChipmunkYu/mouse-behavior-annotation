export type MediaSurface = "preview" | "clips" | "review" | "annotate";

export interface NativeMediaFlags {
  global: string | undefined;
  preview: string | undefined;
  clips: string | undefined;
  review: string | undefined;
  annotate: string | undefined;
}

/** 仅接受严格的小写字面值 true；总开关和页面开关必须同时开启。 */
export function parseNativeMediaEnabled(surface: MediaSurface, flags: NativeMediaFlags): boolean {
  return flags.global === "true" && flags[surface] === "true";
}

const BUILD_FLAGS: NativeMediaFlags = {
  global: import.meta.env.VITE_NATIVE_MEDIA_ENABLED,
  preview: import.meta.env.VITE_NATIVE_MEDIA_PREVIEW_ENABLED,
  clips: import.meta.env.VITE_NATIVE_MEDIA_CLIPS_ENABLED,
  review: import.meta.env.VITE_NATIVE_MEDIA_REVIEW_ENABLED,
  annotate: import.meta.env.VITE_NATIVE_MEDIA_ANNOTATE_ENABLED,
};

export function isNativeMediaEnabled(surface: MediaSurface): boolean {
  return parseNativeMediaEnabled(surface, BUILD_FLAGS);
}
