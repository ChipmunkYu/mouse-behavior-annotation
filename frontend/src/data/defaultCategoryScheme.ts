import type { CategorySchemeCategoryInput, RoleDefinitionInput } from "../api/types";

const CATEGORY_COLORS = [
  "#E6194B",
  "#3CB44B",
  "#FFE119",
  "#4363D8",
  "#F58231",
  "#911EB4",
  "#46F0F0",
  "#F032E6",
  "#BCF60C",
  "#FABEBE",
  "#008080",
  "#E6BEFF",
] as const;

function role(name: string, roleSortOrder: number): RoleDefinitionInput {
  return {
    name,
    min_count: 1,
    max_count: 1,
    role_sort_order: roleSortOrder,
  };
}

function unorderedCategory(
  name: string,
  group: string,
  sortOrder: number,
  minCount: number,
  maxCount: number | null,
): CategorySchemeCategoryInput {
  return {
    name,
    group,
    color: CATEGORY_COLORS[sortOrder],
    sort_order: sortOrder,
    is_active: true,
    participant_mode: "unordered",
    role_definitions: [],
    mouse_count_min: minCount,
    mouse_count_max: maxCount,
  };
}

function roleBasedCategory(
  name: string,
  sortOrder: number,
  firstRole: string,
  secondRole: string,
): CategorySchemeCategoryInput {
  return {
    name,
    group: "社交行为",
    color: CATEGORY_COLORS[sortOrder],
    sort_order: sortOrder,
    is_active: true,
    participant_mode: "role_based",
    role_definitions: [role(firstRole, 0), role(secondRole, 1)],
  };
}

/** 返回 RFID-CV 行为标注规范的可编辑 12 类方案；每次调用均创建全新的类别和角色对象。 */
export function createDefaultCategoryScheme(): CategorySchemeCategoryInput[] {
  return [
    unorderedCategory("奔跑", "个体行为", 0, 1, 1),
    unorderedCategory("行走", "个体行为", 1, 1, 1),
    unorderedCategory("静止", "个体行为", 2, 1, 1),
    unorderedCategory("一起", "社交行为", 3, 2, 2),
    roleBasedCategory("主动接近/被接近", 4, "主动接近者", "被接近者"),
    roleBasedCategory("追逐/被追逐", 5, "追逐者", "被追逐者"),
    roleBasedCategory("回避/被回避", 6, "回避者", "被回避者"),
    unorderedCategory("攻击", "社交行为", 7, 2, 2),
    roleBasedCategory("鼻头接触", 8, "鼻部接触者", "头部被接触者"),
    roleBasedCategory("鼻尾接触", 9, "鼻部接触者", "尾部被接触者"),
    unorderedCategory("扎堆", "群体行为", 10, 3, null),
    unorderedCategory("孤立", "群体行为", 11, 1, 1),
  ];
}
