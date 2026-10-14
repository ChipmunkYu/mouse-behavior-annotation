import { describe, expect, it } from "vitest";
import { ApiError, operationalErrorDetail } from "./client";
import { actionError, deleteError } from "../pages/VideosPage";
import { friendlyError } from "../pages/ProjectManagementPage";

const structured = (code: string, message = "server message") =>
  new ApiError(409, message, { detail: { code, message, context: { video_id: 7 } } });

describe("stable operational errors", () => {
  it("reads structured details and rejects legacy/non-ApiError values", () => {
    expect(operationalErrorDetail(structured("video_release_not_allowed"))).toEqual({
      code: "video_release_not_allowed", message: "server message", context: { video_id: 7 },
    });
    expect(operationalErrorDetail(new ApiError(409, "legacy", { detail: "legacy" }))).toBeNull();
    expect(operationalErrorDetail(new Error("plain"))).toBeNull();
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
