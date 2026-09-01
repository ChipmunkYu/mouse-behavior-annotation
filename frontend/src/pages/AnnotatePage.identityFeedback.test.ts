import { describe, expect, it } from "vitest";
import type { CorrectedTrackSummary } from "../api/types";
import { buildIdentityEditFeedback, identityEditFeedbackForRoute, mergePinnedTracks } from "./AnnotatePage";

const track = (display_track_id: number): CorrectedTrackSummary => ({
  display_track_id,
  first_frame: 0,
  last_frame: 10,
  detection_count: 11,
  visible_in_current_frame: false,
});

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

describe("track 修正列表置顶", () => {
  it("补入基础 200 条外的已选 track 并按选择顺序置顶", () => {
    expect(mergePinnedTracks([track(1), track(2)], [202, 201], [track(201), track(202)]).map((item) => item.display_track_id))
      .toEqual([202, 201, 1, 2]);
  });

  it("基础结果已有的已选 track 只保留一次", () => {
    expect(mergePinnedTracks([track(1), track(2), track(3)], [2], [track(2)]).map((item) => item.display_track_id))
      .toEqual([2, 1, 3]);
  });

  it("补取返回 substring 匹配时只合并 exact track", () => {
    expect(mergePinnedTracks([track(1)], [20], [track(120), track(20), track(201)]).map((item) => item.display_track_id))
      .toEqual([20, 1]);
  });
});
