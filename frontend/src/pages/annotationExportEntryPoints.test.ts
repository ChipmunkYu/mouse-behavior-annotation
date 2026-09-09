import { describe, expect, it } from "vitest";
import annotateSource from "./AnnotatePage.tsx?raw";
import exportSource from "./ExportPage.tsx?raw";
import apiSource from "../api/index.ts?raw";

describe("annotation and project export entry points", () => {
  it("does not expose the legacy single-video JSON export from AnnotatePage", () => {
    expect(annotateSource).not.toContain("导出 JSON");
    expect(annotateSource).not.toContain("handleExport");
    expect(annotateSource).not.toContain("exportAnnotations");
  });

  it("does not show the obsolete participant selection instruction", () => {
    expect(annotateSource).not.toContain("点击视频框或下方 track ID 选择参与对象");
    expect(annotateSource).toContain('selected.length ? <div className="selected-mice"');
    expect(annotateSource).not.toContain("Tab 切换模式；T 进入 track 列表导航；Space 播放；Ctrl+Enter 保存");
  });

  it("keeps rejection feedback inside the left workspace column and merges time context into the draft summary", () => {
    expect(annotateSource).toMatch(/<div className="annotate-body">\s*<section className="annotate-main">\s*\{behaviorReviewState\?\.feedback_items\.length \? \(/);
    expect(annotateSource.indexOf('className="behavior-feedback-panel"')).toBeLessThan(annotateSource.indexOf('className="card player-card"'));
    expect(annotateSource).toContain('className="draft-time-context"');
    expect(annotateSource).not.toContain('className="statusbar"');
    expect(annotateSource).not.toContain('className="time-display"');
  });

  it("keeps the formal project ZIP export page", () => {
    expect(exportSource).toContain("开始导出 ZIP");
    expect(exportSource).toContain("createExport");
    expect(exportSource).toContain("handleExport");
    expect(apiSource).toContain("export function exportAnnotations");
  });
});
