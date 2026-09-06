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

  it("keeps the formal project ZIP export page", () => {
    expect(exportSource).toContain("开始导出 ZIP");
    expect(exportSource).toContain("createExport");
    expect(exportSource).toContain("handleExport");
    expect(apiSource).toContain("export function exportAnnotations");
  });
});
