// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { getBehaviorStats } from ".";

afterEach(() => vi.restoreAllMocks());

describe("behavior stats API wiring", () => {
  it("requests the project behavior-stats endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ items: [] }), { status: 200, headers: { "Content-Type": "application/json" } }));
    const result = await getBehaviorStats(5);
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/projects\/5\/behavior-stats$/);
    expect(result).toEqual({ items: [] });
  });
});
