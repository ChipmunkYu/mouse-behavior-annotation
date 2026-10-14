import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiFetch, operationalErrorDetail } from "./client";
import { actionError, deleteError } from "../pages/VideosPage";
import { friendlyError } from "../pages/ProjectManagementPage";

const structured = (code: string, message = "server message") =>
  new ApiError(409, message, { detail: message, code, context: { video_id: 7 } }, code, { video_id: 7 });
const nested = (code: string, message = "server message") =>
  new ApiError(409, message, { detail: { code, message, context: { video_id: 7 } } });

describe("stable operational errors", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("reads structured details and rejects legacy/non-ApiError values", () => {
    expect(operationalErrorDetail(structured("video_release_not_allowed"))).toEqual({
      code: "video_release_not_allowed", message: "server message", context: { video_id: 7 },
    });
    expect(structured("video_release_not_allowed").detail).toEqual({
      detail: "server message", code: "video_release_not_allowed", context: { video_id: 7 },
    });
    expect(operationalErrorDetail(nested("video_release_not_allowed"))).toEqual({
      code: "video_release_not_allowed", message: "server message", context: { video_id: 7 },
    });
    expect(operationalErrorDetail(new ApiError(409, "legacy", { detail: "legacy" }))).toBeNull();
    expect(operationalErrorDetail(new Error("plain"))).toBeNull();
  });

  it("preserves response details while exposing top-level operational metadata", async () => {
    const detail = { message: "validation failed", items: [1], errors: ["bad row"], conflicts: [{ id: 2 }] };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail }), { status: 422 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        detail: "release denied", code: "video_release_not_allowed", context: { video_id: 7 },
      }), { status: 409 }));
    vi.stubGlobal("localStorage", { getItem: () => null });
    vi.stubGlobal("fetch", fetchMock);

    const validationError = await apiFetch("/validation").catch((error: unknown) => error);
    expect(validationError).toBeInstanceOf(ApiError);
    expect((validationError as ApiError).detail).toEqual({ detail });
    expect((validationError as ApiError).detail).toMatchObject({
      detail: { items: [1], errors: ["bad row"], conflicts: [{ id: 2 }] },
    });

    const operationalError = await apiFetch("/release").catch((error: unknown) => error);
    expect(operationalError).toBeInstanceOf(ApiError);
    expect((operationalError as ApiError).detail).toEqual({
      detail: "release denied", code: "video_release_not_allowed", context: { video_id: 7 },
    });
    expect(operationalErrorDetail(operationalError)).toEqual({
      code: "video_release_not_allowed", message: "release denied", context: { video_id: 7 },
    });
  });

  it("maps only known page codes and preserves unknown/string fallback", () => {
    expect(actionError(structured("video_release_not_allowed"))).toContain("当前负责人");
    expect(actionError(structured("video_assignment_conflict"))).toContain("批量改派");
    expect(deleteError(structured("video_delete_workflow_conflict"))).toContain("视频状态");
    expect(deleteError(structured("video_delete_related_job_conflict"))).toContain("关联任务");
    expect(deleteError(structured("video_delete_cleanup_incomplete"))).toContain("未完整结束");
    expect(friendlyError(structured("project_owner_protected"))).toContain("所有者");
    expect(friendlyError(structured("member_still_assigned"))).toContain("仍负责视频");
    expect(actionError(structured("future_code", "unchanged"))).toBe("unchanged");
    expect(friendlyError(new ApiError(409, "legacy string", { detail: "legacy string" }))).toBe("legacy string");
  });
});
