// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { type ConfirmOptions, useConfirm } from "./ConfirmDialog";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root;
let host: HTMLDivElement;
let openConfirm: ((options: ConfirmOptions) => Promise<boolean>) | null;

function Harness() {
  const [dialog, confirm] = useConfirm();
  openConfirm = confirm;
  return dialog;
}

beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  openConfirm = null;
  act(() => root.render(<Harness />));
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe("useConfirm keyboard boundaries", () => {
  it("confirms a non-dangerous dialog with Enter", async () => {
    let result: boolean | undefined;
    await act(async () => {
      void openConfirm!({ title: "普通确认", message: "继续？" }).then((value) => { result = value; });
    });

    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true }));
    });

    expect(result).toBe(true);
  });

  it("ignores Enter and Space, including repeats, for a dangerous dialog", async () => {
    let result: boolean | undefined;
    await act(async () => {
      void openConfirm!({ title: "危险确认", message: "删除？", danger: true }).then((value) => { result = value; });
    });

    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true }));
      window.dispatchEvent(new KeyboardEvent("keydown", { key: " ", code: "Space", bubbles: true }));
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true, repeat: true }));
      window.dispatchEvent(new KeyboardEvent("keydown", { key: " ", code: "Space", bubbles: true, repeat: true }));
    });
    expect(result).toBeUndefined();

    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }));
    });
    expect(result).toBe(false);
  });
});
