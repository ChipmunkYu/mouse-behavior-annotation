import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import type { DetectionWithTrack } from "../api/types";
import {
  clientToSource,
  getVideoOverlayGeometry,
  sourceToOverlay,
  type VideoOverlayGeometry,
  type VideoOverlayPoint,
} from "../utils/videoOverlayGeometry";

export type MeasurementToolMode = "select" | "calibrate" | "measure";

export interface MeasurementSegment {
  id: string;
  frame: number;
  start: VideoOverlayPoint;
  end: VideoOverlayPoint;
}

export interface VideoCalibration {
  reference: MeasurementSegment;
  realLength: number;
  unit: string;
}

interface MeasurementOverlayProps {
  video: HTMLVideoElement | null;
  sourceWidth?: number | null;
  sourceHeight?: number | null;
  frame: number;
  mode: MeasurementToolMode;
  detections: DetectionWithTrack[];
  keypointsVisible: boolean;
  calibration: VideoCalibration | null;
  measurements: MeasurementSegment[];
  onCalibrationLineChange: (line: MeasurementSegment | null) => void | Promise<void>;
  onMeasurementsChange: (segments: MeasurementSegment[]) => void | Promise<void>;
}

const SNAP_RADIUS_CSS_PX = 12;

function length(segment: Pick<MeasurementSegment, "start" | "end">): number {
  return Math.hypot(segment.end.x - segment.start.x, segment.end.y - segment.start.y);
}

function keypointXY(point: unknown): VideoOverlayPoint | null {
  if (!point || typeof point !== "object") return null;
  const value = point as { x_px?: number; y_px?: number; x?: number; y?: number; confidence?: number };
  const x = value.x_px ?? value.x;
  const y = value.y_px ?? value.y;
  return typeof x === "number" && typeof y === "number" && (value.confidence ?? 1) >= .15 ? { x, y } : null;
}

export default function MeasurementOverlay({
  video,
  sourceWidth,
  sourceHeight,
  frame,
  mode,
  detections,
  keypointsVisible,
  calibration,
  measurements,
  onCalibrationLineChange,
  onMeasurementsChange,
}: MeasurementOverlayProps) {
  const overlayRef = useRef<SVGSVGElement>(null);
  const [draft, setDraft] = useState<MeasurementSegment | null>(null);
  const [snappedPoint, setSnappedPoint] = useState<VideoOverlayPoint | null>(null);
  const [, setLayoutVersion] = useState(0);
  const active = mode !== "select";

  const geometry = getVideoOverlayGeometry(overlayRef.current, video, sourceWidth, sourceHeight);
  const snapCandidates = useMemo(() => {
    const endpoints = [calibration?.reference, ...measurements]
      .filter((item): item is MeasurementSegment => item != null)
      .flatMap((item) => [item.start, item.end]);
    if (!keypointsVisible) return endpoints;
    return endpoints.concat(detections.flatMap((detection) => (detection.keypoints ?? []).map(keypointXY).filter((point): point is VideoOverlayPoint => point != null)));
  }, [calibration, detections, keypointsVisible, measurements]);

  useEffect(() => {
    const element = overlayRef.current;
    if (!element || !video) return;
    const observer = new ResizeObserver(() => setLayoutVersion((value) => value + 1));
    observer.observe(video);
    setLayoutVersion((value) => value + 1);
    return () => observer.disconnect();
  }, [video]);

  useEffect(() => {
    setDraft(null);
    setSnappedPoint(null);
  }, [frame, mode]);

  const pointFromEvent = useCallback((event: PointerEvent<SVGSVGElement>, geo: VideoOverlayGeometry) => {
    const raw = clientToSource(event.clientX, event.clientY, event.currentTarget.getBoundingClientRect(), geo);
    let nearest: VideoOverlayPoint | null = null;
    let nearestDistance = SNAP_RADIUS_CSS_PX / geo.scale;
    for (const candidate of snapCandidates) {
      const distance = Math.hypot(candidate.x - raw.x, candidate.y - raw.y);
      if (distance <= nearestDistance) {
        nearest = candidate;
        nearestDistance = distance;
      }
    }
    setSnappedPoint(nearest);
    return nearest ?? raw;
  }, [snapCandidates]);

  function handlePointerDown(event: PointerEvent<SVGSVGElement>) {
    if (!active || event.button !== 0 || !geometry) return;
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    const start = pointFromEvent(event, geometry);
    setDraft({ id: `${Date.now()}-${event.pointerId}`, frame, start, end: start });
  }

  function handlePointerMove(event: PointerEvent<SVGSVGElement>) {
    if (!draft || !geometry) return;
    event.preventDefault();
    setDraft({ ...draft, end: pointFromEvent(event, geometry) });
  }

  function finishDrawing(event: PointerEvent<SVGSVGElement>) {
    if (!draft || !geometry) return;
    event.preventDefault();
    event.stopPropagation();
    const completed = { ...draft, end: pointFromEvent(event, geometry) };
    setDraft(null);
    setSnappedPoint(null);
    if (length(completed) * geometry.scale < 3) return;
    if (mode === "calibrate") void onCalibrationLineChange(completed);
    else if (mode === "measure") void onMeasurementsChange([...measurements, completed]);
  }

  function renderSegment(segment: MeasurementSegment, kind: "calibration" | "measurement" | "draft") {
    if (!geometry) return null;
    const start = sourceToOverlay(segment.start, geometry);
    const end = sourceToOverlay(segment.end, geometry);
    const midpoint = { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 };
    const referencePixels = calibration ? length(calibration.reference) : 0;
    const realDistance = calibration && calibration.realLength > 0 && referencePixels > 0 ? length(segment) / referencePixels * calibration.realLength : null;
    const label = kind === "calibration" && calibration && calibration.realLength > 0
      ? `${calibration.realLength.toLocaleString()} ${calibration.unit}`
      : kind === "measurement" && realDistance != null && calibration
        ? `${realDistance.toFixed(realDistance >= 10 ? 1 : 2)} ${calibration.unit}`
        : null;
    return <g className={`measurement-segment ${kind}`} key={`${kind}-${segment.id}`}>
      <line className="measurement-hit-line" x1={start.x} y1={start.y} x2={end.x} y2={end.y} />
      <line x1={start.x} y1={start.y} x2={end.x} y2={end.y} />
      <circle cx={start.x} cy={start.y} r="4" />
      <circle cx={end.x} cy={end.y} r="4" />
      {label ? <g className="measurement-label" transform={`translate(${midpoint.x} ${midpoint.y})`}>
        <rect x="-38" y="-22" width="76" height="18" rx="9" />
        <text y="-9" textAnchor="middle">{label}</text>
      </g> : null}
    </g>;
  }

  return <svg
    ref={overlayRef}
    className={`measurement-overlay${active ? " active" : ""}`}
    viewBox={geometry ? `0 0 ${geometry.cssW} ${geometry.cssH}` : undefined}
    preserveAspectRatio="none"
    onPointerDown={handlePointerDown}
    onPointerMove={handlePointerMove}
    onPointerUp={finishDrawing}
    onPointerCancel={() => { setDraft(null); setSnappedPoint(null); }}
    aria-label={mode === "calibrate" ? "参考距离标定绘制层" : mode === "measure" ? "距离测量绘制层" : "距离测量显示层"}
  >
    {calibration ? renderSegment(calibration.reference, "calibration") : null}
    {measurements.map((segment) => renderSegment(segment, "measurement"))}
    {draft ? renderSegment(draft, "draft") : null}
    {geometry && snappedPoint ? (() => { const point = sourceToOverlay(snappedPoint, geometry); return <circle className="measurement-snap" cx={point.x} cy={point.y} r="8" />; })() : null}
  </svg>;
}
