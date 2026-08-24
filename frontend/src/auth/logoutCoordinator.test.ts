import { describe, expect, it, vi } from "vitest";
import { createLogoutCoordinator } from "./logoutCoordinator";

describe("logout coordinator", () => {
  it("shares one in-flight request and uses the direct same-origin endpoint", async () => {
    let resolve!: (response: Response) => void;
    const fetcher = vi.fn(() => new Promise<Response>((done) => { resolve = done; }));
    const logout = createLogoutCoordinator(fetcher, "/api");
    const first = logout();
    const second = logout();
    expect(first).toBe(second);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledWith("/api/auth/logout", {
      method: "POST",
      credentials: "same-origin",
    });
    resolve(new Response(null, { status: 204 }));
    await expect(first).resolves.toEqual({ accepted: true });
  });

  it("returns failure without throwing and resets after settlement", async () => {
    const fetcher = vi.fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(new Response(null, { status: 401 }));
    const logout = createLogoutCoordinator(fetcher, "/api");
    await expect(logout()).resolves.toEqual({ accepted: false });
    await expect(logout()).resolves.toEqual({ accepted: false });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("does not recurse when the logout endpoint itself returns unauthorized", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 401 }));
    const logout = createLogoutCoordinator(fetcher, "/api");
    await logout();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("waits only for the current in-flight logout", async () => {
    let resolve!: (response: Response) => void;
    const fetcher = vi.fn(() => new Promise<Response>((done) => { resolve = done; }));
    const logout = createLogoutCoordinator(fetcher, "/api");

    await logout.waitForInFlight();
    expect(fetcher).not.toHaveBeenCalled();

    const request = logout();
    let waitSettled = false;
    const wait = logout.waitForInFlight().then(() => { waitSettled = true; });
    await Promise.resolve();
    expect(waitSettled).toBe(false);
    expect(fetcher).toHaveBeenCalledTimes(1);

    resolve(new Response(null, { status: 204 }));
    await Promise.all([request, wait]);
    expect(waitSettled).toBe(true);

    await logout.waitForInFlight();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
