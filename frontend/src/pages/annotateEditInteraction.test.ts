import { describe, expect, it } from "vitest";
import type { Category } from "../api/types";
import annotateSource from "./AnnotatePage.tsx?raw";
import {
  buildAnnotationEditPatch,
  isEditingAnnotationShortcutAllowed,
  parseMouseIdList,
} from "./AnnotatePage";

function category(overrides: Partial<Category> = {}): Category {
  return {
    id: 1,
    name: "追逐",
    group: "社交",
    color: null,
    is_active: true,
    mouse_count_min: 1,
    mouse_count_max: null,
    participant_mode: "unordered",
    role_definitions: [],
    sort_order: 0,
    project_id: 1,
    ...overrides,
  } as Category;
}

function shortcut(
  code: string,
  modifiers: Partial<{ ctrlKey: boolean; altKey: boolean; metaKey: boolean; shiftKey: boolean }> = {},
) {
  return { code, ctrlKey: false, altKey: false, metaKey: false, shiftKey: false, ...modifiers };
}

describe("编辑退回行为时的快捷键放行", () => {
  it("放行类别数字键、S/D、T、参与对象导航、视频传输与 Ctrl+Enter", () => {
    expect(isEditingAnnotationShortcutAllowed(shortcut("Digit3"))).toBe(true);
    expect(isEditingAnnotationShortcutAllowed(shortcut("Numpad9"))).toBe(true);
    expect(isEditingAnnotationShortcutAllowed(shortcut("KeyS"))).toBe(true);
    expect(isEditingAnnotationShortcutAllowed(shortcut("KeyD"))).toBe(true);
    expect(isEditingAnnotationShortcutAllowed(shortcut("KeyT"))).toBe(true);
    expect(isEditingAnnotationShortcutAllowed(shortcut("Space"))).toBe(true);
    expect(isEditingAnnotationShortcutAllowed(shortcut("ArrowLeft"))).toBe(true);
    expect(isEditingAnnotationShortcutAllowed(shortcut("ArrowRight", { shiftKey: true }))).toBe(true);
    expect(isEditingAnnotationShortcutAllowed(shortcut("ArrowUp"))).toBe(true);
    expect(isEditingAnnotationShortcutAllowed(shortcut("ArrowDown"))).toBe(true);
    expect(isEditingAnnotationShortcutAllowed(shortcut("Enter"))).toBe(true);
    expect(isEditingAnnotationShortcutAllowed(shortcut("Enter", { ctrlKey: true }))).toBe(true);
    expect(isEditingAnnotationShortcutAllowed(shortcut("Comma", { shiftKey: true }))).toBe(true);
    expect(isEditingAnnotationShortcutAllowed(shortcut("Period", { shiftKey: true }))).toBe(true);
  });

  it("仍然抑制删除、撤销、帮助等会破坏编辑的快捷键", () => {
    expect(isEditingAnnotationShortcutAllowed(shortcut("Delete"))).toBe(false);
    expect(isEditingAnnotationShortcutAllowed(shortcut("KeyZ", { ctrlKey: true }))).toBe(false);
    expect(isEditingAnnotationShortcutAllowed(shortcut("Slash", { shiftKey: true }))).toBe(false);
    expect(isEditingAnnotationShortcutAllowed(shortcut("KeyQ"))).toBe(false);
    expect(isEditingAnnotationShortcutAllowed(shortcut("Digit3", { ctrlKey: true }))).toBe(false);
    expect(isEditingAnnotationShortcutAllowed(shortcut("KeyS", { shiftKey: true }))).toBe(false);
  });
});

describe("参与对象输入解析", () => {
  it("支持逗号 / 中文逗号 / 空白分隔并去重排序", () => {
    expect(parseMouseIdList("3, 1，2 1")).toEqual([1, 2, 3]);
  });

  it("忽略非整数并允许空输入", () => {
    expect(parseMouseIdList("1, abc, 2.5")).toEqual([1]);
    expect(parseMouseIdList("")).toEqual([]);
  });
});

describe("编辑行为 PATCH 构造", () => {
  it("直接键入的帧变化进入补丁，且不重复创建草稿字段", () => {
    const result = buildAnnotationEditPatch({
      category: category(),
      startFrame: 12,
      endFrame: 34,
      mouseIds: [2, 1],
      fps: 25,
      frameCount: 100,
      detectionImportRevision: 5,
      identityRevision: 7,
    });
    expect("patch" in result).toBe(true);
    if (!("patch" in result)) return;
    expect(result.patch.start_frame).toBe(12);
    expect(result.patch.end_frame).toBe(34);
    expect(result.patch.mouse_ids).toEqual([1, 2]);
    expect(result.patch).not.toHaveProperty("participant_roles");
    expect(result.patch.detection_import_revision).toBe(5);
    expect(result.patch.identity_revision).toBe(7);
  });

  it("角色类别不写 mouse_ids，交由共享角色分配补齐", () => {
    const result = buildAnnotationEditPatch({
      category: category({ participant_mode: "role_based" }),
      startFrame: 1,
      endFrame: 5,
      mouseIds: [3],
      fps: 25,
      frameCount: 100,
      detectionImportRevision: 1,
      identityRevision: 1,
    });
    expect("patch" in result).toBe(true);
    if (!("patch" in result)) return;
    expect(result.patch).not.toHaveProperty("mouse_ids");
  });

  it("拒绝非法帧区间、越界与无效 FPS", () => {
    const base = { category: category(), mouseIds: [], fps: 25, frameCount: 100, detectionImportRevision: 1, identityRevision: 1 };
    expect(buildAnnotationEditPatch({ ...base, startFrame: 10, endFrame: 10 })).toEqual({ error: "结束帧必须大于开始帧，单帧行为不能保存" });
    expect(buildAnnotationEditPatch({ ...base, startFrame: 0, endFrame: 200 })).toEqual({ error: "结束帧不能超过最后一帧 99" });
    expect(buildAnnotationEditPatch({ ...base, startFrame: 1, endFrame: 5, fps: null })).toEqual({ error: "视频 FPS 无效，无法由帧派生时间" });
    expect(buildAnnotationEditPatch({ ...base, startFrame: null, endFrame: 5 })).toEqual({ error: "请设置开始帧和结束帧" });
  });
});

describe("编辑表单受控于共享编辑状态", () => {
  it("表单不再持有独立的类别 / 帧本地状态", () => {
    expect(annotateSource).not.toContain("useState(ann.category_id)");
    expect(annotateSource).not.toContain("useState(String(ann.start_frame))");
    expect(annotateSource).not.toContain("useState(String(ann.end_frame))");
    expect(annotateSource).not.toContain("setCategoryId");
  });

  it("帧输入与类别下拉由共享状态驱动", () => {
    expect(annotateSource).toContain('value={activeCategory?.id ?? ""}');
    expect(annotateSource).toContain('value={sNum ?? ""}');
    expect(annotateSource).toContain('value={eNum ?? ""}');
  });

  it("进入编辑时把标注同步进共享状态（类别 / 起止帧 / 参与对象）", () => {
    expect(annotateSource).toContain("setStartPoint({ frame: annotation.start_frame })");
    expect(annotateSource).toContain("setEndPoint({ frame: annotation.end_frame })");
    expect(annotateSource).toContain("setSelectedMouseIds(annotation.mouse_ids)");
  });

  it("编辑态用白名单放行，而不是整段删除抑制", () => {
    expect(annotateSource).toContain("if (editingAnnotationId != null && !isEditingAnnotationShortcutAllowed(e)) {");
  });

  it("Ctrl+Enter 与主保存按钮在编辑中走编辑保存（PATCH），不新建草稿", () => {
    expect(annotateSource).toContain("void saveCurrentBehavior();");
    const saveEditing = annotateSource.slice(
      annotateSource.indexOf("async function saveEditingAnnotation"),
      annotateSource.indexOf("async function runIdentityEdit"),
    );
    expect(saveEditing).toContain("handleEditSave(id, result.patch)");
    expect(saveEditing).not.toContain("createAnnotation");
    const saveCurrent = annotateSource.slice(
      annotateSource.indexOf("function saveCurrentBehavior"),
      annotateSource.indexOf("async function runIdentityEdit"),
    );
    expect(saveCurrent).toContain("if (editingAnnotationId != null) void saveEditingAnnotation();");
    expect(saveCurrent).toContain("else void saveDraft();");
  });

  it("退回行为点击只定位选中，不自动进入编辑", () => {
    const focusFn = annotateSource.slice(
      annotateSource.indexOf("function focusFeedbackAnnotation"),
      annotateSource.indexOf("async function handleMarkFeedbackModified"),
    );
    expect(focusFn).toContain("setSelectedAnnotationId(resolveFeedbackSelection(selectedAnnotationId, target.annotationId))");
    expect(focusFn).not.toContain("changeEditingAnnotation");
    expect(focusFn).not.toContain("setEditingAnnotationId");
  });
});
