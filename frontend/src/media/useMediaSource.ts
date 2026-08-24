import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { fetchVideoStreamTicket, fetchVideoStreamUrl } from "../api";
import { isNativeMediaEnabled, type MediaSurface } from "./config";
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
  /** 重新创建 generation；同一 generation 内的原生 error 自动续票仍严格最多一次。 */
  reload: () => void;
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
      native: isNativeMediaEnabled(surface),
      fetchTicket: fetchVideoStreamTicket,
      fetchLegacyUrl: fetchVideoStreamUrl,
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

  return { ...state, reload };
}
