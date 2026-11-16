// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { checkIdentityEdit, commitIdentityEdit, createExport, createImportBatch, fetchExportDownload, getExportStatus, getJudgment, getVideo, listClipCategories, listClips, listVideos, putJudgment, revertIdentityEdit, submitVideoForReview, withdrawVideo } from ".";
import type { JudgmentVersions } from "./types";

afterEach(() => vi.restoreAllMocks());
const versions: JudgmentVersions = { expected_annotation_revision: 3, base_detection_import_revision: 1, base_identity_revision: 2, expected_submission_id: 9, expected_decision_revision: 4 };
function mockFetch() { return vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify({ id: 1 }), { status: 200, headers: { "Content-Type": "application/json" } })); }

describe("启发式请求契约", () => {
  it("历史详情直接读取项目内单视频，不从列表推断存在性", async () => {
    const fetch = mockFetch();
    await getVideo(2, 49);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toMatch(/\/projects\/2\/videos\/49$/);
  });

  it("显式 batch source、判断和生命周期 CAS 不混用旧 annotation 写接口", async () => {
    const fetch = mockFetch();
    await createImportBatch(2, undefined, "heuristic");
    await getJudgment(2, 4);
    await putJudgment(2, 4, { judgment_status: "usable", category_id: 7, participant_roles: { role_real: [31] }, ...versions });
    await submitVideoForReview(2, 4, versions);
    await withdrawVideo(2, 4, { expected_submission_id: 9, expected_decision_revision: 4 });
    expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual({ source: "heuristic" });
    expect(fetch.mock.calls[1][0]).toMatch(/\/videos\/4\/judgment$/);
    expect(JSON.parse(String(fetch.mock.calls[2][1]?.body))).toEqual({ judgment_status: "usable", category_id: 7, participant_roles: { role_real: [31] }, ...versions });
    expect(JSON.parse(String(fetch.mock.calls[3][1]?.body))).toEqual(versions);
    expect(JSON.parse(String(fetch.mock.calls[4][1]?.body))).toEqual({ expected_submission_id: 9, expected_decision_revision: 4 });
  });

  it("merge/split/check/revert 传播判断版本和显式参与确认", async () => {
    const fetch = mockFetch();
    const input = { operation: "split" as const, track_ids: [12], frame: 30, ...versions };
    const confirmed_selection = { category_id: 7, mouse_ids: [12, 32], participant_roles: {} };
    await checkIdentityEdit(2, 4, input);
    await commitIdentityEdit(2, 4, { ...input, confirmed_selection });
    await revertIdentityEdit(2, 4, 5, versions);
    expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual(input);
    expect(JSON.parse(String(fetch.mock.calls[1][1]?.body))).toEqual({ ...input, confirmed_selection });
    expect(JSON.parse(String(fetch.mock.calls[2][1]?.body))).toEqual(versions);
  });

  it("两个库来源与其他条件 AND；导出默认 manual 且三接口同 scope", async () => {
    const fetch = mockFetch();
    await listVideos(2, { view: "mine", source: "heuristic", workflow_status: "draft", assignee_membership_id: 8 });
    await listClips(2, { source: "manual", category_id: 7, video_id: 4, search: "mouse", page: 2, page_size: 20 });
    await listClipCategories(2, "heuristic");
    await createExport(2, {});
    await getExportStatus(2);
    await fetchExportDownload(2);
    await createExport(2, { source: "heuristic", category_ids: [7] });
    await getExportStatus(2, "heuristic");
    await fetchExportDownload(2, "heuristic");
    const videoQuery = new URL(String(fetch.mock.calls[0][0]), "http://test").searchParams;
    expect(Object.fromEntries(videoQuery)).toEqual({ source: "heuristic", view: "mine", workflow_status: "draft", assignee_membership_id: "8" });
    expect(Object.fromEntries(new URL(String(fetch.mock.calls[1][0]), "http://test").searchParams)).toEqual({ source: "manual", category_id: "7", video_id: "4", search: "mouse", page: "2", page_size: "20" });
    expect(fetch.mock.calls[2][0]).toMatch(/categories\?source=heuristic$/);
    expect(JSON.parse(String(fetch.mock.calls[3][1]?.body))).toEqual({ source: "manual" });
    expect(fetch.mock.calls[4][0]).toMatch(/status\?source=manual$/);
    expect(fetch.mock.calls[5][0]).toMatch(/download\?source=manual$/);
    expect(JSON.parse(String(fetch.mock.calls[6][1]?.body))).toEqual({ source: "heuristic", category_ids: [7] });
    expect(fetch.mock.calls[7][0]).toMatch(/status\?source=heuristic$/);
    expect(fetch.mock.calls[8][0]).toMatch(/download\?source=heuristic$/);
  });
});
