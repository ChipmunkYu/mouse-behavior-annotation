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
const BLOCK_RADIUS = 15;
const MAX_DETECTIONS_PER_REQUEST = 500;

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
  interactive = false,
  onToggleTrack,
  onFrameData,
  options: controlledOptions,
  onOptionsChange,
  refreshKey = 0,
  onTruncated,
}: {
  projectId: number;
  videoId: number;
  video: HTMLVideoElement | null;
  currentTime: number;
  fallbackFps?: number | null;
  selectedIds?: number[];
  interactive?: boolean;
  onToggleTrack?: (id: number) => void;
  onFrameData?: (data: { frame: number; detections: DetectionWithTrack[]; detectionImport: DetectionImport | null }) => void;
  options?: OverlayOptions;
  onOptionsChange?: (options: OverlayOptions) => void;
  refreshKey?: number;
  onTruncated?: (frame: number) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const cacheRef = useRef(new Map<number, DetectionWithTrack[]>());
  const pendingRef = useRef(new Set<string>());
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

  const detections = cacheRef.current.get(frame) ?? [];

  const setOption = (key: keyof OverlayOptions, value: boolean) => {
    const next = { ...options, [key]: value };
    setLocalOptions(next);
    onOptionsChange?.(next);
  };

  useEffect(() => {
    let alive = true;
    cacheRef.current.clear();
    pendingRef.current.clear();
    setLoadState("loading");
    setTruncatedAt(null);
    genRef.current += 1;
    getCurrentDetectionImport(projectId, videoId)
      .then((value) => { if (alive) { setDetectionImport(value); setLoadState(value.revision > 0 ? "ready" : "none"); } })
      .catch((err: unknown) => {
        if (!alive) return;
        if (err instanceof ApiError && err.status === 404) { setDetectionImport(null); setLoadState("none"); }
        else setLoadState("error");
      });
    return () => { alive = false; };
  }, [projectId, videoId, refreshKey]);

  useEffect(() => {
    if (!detectionImport || loadState !== "ready") return;
    if (cacheRef.current.has(frame)) return;
    const start = Math.max(0, frame - BLOCK_RADIUS);
    const end = frame + BLOCK_RADIUS;
    const key = `${start}-${end}-${detectionImport.revision}`;
    if (pendingRef.current.has(key)) return;
    pendingRef.current.add(key);
    const currentGen = genRef.current;
    getDetections(projectId, videoId, start, end)
      .then(({ detections: rows, total }) => {
        if (currentGen !== genRef.current) return;
        for (let f = start; f <= end; f += 1) cacheRef.current.set(f, []);
        for (const row of rows) {
          const list = cacheRef.current.get(row.frame_index) ?? [];
          list.push(row);
          cacheRef.current.set(row.frame_index, list);
        }
        if (total >= MAX_DETECTIONS_PER_REQUEST) {
          setTruncatedAt(start);
          onTruncated?.(start);
        } else {
          setTruncatedAt(null);
        }
        setCacheVersion((v) => v + 1);
      })
      .catch(() => setLoadState("error"))
      .finally(() => pendingRef.current.delete(key));
  }, [detectionImport, frame, loadState, projectId, videoId, onTruncated]);

  useEffect(() => {
    onFrameData?.({ frame, detections, detectionImport });
  }, [frame, cacheVersion, detectionImport, onFrameData, detections]);

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
          const label = `track ID ${det.display_track_id}`;
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
  }, [detections, geometry, options, selectedIds]);

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
