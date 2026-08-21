import { useMemo } from "react";
import type { CategorySchemeCategoryInput, RoleDefinitionInput } from "../api/types";
import { EmptyState } from "./ui";

export interface CategorySchemeValidation {
  valid: boolean;
  completeCount: number;
  issues: string[];
  categoryIssues: string[][];
}

export function normalizeCategorySchemeDraft(categories: CategorySchemeCategoryInput[]): CategorySchemeCategoryInput[] {
  return categories.map((category, index) => ({
    ...category,
    sort_order: index,
    role_definitions: category.role_definitions.map((role, roleIndex) => ({
      ...role,
      role_sort_order: roleIndex,
    })),
  }));
}

export function toCategorySchemeRequestCategories(categories: CategorySchemeCategoryInput[]): CategorySchemeCategoryInput[] {
  return normalizeCategorySchemeDraft(categories).map((category) => {
    const common = {
      ...(category.id == null ? {} : { id: category.id }),
      name: category.name.trim(),
      group: category.group.trim(),
      color: category.color,
      sort_order: category.sort_order,
      is_active: category.is_active,
      participant_mode: category.participant_mode,
    };
    if (category.participant_mode === "role_based") {
      return {
        ...common,
        participant_mode: "role_based" as const,
        role_definitions: category.role_definitions.map((role) => ({
          ...(role.key == null ? {} : { key: role.key }),
          name: role.name.trim(),
          min_count: role.min_count,
          max_count: role.max_count,
          role_sort_order: role.role_sort_order,
        })),
      };
    }
    return {
      ...common,
      participant_mode: "unordered" as const,
      role_definitions: [],
      mouse_count_min: category.mouse_count_min,
      mouse_count_max: category.mouse_count_max,
    };
  });
}

export function validateCategoryScheme(
  categories: CategorySchemeCategoryInput[],
  { requireColor = false }: { requireColor?: boolean } = {},
): CategorySchemeValidation {
  const normalized = normalizeCategorySchemeDraft(categories);
  const categoryIssues = normalized.map((category) => {
    const issues: string[] = [];
    if (!category.name.trim()) issues.push("填写类别名称");
    if (!category.group.trim()) issues.push("填写分组");
    if (requireColor && !category.color) issues.push("选择标识色");
    if (category.participant_mode === "unordered") {
      const min = category.mouse_count_min;
      const max = category.mouse_count_max;
      if (!Number.isInteger(min) || (min ?? 0) < 1) issues.push("最少对象须为不小于 1 的整数");
      if (max != null && (!Number.isInteger(max) || max < (min ?? 1))) issues.push("最多对象须为空或不小于最少对象");
    } else {
      if (category.role_definitions.length === 0) issues.push("至少添加一个角色");
      const roleNames = new Set<string>();
      let totalMin = 0;
      category.role_definitions.forEach((role, roleIndex) => {
        const name = role.name.trim();
        if (!name) issues.push(`填写角色 ${roleIndex + 1} 名称`);
        else if (roleNames.has(name.toLocaleLowerCase())) issues.push("角色名称不能重复");
        else roleNames.add(name.toLocaleLowerCase());
        if (!Number.isInteger(role.min_count) || role.min_count < 0) issues.push(`${name || `角色 ${roleIndex + 1}`}的最少数量须为非负整数`);
        else totalMin += role.min_count;
        if (role.max_count != null && (!Number.isInteger(role.max_count) || role.max_count < role.min_count)) issues.push(`${name || `角色 ${roleIndex + 1}`}的最多数量须为空或不小于最少数量`);
      });
      if (category.role_definitions.length > 0 && totalMin < 1) issues.push("所有角色的最少数量之和须至少为 1");
    }
    return [...new Set(issues)];
  });
  const foldedNames = new Map<string, number[]>();
  normalized.forEach((category, index) => {
    const name = category.name.trim().toLocaleLowerCase();
    if (name) foldedNames.set(name, [...(foldedNames.get(name) ?? []), index]);
  });
  foldedNames.forEach((indices) => {
    if (indices.length > 1) indices.forEach((index) => categoryIssues[index].push("类别名称不能重复"));
  });
  const issues = normalized.length === 0
    ? ["至少主动添加并完成一个行为类别"]
    : categoryIssues.flatMap((rows, index) => rows.map((issue) => `类别 ${index + 1}：${issue}`));
  return {
    valid: normalized.length > 0 && issues.length === 0,
    completeCount: categoryIssues.filter((rows) => rows.length === 0).length,
    issues,
    categoryIssues,
  };
}

export default function CategorySchemeEditor({
  value,
  onChange,
  disabled = false,
  requireColor = false,
  emptyHint = "新增至少一个类别后继续。",
  showCompleteness = true,
}: {
  value: CategorySchemeCategoryInput[];
  onChange: (categories: CategorySchemeCategoryInput[]) => void;
  disabled?: boolean;
  requireColor?: boolean;
  emptyHint?: string;
  showCompleteness?: boolean;
}) {
  const normalized = useMemo(() => normalizeCategorySchemeDraft(value), [value]);
  const validation = useMemo(() => validateCategoryScheme(normalized, { requireColor }), [normalized, requireColor]);
  const patchCategory = (index: number, patch: Partial<CategorySchemeCategoryInput>) => onChange(normalized.map((row, rowIndex) => rowIndex === index ? { ...row, ...patch } : row));
  const patchRole = (categoryIndex: number, roleIndex: number, patch: Partial<RoleDefinitionInput>) => patchCategory(categoryIndex, {
    role_definitions: normalized[categoryIndex].role_definitions.map((role, index) => index === roleIndex ? { ...role, ...patch } : role),
  });
  const moveCategory = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= normalized.length) return;
    const next = [...normalized];
    [next[index], next[target]] = [next[target], next[index]];
    onChange(normalizeCategorySchemeDraft(next));
  };
  const addCategory = () => onChange([...normalized, {
    name: "", group: "", color: "#2f6fed", sort_order: normalized.length,
    is_active: true, participant_mode: "unordered", role_definitions: [],
    mouse_count_min: 1, mouse_count_max: null,
  }]);

  return <div className="scheme-editor-control">
    {showCompleteness ? <div className={`scheme-completeness ${validation.valid ? "complete" : "incomplete"}`} role="status">
      <b>{validation.valid ? "✓ 类别方案已完整" : `还需完成 ${Math.max(1, normalized.length - validation.completeCount)} 个类别`}</b>
      <span>{normalized.length ? `已完成 ${validation.completeCount} / ${normalized.length}` : "尚未添加类别"}</span>
      {!validation.valid ? <span>{validation.issues.slice(0, 3).join("；")}{validation.issues.length > 3 ? `；另有 ${validation.issues.length - 3} 项` : ""}</span> : <span>创建后方案保持未锁定，可在项目管理中最终复核。</span>}
    </div> : null}
    <div className="scheme-category-list">
      {normalized.map((category, index) => <section className={`scheme-category${validation.categoryIssues[index]?.length ? " has-error" : ""}`} key={category.id ?? `new-${index}`}>
        <div className="scheme-category-head"><span className="scheme-order">{index + 1}</span><b>{category.name || "未命名类别"}</b><span className="flex-spacer" />{!disabled ? <><button type="button" className="btn btn-sm" disabled={index === 0} onClick={() => moveCategory(index, -1)} aria-label="类别上移">↑ 上移</button><button type="button" className="btn btn-sm" disabled={index === normalized.length - 1} onClick={() => moveCategory(index, 1)} aria-label="类别下移">↓ 下移</button><button type="button" className="btn btn-sm btn-danger" onClick={() => onChange(normalized.filter((_, rowIndex) => rowIndex !== index))}>删除类别</button></> : null}</div>
        <div className="scheme-fields"><label>类别名称<input className="input" maxLength={64} value={category.name} disabled={disabled} onChange={(e) => patchCategory(index, { name: e.target.value })} /></label><label>分组<input className="input" maxLength={64} value={category.group} disabled={disabled} onChange={(e) => patchCategory(index, { group: e.target.value })} /></label><label>标识色<input className="scheme-color" type="color" value={category.color ?? "#2f6fed"} disabled={disabled} onChange={(e) => patchCategory(index, { color: e.target.value })} /></label><label>运行状态<select className="select" value={category.is_active ? "active" : "inactive"} disabled={disabled} onChange={(e) => patchCategory(index, { is_active: e.target.value === "active" })}><option value="active">启用</option><option value="inactive">停用</option></select></label><label>参与方式<select className="select" value={category.participant_mode} disabled={disabled} onChange={(e) => patchCategory(index, { participant_mode: e.target.value as "unordered" | "role_based", role_definitions: e.target.value === "unordered" ? [] : category.role_definitions, mouse_count_min: e.target.value === "unordered" ? 1 : undefined, mouse_count_max: null })}><option value="unordered">无序参与对象</option><option value="role_based">按角色分配</option></select></label></div>
        {category.participant_mode === "unordered" ? <div className="count-fields"><label>最少对象<input className="input" type="number" min={1} step={1} disabled={disabled} value={category.mouse_count_min ?? 1} onChange={(e) => patchCategory(index, { mouse_count_min: Number(e.target.value) })} /></label><label>最多对象<input className="input" type="number" min={category.mouse_count_min ?? 1} step={1} disabled={disabled} value={category.mouse_count_max ?? ""} placeholder="不限" onChange={(e) => patchCategory(index, { mouse_count_max: e.target.value === "" ? null : Number(e.target.value) })} /></label></div> : <div className="role-definition-editor"><div className="role-editor-head"><b>参与对象角色</b><span>按创建顺序展示；标识由系统生成</span>{!disabled ? <button type="button" className="btn btn-sm" onClick={() => patchCategory(index, { role_definitions: [...category.role_definitions, { name: "", min_count: 0, max_count: null, role_sort_order: category.role_definitions.length }] })}>＋ 新增角色</button> : null}</div>{category.role_definitions.length === 0 ? <div className="empty-role">尚未添加角色。角色最少数量之和需至少为 1。</div> : category.role_definitions.map((role, roleIndex) => <div className="role-definition-row" key={role.key ?? `new-role-${roleIndex}`}><span className="role-sequence">角色 {roleIndex + 1}</span><label>名称<input className="input" maxLength={64} value={role.name} disabled={disabled} onChange={(e) => patchRole(index, roleIndex, { name: e.target.value })} /></label><label>最少<input className="input" type="number" min={0} step={1} value={role.min_count} disabled={disabled} onChange={(e) => patchRole(index, roleIndex, { min_count: Number(e.target.value) })} /></label><label>最多<input className="input" type="number" min={role.min_count} step={1} value={role.max_count ?? ""} placeholder="不限" disabled={disabled} onChange={(e) => patchRole(index, roleIndex, { max_count: e.target.value === "" ? null : Number(e.target.value) })} /></label>{!disabled ? <button type="button" className="btn btn-sm btn-danger" onClick={() => patchCategory(index, { role_definitions: category.role_definitions.filter((_, i) => i !== roleIndex) })}>删除</button> : null}</div>)}</div>}
        {validation.categoryIssues[index]?.length ? <div className="scheme-category-errors" role="status">{validation.categoryIssues[index].join("；")}</div> : null}
      </section>)}
      {normalized.length === 0 ? <EmptyState compact title="方案中还没有类别" hint={emptyHint} /> : null}
    </div>
    {!disabled ? <div className="scheme-editor-add"><button type="button" className="btn" onClick={addCategory}>＋ 新增类别</button><span>新增项不会携带服务端类别 ID 或角色 key。</span></div> : null}
  </div>;
}
