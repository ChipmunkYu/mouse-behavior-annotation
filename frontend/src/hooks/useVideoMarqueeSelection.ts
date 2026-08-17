import { useCallback, useEffect, useRef, useState, type Dispatch, type PointerEvent as ReactPointerEvent, type SetStateAction } from "react";

type SelectionMode = "replace" | "append" | "toggle";

export interface MarqueeRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

interface Gesture {
  pointerId: number;
  startPageX: number;
  startPageY: number;
  lastClientX: number;
  lastClientY: number;
  gridPageLeft: number;
  gridPageRight: number;
  snapshot: Set<number>;
  mode: SelectionMode;
  dragging: boolean;
}

interface Options {
  enabled: boolean;
  selected: Set<number>;
  setSelected: Dispatch<SetStateAction<Set<number>>>;
  onAnnounce: (message: string) => void;
}

const DRAG_THRESHOLD = 5;
const EDGE_ZONE = 56;

export function combineSelection(snapshot: Set<number>, hits: Set<number>, mode: SelectionMode): Set<number> {
  if (mode === "replace") return new Set(hits);
  const next = new Set(snapshot);
  hits.forEach((id) => {
    if (mode === "append") next.add(id);
    else if (next.has(id)) next.delete(id);
    else next.add(id);
  });
  return next;
}

function selectionMode(event: { shiftKey: boolean; ctrlKey: boolean; metaKey: boolean }): SelectionMode {
  if (event.ctrlKey || event.metaKey) return "toggle";
  if (event.shiftKey) return "append";
  return "replace";
}

function isExcludedTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return true;
  return Boolean(target.closest("button, a, input, select, textarea, label, [data-selection-interactive], [data-selection-copy], [data-video-selectable='false']"));
}

function supportsDesktopSelection(): boolean {
  return window.innerWidth > 760 && window.matchMedia("(pointer: fine)").matches;
}

export function useVideoMarqueeSelection({ enabled, selected, setSelected, onAnnounce }: Options) {
  const gridRef = useRef<HTMLDivElement>(null);
  const gestureRef = useRef<Gesture | null>(null);
  const selectedRef = useRef(selected);
  const animationRef = useRef<number | null>(null);
  const suppressClickRef = useRef(false);
  const [rect, setRect] = useState<MarqueeRect | null>(null);
  const [deviceAllowed, setDeviceAllowed] = useState(supportsDesktopSelection);
  const interactionEnabled = enabled && deviceAllowed;

  useEffect(() => { selectedRef.current = selected; }, [selected]);

  const stopAnimation = useCallback(() => {
    if (animationRef.current !== null) cancelAnimationFrame(animationRef.current);
    animationRef.current = null;
  }, []);

  const releaseCapture = useCallback((pointerId: number) => {
    const grid = gridRef.current;
    if (!grid?.hasPointerCapture(pointerId)) return;
    try { grid.releasePointerCapture(pointerId); } catch { /* pointer may already be gone */ }
  }, []);

  const finish = useCallback((restore: boolean, announce = true) => {
    const gesture = gestureRef.current;
    if (!gesture) return;
    if (restore) setSelected(new Set(gesture.snapshot));
    releaseCapture(gesture.pointerId);
    stopAnimation();
    gestureRef.current = null;
    setRect(null);
    document.body.classList.remove("marquee-selecting");
    if (announce) onAnnounce(restore ? "已取消框选" : `已选择 ${selectedRef.current.size} 个视频`);
  }, [onAnnounce, releaseCapture, setSelected, stopAnimation]);

  const updateSelection = useCallback(() => {
    const gesture = gestureRef.current;
    const grid = gridRef.current;
    if (!gesture?.dragging || !grid) return;

    const currentPageX = window.scrollX + gesture.lastClientX;
    const currentPageY = window.scrollY + gesture.lastClientY;
    const pageLeft = Math.min(gesture.startPageX, currentPageX);
    const pageTop = Math.min(gesture.startPageY, currentPageY);
    const pageRight = Math.max(gesture.startPageX, currentPageX);
    const pageBottom = Math.max(gesture.startPageY, currentPageY);
    setRect({
      left: pageLeft - window.scrollX,
      top: pageTop - window.scrollY,
      width: pageRight - pageLeft,
      height: pageBottom - pageTop,
    });

    const hits = new Set<number>();
    grid.querySelectorAll<HTMLElement>("[data-video-selectable='true'][data-video-id]").forEach((card) => {
      const bounds = card.getBoundingClientRect();
      const centerX = window.scrollX + bounds.left + bounds.width / 2;
      const centerY = window.scrollY + bounds.top + bounds.height / 2;
      if (centerX >= pageLeft && centerX <= pageRight && centerY >= pageTop && centerY <= pageBottom) {
        hits.add(Number(card.dataset.videoId));
      }
    });
    const next = combineSelection(gesture.snapshot, hits, gesture.mode);
    selectedRef.current = next;
    setSelected(next);
  }, [setSelected]);

  const startAutoScroll = useCallback(() => {
    if (animationRef.current !== null) return;
    const tick = () => {
      const gesture = gestureRef.current;
      if (!gesture?.dragging) { animationRef.current = null; return; }
      const y = gesture.lastClientY;
      let delta = 0;
      const pageX = window.scrollX + gesture.lastClientX;
      if (pageX >= gesture.gridPageLeft && pageX <= gesture.gridPageRight) {
        if (y < EDGE_ZONE) delta = -Math.ceil((EDGE_ZONE - Math.max(0, y)) / 4);
        else if (y > window.innerHeight - EDGE_ZONE) delta = Math.ceil((y - (window.innerHeight - EDGE_ZONE)) / 4);
      }
      if (delta !== 0) {
        window.scrollBy(0, Math.max(-18, Math.min(18, delta)));
        updateSelection();
      }
      animationRef.current = requestAnimationFrame(tick);
    };
    animationRef.current = requestAnimationFrame(tick);
  }, [updateSelection]);

  const onPointerDown = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.pointerType === "touch") {
      suppressClickRef.current = true;
      window.setTimeout(() => { suppressClickRef.current = false; }, 700);
      return;
    }
    if (!interactionEnabled || event.button !== 0 || !event.isPrimary || isExcludedTarget(event.target)) return;
    const grid = gridRef.current;
    if (!grid) return;
    suppressClickRef.current = false;
    const gridBounds = grid.getBoundingClientRect();
    gestureRef.current = {
      pointerId: event.pointerId,
      startPageX: window.scrollX + event.clientX,
      startPageY: window.scrollY + event.clientY,
      lastClientX: event.clientX,
      lastClientY: event.clientY,
      gridPageLeft: window.scrollX + gridBounds.left,
      gridPageRight: window.scrollX + gridBounds.right,
      snapshot: new Set(selectedRef.current),
      mode: selectionMode(event),
      dragging: false,
    };
    grid.setPointerCapture(event.pointerId);
  }, [interactionEnabled]);

  const onPointerMove = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    const gesture = gestureRef.current;
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    gesture.lastClientX = event.clientX;
    gesture.lastClientY = event.clientY;
    if (!gesture.dragging && Math.hypot(
      window.scrollX + event.clientX - gesture.startPageX,
      window.scrollY + event.clientY - gesture.startPageY,
    ) >= DRAG_THRESHOLD) {
      gesture.dragging = true;
      suppressClickRef.current = true;
      document.body.classList.add("marquee-selecting");
      startAutoScroll();
    }
    if (gesture.dragging) {
      event.preventDefault();
      updateSelection();
    }
  }, [startAutoScroll, updateSelection]);

  const onPointerUp = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    const gesture = gestureRef.current;
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    const wasDragging = gesture.dragging;
    finish(false, wasDragging);
    if (wasDragging) requestAnimationFrame(() => gridRef.current?.focus({ preventScroll: true }));
  }, [finish]);

  const onPointerCancel = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (gestureRef.current?.pointerId === event.pointerId) finish(true);
  }, [finish]);

  const selectCard = useCallback((id: number, event: { shiftKey: boolean; ctrlKey: boolean; metaKey: boolean }) => {
    if (!interactionEnabled) return;
    if (suppressClickRef.current) { suppressClickRef.current = false; return; }
    const mode = selectionMode(event);
    setSelected((old) => {
      if (mode === "replace") {
        const next = old.size === 1 && old.has(id) ? old : new Set([id]);
        onAnnounce("已选择 1 个视频");
        return next;
      }
      const next = new Set(old);
      if (mode === "append") next.add(id);
      else if (next.has(id)) next.delete(id);
      else next.add(id);
      onAnnounce(`已选择 ${next.size} 个视频`);
      return next;
    });
  }, [interactionEnabled, onAnnounce, setSelected]);

  useEffect(() => {
    const updateDevice = () => setDeviceAllowed(supportsDesktopSelection());
    const media = window.matchMedia("(pointer: fine)");
    window.addEventListener("resize", updateDevice);
    media.addEventListener("change", updateDevice);
    return () => {
      window.removeEventListener("resize", updateDevice);
      media.removeEventListener("change", updateDevice);
    };
  }, []);

  useEffect(() => {
    if (!interactionEnabled && gestureRef.current) finish(true);
  }, [finish, interactionEnabled]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && gestureRef.current) {
        event.preventDefault();
        finish(true);
        requestAnimationFrame(() => gridRef.current?.focus({ preventScroll: true }));
      }
    };
    const onBlur = () => { if (gestureRef.current) finish(true); };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("blur", onBlur);
      const gesture = gestureRef.current;
      if (gesture) releaseCapture(gesture.pointerId);
      gestureRef.current = null;
      stopAnimation();
      document.body.classList.remove("marquee-selecting");
    };
  }, [finish, releaseCapture, stopAnimation]);

  return {
    gridRef,
    rect,
    interactionEnabled,
    gridPointerHandlers: { onPointerDown, onPointerMove, onPointerUp, onPointerCancel },
    selectCard,
    cancelGesture: () => { if (gestureRef.current) finish(true, false); },
  };
}
