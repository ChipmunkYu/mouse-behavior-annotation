import { describe, expect, it } from "vitest";
import { buildIdentityEditFeedback, identityEditFeedbackForRoute } from "./AnnotatePage";

describe("track 修正反馈", () => {
  it("说明 Split 的新 Track ID 和 Merge 保留的 Track ID", () => {
    expect(buildIdentityEditFeedback("split", [12], 48, { new_display_track_id: 27 }))
      .toBe("Split 完成：Track 12 从帧 48 起拆分为新 Track 27，已自动选中。");
    expect(buildIdentityEditFeedback("merge", [12, 27, 31], 48, { retained_display_track_id: 12 }))
      .toBe("Merge 完成：保留 Track 12，已并入 2 个 track，已自动选中。");
  });

  it("服务端缺失 ID 时不伪造值", () => {
    expect(buildIdentityEditFeedback("split", [12], 48, {}))
      .toBe("Split 已完成，但服务端未返回新 Track ID。");
    expect(buildIdentityEditFeedback("merge", [12, 27], 48, {}))
      .toBe("Merge 已完成，但服务端未返回保留的 Track ID。");
  });

  it("仅保留当前项目和视频的反馈", () => {
    const feedback = { text: "旧视频反馈", key: 1, routeKey: "1:10" };
    expect(identityEditFeedbackForRoute(feedback, "1:10")).toBe(feedback);
    expect(identityEditFeedbackForRoute(feedback, "1:11")).toBeNull();
    expect(identityEditFeedbackForRoute(feedback, "2:10")).toBeNull();
  });
});
