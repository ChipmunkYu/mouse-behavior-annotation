import type { RoleDefinition } from "../api/types";

export type RoleAssignments = Record<string, number[]>;

/**
 * A role is available on first entry only after every preceding role reaches
 * its minimum. Once entered, the caller keeps its key in `unlocked` so later
 * edits to preceding roles do not prevent manual back-and-forth navigation.
 */
export function isRoleAccessible(
  roles: RoleDefinition[],
  assignments: RoleAssignments,
  unlocked: ReadonlySet<string>,
  key: string,
): boolean {
  const index = roles.findIndex((role) => role.key === key);
  if (index < 0) return false;
  return index === 0 || unlocked.has(key) || roles.slice(0, index).every(
    (role) => (assignments[role.key] ?? []).length >= role.min_count,
  );
}

/** Restore a draft through its furthest role with any saved assignment. */
export function getInitiallyUnlockedRoleKeys(
  roles: RoleDefinition[],
  assignments: RoleAssignments,
): Set<string> {
  const furthestAssignedIndex = roles.reduce(
    (furthest, role, index) => (assignments[role.key] ?? []).length > 0 ? index : furthest,
    -1,
  );
  const unlocked = new Set<string>();
  roles.forEach((role, index) => {
    if (index <= furthestAssignedIndex || isRoleAccessible(roles, assignments, unlocked, role.key)) {
      unlocked.add(role.key);
    }
  });
  return unlocked;
}
