export interface VideoOverlayGeometry {
  cssW: number;
  cssH: number;
  sourceW: number;
  sourceH: number;
  scale: number;
  ox: number;
  oy: number;
}

export interface VideoOverlayPoint {
  x: number;
  y: number;
}

export function getVideoOverlayGeometry(
  element: Pick<HTMLElement, "clientWidth" | "clientHeight"> | null,
  video: Pick<HTMLVideoElement, "videoWidth" | "videoHeight"> | null,
  sourceWidth?: number | null,
  sourceHeight?: number | null,
): VideoOverlayGeometry | null {
  if (!element || !video) return null;
  const cssW = element.clientWidth;
  const cssH = element.clientHeight;
  const sourceW = sourceWidth || video.videoWidth;
  const sourceH = sourceHeight || video.videoHeight;
  if (!cssW || !cssH || !sourceW || !sourceH) return null;
  const scale = Math.min(cssW / sourceW, cssH / sourceH);
  return { cssW, cssH, sourceW, sourceH, scale, ox: (cssW - sourceW * scale) / 2, oy: (cssH - sourceH * scale) / 2 };
}

export function sourceToOverlay(point: VideoOverlayPoint, geometry: VideoOverlayGeometry): VideoOverlayPoint {
  return { x: geometry.ox + point.x * geometry.scale, y: geometry.oy + point.y * geometry.scale };
}

export function clientToSource(
  clientX: number,
  clientY: number,
  rect: Pick<DOMRect, "left" | "top">,
  geometry: VideoOverlayGeometry,
): VideoOverlayPoint {
  return {
    x: Math.min(geometry.sourceW, Math.max(0, (clientX - rect.left - geometry.ox) / geometry.scale)),
    y: Math.min(geometry.sourceH, Math.max(0, (clientY - rect.top - geometry.oy) / geometry.scale)),
  };
}
