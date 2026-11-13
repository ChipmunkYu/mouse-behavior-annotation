import { describe, expect, it } from "vitest";
import annotateSource from "./AnnotatePage.tsx?raw";
import exportSource from "./ExportPage.tsx?raw";
import apiSource from "../api/index.ts?raw";
import videosSource from "./VideosPage.tsx?raw";
import clipsSource from "./ClipsPage.tsx?raw";
import trackPanelsSource from "../components/AnnotationTrackPanels.tsx?raw";
import { DEFAULT_PAGE_SIZE } from "../api/types";

describe("library data source filter labels", () => {
  it.each([
    ["video-toolbar", videosSource, "videos-data-source"],
    ["clips-filters", clipsSource, "clips-data-source"],
  ])("keeps an accessible, unlabeled source select after search inside %s", (toolbar, source, id) => {
    const toolbarStart = source.indexOf(`className="${toolbar}"`);
    expect(toolbarStart).toBeGreaterThan(-1);
    const row = source.slice(toolbarStart, source.indexOf("</div>", toolbarStart));
    const searchIndex = row.indexOf('className="input search"');
    const sourceIndex = row.indexOf(`id="${id}"`);
    expect(searchIndex).toBeGreaterThan(-1);
    expect(sourceIndex).toBeGreaterThan(searchIndex);
    expect(row).not.toContain('<label');
    expect(row).not.toContain('className="field"');
    const field = row.slice(row.lastIndexOf("<select", sourceIndex), row.indexOf("</select>", sourceIndex));
    expect(field).toContain('aria-label="数据来源"');
    expect(field).toContain('className="select source-filter"');
    expect(field.match(/<option value="[^"]+">[^<]+<\/option>/g)).toEqual([
      '<option value="all">全部来源</option>',
      '<option value="manual">人工</option>',
      '<option value="heuristic">启发式</option>',
    ]);
  });

  it("keeps video workflow and assignee controls conditional on view, not row count", () => {
    const row = videosSource.slice(videosSource.indexOf('className="video-toolbar"'), videosSource.indexOf('className="visually-hidden"'));
    expect(row).toContain('{view === "unassigned" ? <span className="fixed-filter"');
    expect(row).toContain('{view === "all" ? <select className="select assignee-filter"');
    expect(row.indexOf('id="videos-data-source"')).toBeLessThan(row.indexOf('className="fixed-filter"'));
    expect(row).not.toMatch(/videos\?*\.|displayed\.|\.length/);
  });

  it("keeps clip filters and pagination with a fixed default page size", () => {
    const start = clipsSource.indexOf('className="clips-filters"');
    const row = clipsSource.slice(start, clipsSource.indexOf('</div>', start));
    expect(row.indexOf('className="select video-select"')).toBeGreaterThan(row.indexOf('id="clips-data-source"'));
    expect(clipsSource).not.toContain('aria-label="每页条数"');
    expect(clipsSource).not.toMatch(/pageSize|setPageSize/);
    expect(DEFAULT_PAGE_SIZE).toBe(20);
    expect(clipsSource.match(/page_size: DEFAULT_PAGE_SIZE/g)).toHaveLength(2);
    expect(clipsSource).toContain('← 上一页');
    expect(clipsSource).toContain('下一页 →');
  });

});

describe("annotation and project export entry points", () => {
  it("does not expose the legacy single-video JSON export from AnnotatePage", () => {
    expect(annotateSource).not.toContain("导出 JSON");
    expect(annotateSource).not.toContain("handleExport");
    expect(annotateSource).not.toContain("exportAnnotations");
  });

  it("does not show the obsolete participant selection instruction", () => {
    expect(annotateSource).not.toContain("点击视频框或下方 track ID 选择参与对象");
    expect(trackPanelsSource).toContain('selected.length ? <div className="selected-mice"');
    expect(annotateSource).toContain('from "../components/AnnotationTrackPanels"');
    expect(annotateSource).not.toContain("Tab 切换模式；T 进入 track 列表导航；Space 播放；Ctrl+Enter 保存");
  });

  it("keeps rejection feedback before the player in annotate-main, independently scrolling", () => {
    const mainIndex = annotateSource.indexOf('className="annotate-main"');
    const panelIndex = annotateSource.indexOf("<RejectionFeedbackPanel");
    const playerIndex = annotateSource.indexOf('className="card player-card"');
    expect(mainIndex).toBeGreaterThan(-1);
    expect(panelIndex).toBeGreaterThan(mainIndex);
    expect(playerIndex).toBeGreaterThan(panelIndex);
    expect(annotateSource).toContain("focusedAnnotationId={selectedAnnotationId}");
    expect(annotateSource).toContain("标记已修改");
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
