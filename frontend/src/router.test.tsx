// @vitest-environment jsdom
import { act } from "react";
import type { ReactNode } from "react";
import { useBlocker } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("./auth/AuthContext", () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
}));

vi.mock("./App", () => ({
  default: function BlockerProbe() {
    useBlocker(false);
    return <div>blocker rendered</div>;
  },
}));

describe("application router", () => {
  it("renders the application entry point with useBlocker inside the data router", async () => {
    document.body.innerHTML = '<div id="root"></div>';

    await act(async () => { await import("./main"); });

    expect(document.getElementById("root")?.textContent).toBe("blocker rendered");
  });
});
