import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiRaw } = vi.hoisted(() => ({ apiRaw: vi.fn() }));
vi.mock("./client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./client")>()),
  apiRaw,
}));

import { fetchClipThumbnailUrl } from "./index";

describe("clip thumbnail API", () => {
  beforeEach(() => {
    apiRaw.mockReset();
    vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:thumbnail") });
  });

  it("requests by project and opaque clip identity", async () => {
    apiRaw.mockResolvedValue(new Response(new Blob(["image"]), { status: 200 }));

    await expect(fetchClipThumbnailUrl(12, 34)).resolves.toBe("blob:thumbnail");
    expect(apiRaw).toHaveBeenCalledWith("/projects/12/clips/34/thumbnail");
  });
});
