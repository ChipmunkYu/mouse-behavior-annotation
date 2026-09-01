// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { resolveAnnotateEscapeAction } from "./AnnotatePage";
import { handleClipFrameShortcut, stepClipFrame } from "./ClipsPage";

describe("Gate 1 shortcut regressions", () => {
  it("does not reset or confirm a draft when Escape has no cancellation target", () => {
    const resetDraft = vi.fn();
    const confirm = vi.fn();

    const action = resolveAnnotateEscapeAction({
      shortcutHelpOpen: false,
      editingAnnotationId: null,
      participantNavigationActive: false,
      identityNavigationActive: false,
      hasDraft: true,
    });
    if (action) resetDraft();

    expect(action).toBeNull();
    expect(resetDraft).not.toHaveBeenCalled();
    expect(confirm).not.toHaveBeenCalled();
  });

  it("steps exactly one frame left and right", () => {
    const step = vi.fn();
    const left = new KeyboardEvent("keydown", { code: "ArrowLeft", cancelable: true });
    const right = new KeyboardEvent("keydown", { code: "ArrowRight", cancelable: true });
    const repeat = new KeyboardEvent("keydown", { code: "ArrowRight", cancelable: true, repeat: true });
    const stopLeft = vi.spyOn(left, "stopPropagation");
    const stopRight = vi.spyOn(right, "stopPropagation");

    expect(handleClipFrameShortcut(left, step)).toBe(true);
    expect(handleClipFrameShortcut(right, step)).toBe(true);
    expect(handleClipFrameShortcut(repeat, step)).toBe(true);
    expect(step.mock.calls).toEqual([[-1], [1]]);
    expect(left.defaultPrevented).toBe(true);
    expect(right.defaultPrevented).toBe(true);
    expect(repeat.defaultPrevented).toBe(true);
    expect(stopLeft).toHaveBeenCalledOnce();
    expect(stopRight).toHaveBeenCalledOnce();
    expect(stepClipFrame(2, -1, 25, 10)).toBe(1.96);
    expect(stepClipFrame(2, 1, 25, 10)).toBe(2.04);
    expect(stepClipFrame(0, -1, 25, 10)).toBe(0);
    expect(stepClipFrame(10, 1, 25, 10)).toBe(10);
  });
});
