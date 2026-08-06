/**
 * DemoMouseStage：演示模式的「顶视小鼠演示画面」占位播放器。
 * - Canvas 绘制：暗色鼠笼地面 + 格线 + 数只沿平滑轨迹游走的小鼠（无任何视频二进制资源）
 * - 支持播放 / 暂停、逐帧步进（由外部驱动 currentTime）、时间轴跳转、片段区间循环
 * - 页面显著标注「演示画面」，避免与真实视频混淆
 * 标注工作台与审核工作台在演示模式下用它替换 <video>。
 */
import { useCallback, useEffect, useMemo, useRef } from "react";

export interface DemoMouseStageProps {
  /** 总时长（秒） */
  duration: number;
  /** 当前播放时间（外部受控，配合时间轴 / S/D 标注） */
  currentTime: number;
  playing: boolean;
  fps?: number | null;
  /** 轨迹随机种子（按视频 / 片段 id 固定，保证同一视频每次一致） */
  seed?: number;
  /** 片段预览循环区间（clip 本地时间） */
  loop?: { start: number; end: number } | null;
  /** 右上角标注，如「标注工作台 · 演示画面」 */
  badge?: string;
  onTimeUpdate: (t: number) => void;
  onTogglePlay: () => void;
}

interface Mouse {
  cx: number;
  cy: number;
  a1: number;
  a2: number;
  w1: number;
  w2: number;
  p1: number;
  p2: number;
  size: number;
  color: string;
}

function mulberry32(a: number): () => number {
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function buildMice(seed: number): Mouse[] {
  const rng = mulberry32((seed * 7919) | 0);
  const colors = ["#e8e6e3", "#b9b4ac", "#8f8a83", "#5f6a72"];
  return Array.from({ length: 4 }, (_, i) => ({
    cx: 0.22 + rng() * 0.56,
    cy: 0.24 + rng() * 0.5,
    a1: 14 + rng() * 26,
    a2: 5 + rng() * 12,
    w1: 0.28 + rng() * 0.6,
    w2: 0.5 + rng() * 1.1,
    p1: rng() * Math.PI * 2,
    p2: rng() * Math.PI * 2,
    size: 6.5 + rng() * 3.5,
    color: colors[i % colors.length],
  }));
}

function mousePos(m: Mouse, t: number, W: number, H: number): { x: number; y: number; heading: number } {
  const x = m.cx * W + m.a1 * Math.sin(m.w1 * t + m.p1) + m.a2 * Math.sin(m.w2 * t + m.p2);
  const y = m.cy * H + m.a1 * 0.85 * Math.cos(m.w1 * 0.92 * t + m.p1 * 1.31) + m.a2 * 0.7 * Math.sin(m.w2 * t + m.p2 + 1.2);
  const dx = m.a1 * m.w1 * Math.cos(m.w1 * t + m.p1) + m.a2 * m.w2 * Math.cos(m.w2 * t + m.p2);
  const dy = -m.a1 * 0.85 * m.w1 * 0.92 * Math.sin(m.w1 * 0.92 * t + m.p1 * 1.31) + m.a2 * 0.7 * m.w2 * Math.cos(m.w2 * t + m.p2 + 1.2);
  return { x, y, heading: Math.atan2(dy, dx) };
}

function fmtT(sec: number): string {
  const s = Math.max(0, Math.floor(sec));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

export default function DemoMouseStage({
  duration,
  currentTime,
  playing,
  fps,
  seed = 1,
  loop = null,
  badge,
  onTimeUpdate,
  onTogglePlay,
}: DemoMouseStageProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const tRef = useRef(currentTime);
  const latest = useRef({ duration, fps, loop, onTimeUpdate, onTogglePlay });
  latest.current = { duration, fps, loop, onTimeUpdate, onTogglePlay };
  const mice = useMemo(() => buildMice(seed), [seed]);

  // 暂停 / 外部跳转时，以外部 currentTime 为准（避免与播放器内部时钟脱节）
  useEffect(() => {
    if (!playing) tRef.current = currentTime;
  }, [currentTime, playing]);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const W = canvas.clientWidth;
    const H = canvas.clientHeight;
    if (W === 0 || H === 0) return;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(dpr, dpr);

    const t = tRef.current;
    const cur = latest.current;
    const dur = cur.duration > 0 ? cur.duration : 1;

    // 地面
    ctx.fillStyle = "#14161a";
    ctx.fillRect(0, 0, W, H);
    // 格线
    ctx.strokeStyle = "rgba(255,255,255,0.045)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let x = 40; x < W; x += 40) {
      ctx.moveTo(x, 0);
      ctx.lineTo(x, H);
    }
    for (let y = 40; y < H; y += 40) {
      ctx.moveTo(0, y);
      ctx.lineTo(W, y);
    }
    ctx.stroke();
    // 笼壁
    ctx.strokeStyle = "rgba(255,255,255,0.18)";
    ctx.lineWidth = 2;
    ctx.strokeRect(1, 1, W - 2, H - 2);
    // 食皿（角落圆形）
    ctx.fillStyle = "rgba(255,255,255,0.07)";
    ctx.beginPath();
    ctx.arc(W - 56, 40, 26, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "rgba(255,255,255,0.1)";
    ctx.beginPath();
    ctx.arc(W - 56, 40, 12, 0, Math.PI * 2);
    ctx.fill();

    // 小鼠
    for (const m of mice) {
      const { x, y, heading } = mousePos(m, t, W, H);
      ctx.save();
      ctx.translate(x, y);
      ctx.rotate(heading);
      // 阴影
      ctx.fillStyle = "rgba(0,0,0,0.35)";
      ctx.beginPath();
      ctx.ellipse(1, 2.5, m.size * 1.5, m.size * 0.85, 0, 0, Math.PI * 2);
      ctx.fill();
      // 身体
      ctx.fillStyle = m.color;
      ctx.beginPath();
      ctx.ellipse(0, 0, m.size * 1.35, m.size * 0.72, 0, 0, Math.PI * 2);
      ctx.fill();
      // 耳
      ctx.fillStyle = m.color;
      ctx.beginPath();
      ctx.arc(m.size * 0.7, -m.size * 0.55, m.size * 0.26, 0, Math.PI * 2);
      ctx.arc(m.size * 0.7, m.size * 0.55, m.size * 0.26, 0, Math.PI * 2);
      ctx.fill();
      // 头部
      ctx.fillStyle = m.color;
      ctx.beginPath();
      ctx.arc(m.size * 1.2, 0, m.size * 0.5, 0, Math.PI * 2);
      ctx.fill();
      // 鼻尖
      ctx.fillStyle = "#f0c9c3";
      ctx.beginPath();
      ctx.arc(m.size * 1.62, 0, m.size * 0.16, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }

    // 右上角：演示画面标注
    if (badge) {
      ctx.fillStyle = "rgba(255,255,255,0.55)";
      ctx.font = "11px 'SFMono-Regular', Consolas, monospace";
      ctx.textAlign = "right";
      ctx.textBaseline = "top";
      ctx.fillText(badge, W - 10, 8);
    }
    // 左下角：时间
    ctx.fillStyle = "rgba(255,255,255,0.7)";
    ctx.font = "12px 'SFMono-Regular', Consolas, monospace";
    ctx.textAlign = "left";
    ctx.textBaseline = "bottom";
    const tText = `${fmtT(t)} / ${fmtT(dur)}`;
    ctx.fillText(tText, 10, H - 8);
    ctx.fillStyle = "rgba(255,255,255,0.4)";
    ctx.font = "10px 'SFMono-Regular', Consolas, monospace";
    const fpsLabel = cur.fps && cur.fps > 0 ? `${cur.fps} fps` : "";
    ctx.fillText(fpsLabel, 10, H - 22);

    // 暂停遮罩
    if (!playing) {
      ctx.fillStyle = "rgba(0,0,0,0.28)";
      ctx.fillRect(0, 0, W, H);
      const cx = W / 2;
      const cy = H / 2;
      ctx.fillStyle = "rgba(255,255,255,0.85)";
      ctx.beginPath();
      ctx.moveTo(cx - 12, cy - 18);
      ctx.lineTo(cx - 12, cy + 18);
      ctx.lineTo(cx + 16, cy);
      ctx.closePath();
      ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,0.6)";
      ctx.font = "11px sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "bottom";
      ctx.fillText("演示画面 · 点击播放", cx, H - 26);
    }
  }, [mice]);

  // 播放时钟：rAF 推进内部时间，逐帧通知外部
  useEffect(() => {
    if (!playing) return;
    let raf = 0;
    let last = performance.now();
    const tick = (now: number) => {
      const { duration: dur, loop: lp, onTimeUpdate: otu, onTogglePlay: otp } = latest.current;
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      if (lp && lp.end > lp.start) {
        const span = lp.end - lp.start;
        tRef.current = lp.start + (((tRef.current - lp.start + dt) % span) + span) % span;
        otu(tRef.current);
      } else {
        const next = tRef.current + dt;
        if (next >= dur) {
          tRef.current = dur;
          draw();
          otp();
          return;
        }
        tRef.current = next;
        otu(tRef.current);
      }
      draw();
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing, draw]);

  // 暂停 / 外部 seek 时重绘
  useEffect(() => {
    draw();
  }, [draw, currentTime, playing, duration, seed, badge, loop?.start, loop?.end]);

  return (
    <canvas
      ref={canvasRef}
      className="demo-stage"
      role="img"
      aria-label={badge ? `${badge}：示意画面，非真实视频` : "顶视小鼠示意画面（演示模式）"}
      onClick={onTogglePlay}
      title="点击播放 / 暂停"
    />
  );
}
