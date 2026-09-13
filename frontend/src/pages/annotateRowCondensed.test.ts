import { describe, expect, it } from "vitest";
import annotateSource from "./AnnotatePage.tsx?raw";

// 只截取非编辑态的行为行，避免草稿摘要等其他 "帧 / 审核" 文案干扰断言。
const rowSource = annotateSource.slice(
  annotateSource.indexOf("function AnnotationRow"),
  annotateSource.indexOf("function AnnotationEditForm"),
);

describe("精简行为行", () => {
  it("只保留行为身份：类别、时间、参与对象", () => {
    expect(rowSource).toContain('className="anno-cat"');
    expect(rowSource).toContain('className="anno-times"');
    expect(rowSource).toContain("<ParticipantSummary");
  });

  it("不再渲染帧号 / 标注者 / 可信度 / 审核状态元数据", () => {
    expect(rowSource).not.toContain("帧 {ann.start_frame}");
    expect(rowSource).not.toContain("标注者");
    expect(rowSource).not.toContain("可信度");
    expect(rowSource).not.toContain("审核");
    expect(rowSource).not.toContain("decisionStatus");
  });

  it("不用彩色底色强调已通过 / 需修改状态", () => {
    expect(rowSource).not.toContain("behavior-lock");
    expect(rowSource).not.toContain("behavior-rejected");
  });

  it("编辑 / 删除是显眼且可访问的原生按钮", () => {
    expect(rowSource).toContain('className="btn btn-sm"');
    expect(rowSource).toContain("btn btn-sm btn-danger");
    expect(rowSource).toContain("aria-label={`编辑行为：${label}`}");
    expect(rowSource).toContain("aria-label={`删除行为：${label}`}");
    expect(rowSource).toContain('type="button"');
  });

  it("保留待处理提示（角色待补全 / Track 失效）", () => {
    expect(rowSource).toContain("角色待补全");
    expect(rowSource).toContain("Track 已失效，需要重新分配");
    expect(rowSource).toContain("Track 已失效，需要重新选择");
  });
});
