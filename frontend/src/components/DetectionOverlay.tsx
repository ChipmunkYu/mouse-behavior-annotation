import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { getCurrentDetectionImport, getDetections } from "../api";
import { ApiError } from "../api/client";
import type { DetectionImport, DetectionWithTrack } from "../api/types";

export interface OverlayOptions {
  boxes: boolean;
  ids: boolean;
  keypoints: boolean;
  skeleton: boolean;
}

const DEFAULT_OPTIONS: OverlayOptions = { boxes: true, ids: true, keypoints: false, skeleton: false };
const BLOCK_SIZE = 31;
const EMPTY_DETECTIONS: DetectionWithTrack[] = [];

export type FrameDataStatus = "loading" | "complete" | "incomplete";
export interface OverlayFrameData {
  frame: number;
  detections: DetectionWithTrack[];
  detectionImport: DetectionImport | null;
  status: FrameDataStatus;
}

export function detectionBlockForFrame(frame: number): { start: number; end: number } {
  const start = Math.floor(Math.max(0, frame) / BLOCK_SIZE) * BLOCK_SIZE;
  return { start, end: start + BLOCK_SIZE - 1 };
}

export function detectionRangeComplete(total: number, returned: number): boolean {
  return total <= returned;
}

function pointXY(point: unknown): [number, number, number] | null {
  if (!point || typeof point !== "object") return null;
  const p = point as { x_px?: number; y_px?: number; x?: number; y?: number; confidence?: number };
  const x = p.x_px ?? p.x;
  const y = p.y_px ?? p.y;
  return typeof x === "number" && typeof y === "number" ? [x, y, p.confidence ?? 1] : null;
}

export default function DetectionOverlay({
  projectId,
  videoId,
  video,
  currentTime,
  fallbackFps,
  selectedIds = [],
  showOnlySelected = false,
  interactive = false,
  onToggleTrack,
  onFrameData,
  options: controlledOptions,
  onOptionsChange,
  refreshKey = 0,
  onTruncated,
  trackRoleLabels = {},
}: {
  projectId: number;
  videoId: number;
  video: HTMLVideoElement | null;
  currentTime: number;
  fallbackFps?: number | null;
  selectedIds?: number[];
  showOnlySelected?: boolean;
  interactive?: boolean;
  onToggleTrack?: (id: number) => void;
  onFrameData?: (data: OverlayFrameData) => void;
  options?: OverlayOptions;
  onOptionsChange?: (options: OverlayOptions) => void;
  refreshKey?: number;
  onTruncated?: (frame: number) => void;
  trackRoleLabels?: Record<number, string>;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const cacheRef = useRef(new Map<number, DetectionWithTrack[]>());
  const completeFramesRef = useRef(new Set<number>());
  const genRef = useRef(0);
  const hitCycleRef = useRef<{ key: string; index: number }>({ key: "", index: 0 });
  const [detectionImport, setDetectionImport] = useState<DetectionImport | null>(null);
  const [loadState, setLoadState] = useState<"loading" | "ready" | "none" | "error">("loading");
  const [cacheVersion, setCacheVersion] = useState(0);
  const [localOptions, setLocalOptions] = useState(DEFAULT_OPTIONS);
  const [truncatedAt, setTruncatedAt] = useState<number | null>(null);
  const options = controlledOptions ?? localOptions;

  const fps = detectionImport?.fps || fallbackFps || 30;

  const frame = useMemo(() => {
    if (!video) return Math.max(0, Math.floor(currentTime * fps));
    return Math.max(0, Math.floor(video.currentTime * fps));
  }, [currentTime, fps, video]);
  const block = detectionBlockForFrame(frame);

  const detections = cacheRef.current.get(frame) ?? EMPTY_DETECTIONS;
  const frameStatus: FrameDataStatus = completeFramesRef.current.has(frame)
    ? "complete"
    : cacheRef.current.has(frame) ? "incomplete" : "loading";

  const setOption = (key: keyof OverlayOptions, value: boolean) => {
    const next = { ...options, [key]: value };
    setLocalOptions(next);
    onOptionsChange?.(next);
  };

  useEffect(() => {
    let alive = true;
    cacheRef.current.clear();
    completeFramesRef.current.clear();
    setDetectionImport(null);
    setLoadState("loading");
    setTruncatedAt(null);
    genRef.current += 1;
    getCurrentDetectionImport(projectId, videoId)
      // current 接口只返回当前 active 导入；不要用 revision 数值再次猜测有效性。
      .then((value) => { if (alive) { setDetectionImport(value); setLoadState("ready"); } })
      .catch((err: unknown) => {
        if (!alive) return;
        if (err instanceof ApiError && err.status === 404) { setDetectionImport(null); setLoadState("none"); }
        else setLoadState("error");
      });
    return () => { alive = false; };
  }, [projectId, videoId, refreshKey]);

  useEffect(() => {
    if (!detectionImport || loadState !== "ready") return;
    let blockComplete = true;
    for (let f = block.start; f <= block.end; f += 1) blockComplete &&= completeFramesRef.current.has(f);
    if (blockComplete) { setTruncatedAt(null); return; }
    const controller = new AbortController();
    const currentGen = ++genRef.current;

    const loadRange = async (start: number, end: number): Promise<number | null> => {
      const { detections: rows, total } = await getDetections(projectId, videoId, start, end, controller.signal);
      if (controller.signal.aborted || currentGen !== genRef.current) return null;
      if (!detectionRangeComplete(total, rows.length) && start < end) {
        const middle = Math.floor((start + end) / 2);
        const [left, right] = await Promise.all([loadRange(start, middle), loadRange(middle + 1, end)]);
        return left ?? right;
      }
      for (let f = start; f <= end; f += 1) cacheRef.current.set(f, []);
      for (const row of rows) {
        const list = cacheRef.current.get(row.frame_index) ?? [];
        list.push(row);
        cacheRef.current.set(row.frame_index, list);
      }
      if (detectionRangeComplete(total, rows.length)) {
        for (let f = start; f <= end; f += 1) completeFramesRef.current.add(f);
      }
      setCacheVersion((v) => v + 1);
      return detectionRangeComplete(total, rows.length) ? null : start;
    };

    void loadRange(block.start, block.end)
      .then((incompleteAt) => {
        if (controller.signal.aborted || currentGen !== genRef.current) return;
        setTruncatedAt(incompleteAt);
        if (incompleteAt != null) onTruncated?.(incompleteAt);
      })
      .catch((err: unknown) => {
        if (!(err instanceof DOMException && err.name === "AbortError") && currentGen === genRef.current) setLoadState("error");
      });
    return () => controller.abort();
  }, [block.end, block.start, detectionImport, loadState, projectId, videoId, onTruncated]);

  useEffect(() => {
    onFrameData?.({ frame, detections, detectionImport, status: frameStatus });
  }, [frame, cacheVersion, detectionImport, onFrameData, detections, frameStatus]);

  const geometry = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas || !video) return null;
    const cssW = canvas.clientWidth;
    const cssH = canvas.clientHeight;
    const sourceW = detectionImport?.width || video.videoWidth;
    const sourceH = detectionImport?.height || video.videoHeight;
    if (!cssW || !cssH || !sourceW || !sourceH) return null;
    const scale = Math.min(cssW / sourceW, cssH / sourceH);
    return { cssW, cssH, sourceW, sourceH, scale, ox: (cssW - sourceW * scale) / 2, oy: (cssH - sourceH * scale) / 2 };
  }, [detectionImport, video]);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    const geo = geometry();
    if (!canvas || !geo) return;
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(geo.cssW * dpr) || canvas.height !== Math.round(geo.cssH * dpr)) {
      canvas.width = Math.round(geo.cssW * dpr);
      canvas.height = Math.round(geo.cssH * dpr);
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, geo.cssW, geo.cssH);
    for (const det of detections) {
      const selected = selectedIds.includes(det.display_track_id);
      if (showOnlySelected && !selected) continue;
      const box = det.box_xyxy_px;
      if (box && box.length >= 4) {
        const [x1, y1, x2, y2] = box;
        const x = geo.ox + x1 * geo.scale;
        const y = geo.oy + y1 * geo.scale;
        const w = (x2 - x1) * geo.scale;
        const h = (y2 - y1) * geo.scale;
        if (options.boxes) {
          ctx.strokeStyle = selected ? "#ffd43b" : `hsl(${(det.display_track_id * 47) % 360} 88% 62%)`;
          ctx.lineWidth = selected ? 3 : 2;
          ctx.strokeRect(x, y, w, h);
        }
        if (options.ids) {
          const roleName = trackRoleLabels[det.display_track_id];
          const label = roleName ? `track ${det.display_track_id} · ${roleName}` : `track ID ${det.display_track_id}`;
          ctx.font = "600 12px Cascadia Mono, monospace";
          const tw = ctx.measureText(label).width + 10;
          ctx.fillStyle = selected ? "#ffd43b" : "rgba(10, 14, 20, .82)";
          ctx.fillRect(x, Math.max(0, y - 20), tw, 20);
          ctx.fillStyle = selected ? "#17191d" : "#fff";
          ctx.fillText(label, x + 5, Math.max(14, y - 6));
        }
      }
      const pts = (det.keypoints ?? []).map(pointXY).filter((p): p is [number, number, number] => p != null && p[2] >= .15);
      if (options.skeleton && pts.length > 1) {
        ctx.strokeStyle = selected ? "#ffd43b" : "rgba(70, 220, 255, .85)";
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        pts.forEach(([x, y], i) => { const px = geo.ox + x * geo.scale; const py = geo.oy + y * geo.scale; if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py); });
        ctx.stroke();
      }
      if (options.keypoints) {
        ctx.fillStyle = selected ? "#ffd43b" : "#55e6ff";
        pts.forEach(([x, y]) => { ctx.beginPath(); ctx.arc(geo.ox + x * geo.scale, geo.oy + y * geo.scale, 2.5, 0, Math.PI * 2); ctx.fill(); });
      }
    }
  }, [detections, geometry, options, selectedIds, showOnlySelected, trackRoleLabels]);

  useEffect(() => {
    if (!video) return;
    let raf: number;
    let prevFrame = -1;
    function loop() {
      const f = Math.max(0, Math.floor(video!.currentTime * fps));
      if (f !== prevFrame) {
        prevFrame = f;
        draw();
      }
      raf = requestAnimationFrame(loop);
    }
    raf = requestAnimationFrame(loop);
    const observer = new ResizeObserver(draw);
    observer.observe(video);
    return () => {
      cancelAnimationFrame(raf);
      observer.disconnect();
    };
  }, [video, fps, draw]);

  useEffect(() => {
    draw();
  }, [cacheVersion, draw]);

  const hits = useMemo(() => detections.filter((d) => d.box_xyxy_px?.length === 4), [detections]);

  function handleClick(e: MouseEvent<HTMLCanvasElement>) {
    if (!interactive || !onToggleTrack) return;
    const geo = geometry();
    if (!geo) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const sx = (e.clientX - rect.left - geo.ox) / geo.scale;
    const sy = (e.clientY - rect.top - geo.oy) / geo.scale;
    const candidates = hits.filter((d) => {
      const b = d.box_xyxy_px!;
      return sx >= b[0] && sx <= b[2] && sy >= b[1] && sy <= b[3];
    });
    if (!candidates.length) {
      if (video && video.paused !== undefined) {
        e.stopPropagation();
        const clickEv = new MouseEvent("click", { bubbles: true, clientX: e.clientX, clientY: e.clientY });
        video.dispatchEvent(clickEv);
      }
      return;
    }
    e.stopPropagation();
    const key = candidates.map((d) => d.detection_id).join("-");
    const index = hitCycleRef.current.key === key ? (hitCycleRef.current.index + 1) % candidates.length : 0;
    hitCycleRef.current = { key, index };
    onToggleTrack(candidates[index].display_track_id);
  }

  return (
    <>
      <canvas ref={canvasRef} className={`detection-overlay${interactive ? " interactive" : ""}`} onClick={handleClick} aria-label="YOLO 检测与 track 叠加层" />
      <div className="overlay-toolbar" aria-label="检测显示选项">
        {(["boxes", "ids", "keypoints", "skeleton"] as const).map((key) => (
          <label className="toggle-control" key={key}>
            <input type="checkbox" checked={options[key]} onChange={(e) => setOption(key, e.target.checked)} />
            <span>{({ boxes: "框", ids: "track ID", keypoints: "关键点", skeleton: "骨架" })[key]}</span>
          </label>
        ))}
        <span className={`batch-indicator ${loadState}`}>
          {loadState === "ready" ? `帧 ${frame} · ${detections.length}` : loadState === "none" ? "无 YOLO 数据" : loadState === "error" ? "检测读取失败" : "检测加载中"}
          {truncatedAt != null ? " ⚠ 截断" : ""}
        </span>
      </div>
    </>
  );
}
