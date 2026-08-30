import { describe, expect, it, vi } from "vitest";
import { createLatestCallback } from "./latestCallback";

describe("latest callback", () => {
  it("keeps one indirection while invoking only the latest callback", () => {
    const first = vi.fn();
    const second = vi.fn();
    const callback = createLatestCallback<(value: string) => void>(first);

    callback.invoke("first");
    callback.set(second);
    callback.invoke("second");
    callback.set(undefined);
    callback.invoke("ignored");

    expect(first).toHaveBeenCalledOnce();
    expect(first).toHaveBeenCalledWith("first");
    expect(second).toHaveBeenCalledOnce();
    expect(second).toHaveBeenCalledWith("second");
  });
});
