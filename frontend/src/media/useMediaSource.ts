import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { fetchVideoStreamBlob } from "../api";
import type { MediaSurface } from "./config";
import { createMediaController, type MediaController, type MediaControllerState, type MediaReadyReason } from "./controller";
import { createLatestCallback } from "./latestCallback";

export type MediaReadyCallback = (reason: MediaReadyReason, element: HTMLVideoElement) => void;

export interface UseMediaSourceInput {
  videoId: number | string | null;
  surface: MediaSurface;
  videoRef: RefObject<HTMLVideoElement>;
  onReady?: MediaReadyCallback;
}

export type UseMediaSourceResult = MediaControllerState & {
  reload: () => void;
  cancel: () => void;
};

const IDLE: MediaControllerState = { status: "idle", generation: 0, readyReason: null };

export function useMediaSource({ videoId, surface, videoRef, onReady }: UseMediaSourceInput): UseMediaSourceResult {
  const [state, setState] = useState<MediaControllerState>(IDLE);
  const controllerRef = useRef<MediaController | null>(null);
  const readyCallbackRef = useRef(createLatestCallback(onReady));
  readyCallbackRef.current.set(onReady);

  useEffect(() => {
    const element = videoRef.current;
    if (!element || videoId == null) {
      setState(IDLE);
      return;
    }
    const controller = createMediaController({
      element,
      fetchBlob: fetchVideoStreamBlob,
      createObjectUrl: URL.createObjectURL,
      revokeObjectUrl: URL.revokeObjectURL,
      onState: setState,
      onReady: (reason, readyElement) => readyCallbackRef.current.invoke(reason, readyElement as HTMLVideoElement),
    });
    controllerRef.current = controller;
    controller.load(videoId);
    return () => {
      controllerRef.current = null;
      controller.dispose();
    };
  }, [surface, videoId, videoRef]);

  const reload = useCallback(() => {
    if (videoId != null) controllerRef.current?.load(videoId);
  }, [videoId]);

  const cancel = useCallback(() => controllerRef.current?.cancel(), []);

  return { ...state, reload, cancel };
}
