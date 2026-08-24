/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  readonly VITE_NATIVE_MEDIA_ENABLED?: string;
  readonly VITE_NATIVE_MEDIA_PREVIEW_ENABLED?: string;
  readonly VITE_NATIVE_MEDIA_CLIPS_ENABLED?: string;
  readonly VITE_NATIVE_MEDIA_REVIEW_ENABLED?: string;
  readonly VITE_NATIVE_MEDIA_ANNOTATE_ENABLED?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
