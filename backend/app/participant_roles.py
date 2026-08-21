"""Canonical participant-role rules shared by every backend authority path."""
from __future__ import annotations

import re
import uuid
from typing import Any, Iterable

ROLE_KEY_RE = re.compile(r"^role_[0-9a-f]{32}$")


class ParticipantRoleError(ValueError):
    pass


def new_role_key() -> str:
    return f"role_{uuid.uuid4().hex}"


def canonicalize_role_definitions(
    participant_mode: str,
    definitions: Any,
    *,
    existing_definitions: Iterable[dict[str, Any]] = (),
) -> tuple[list[dict[str, Any]], int, int | None]:
    """Return canonical definitions and their derived total mouse range."""
    if participant_mode not in {"unordered", "role_based"}:
        raise ParticipantRoleError("participant_mode must be unordered or role_based")
    if not isinstance(definitions, list):
        raise ParticipantRoleError("role_definitions must be a list")
    if participant_mode == "unordered":
        if definitions:
            raise ParticipantRoleError("unordered categories must have empty role_definitions")
        return [], 1, None

    existing_keys = [
        item.get("key") for item in existing_definitions
        if isinstance(item, dict) and isinstance(item.get("key"), str)
    ]
    allowed = set(existing_keys)
    seen_keys: set[str] = set()
    seen_names: set[str] = set()
    canonical: list[dict[str, Any]] = []
    submitted_existing_keys: list[str] = []
    saw_new_role = False
    for index, raw in enumerate(definitions):
        if not isinstance(raw, dict):
            raise ParticipantRoleError("each role definition must be an object")
        extra = set(raw) - {"key", "name", "min_count", "max_count", "role_sort_order"}
        if extra:
            raise ParticipantRoleError(f"unknown role definition fields: {sorted(extra)}")
        key = raw.get("key")
        if key is None:
            saw_new_role = True
            key = new_role_key()
        elif not isinstance(key, str) or not ROLE_KEY_RE.fullmatch(key) or key not in allowed:
            raise ParticipantRoleError("role key is forged, unknown, or belongs to another category")
        else:
            if saw_new_role:
                raise ParticipantRoleError("new roles must follow all retained existing roles")
            submitted_existing_keys.append(key)
        if key in seen_keys:
            raise ParticipantRoleError("duplicate role key")
        seen_keys.add(key)

        name = raw.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64:
            raise ParticipantRoleError("role name must be 1-64 characters after trimming")
        name = name.strip()
        folded = name.casefold()
        if folded in seen_names:
            raise ParticipantRoleError("role names must be unique ignoring case")
        seen_names.add(folded)

        minimum = raw.get("min_count")
        maximum = raw.get("max_count")
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
            raise ParticipantRoleError("role min_count must be a non-negative integer")
        if maximum is not None and (
            isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < minimum
        ):
            raise ParticipantRoleError("role max_count must be null or at least min_count")
        supplied_order = raw.get("role_sort_order", index)
        if isinstance(supplied_order, bool) or supplied_order != index:
            raise ParticipantRoleError("role_sort_order must be continuous from zero")
        canonical.append({
            "key": key,
            "name": name,
            "min_count": minimum,
            "max_count": maximum,
            "role_sort_order": index,
        })

    retained_keys = [key for key in existing_keys if key in seen_keys]
    if submitted_existing_keys != retained_keys:
        raise ParticipantRoleError("existing roles cannot be reordered")

    total_min = sum(item["min_count"] for item in canonical)
    if total_min < 1:
        raise ParticipantRoleError("role-based total min_count must be at least one")
    total_max = (
        None
        if any(item["max_count"] is None for item in canonical)
        else sum(item["max_count"] for item in canonical)
    )
    return canonical, total_min, total_max


def canonicalize_participant_roles(
    definitions: list[dict[str, Any]], participant_roles: Any
) -> tuple[dict[str, list[int]], list[int], str]:
    """Canonicalize a complete role map, derive mouse_ids and completeness status."""
    if not isinstance(participant_roles, dict):
        raise ParticipantRoleError("participant_roles must be an object")
    expected = [item["key"] for item in definitions]
    if set(participant_roles) != set(expected):
        raise ParticipantRoleError("participant_roles must cover exactly all defined roles")
    result: dict[str, list[int]] = {}
    all_ids: set[int] = set()
    valid = True
    for definition in definitions:
        key = definition["key"]
        values = participant_roles[key]
        if not isinstance(values, list):
            raise ParticipantRoleError("each participant role value must be a list")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise ParticipantRoleError("track IDs must be non-negative integers")
        if len(values) != len(set(values)):
            raise ParticipantRoleError("track IDs within a role must be unique")
        if all_ids.intersection(values):
            raise ParticipantRoleError("a track ID cannot appear in multiple roles")
        ordered = sorted(values)
        result[key] = ordered
        all_ids.update(ordered)
        maximum = definition["max_count"]
        if maximum is not None and len(ordered) > maximum:
            raise ParticipantRoleError(f"participant role {key} exceeds max_count")
        valid = valid and len(ordered) >= definition["min_count"]
    return result, sorted(all_ids), "valid" if valid else "needs_participants"
