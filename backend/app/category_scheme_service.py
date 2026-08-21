"""Shared normalization and persistence for complete project category schemes."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from sqlalchemy.orm import Session

from .models import BehaviorCategory, Project
from .participant_roles import ParticipantRoleError, canonicalize_role_definitions
from .schemas import CategorySchemeCategoryIn


class CategorySchemeError(ValueError):
    pass


@dataclass
class NormalizedCategory:
    request: CategorySchemeCategoryIn
    definitions: list[dict]
    minimum: int
    maximum: int | None


def categories_for_project(db: Session, project_id: int) -> list[BehaviorCategory]:
    return db.query(BehaviorCategory).filter_by(project_id=project_id).order_by(
        BehaviorCategory.sort_order, BehaviorCategory.id
    ).all()


def scheme_snapshot(project: Project, categories: list[BehaviorCategory]) -> dict:
    return {
        "project_id": project.id,
        "category_scheme_version": project.category_scheme_version,
        "category_scheme_locked_at": (
            project.category_scheme_locked_at.isoformat()
            if project.category_scheme_locked_at is not None else None
        ),
        "category_scheme_locked_by": project.category_scheme_locked_by,
        "categories": [{
            "id": category.id,
            "project_id": category.project_id,
            "name": category.name,
            "group": category.group,
            "color": category.color,
            "sort_order": category.sort_order,
            "is_active": category.is_active,
            "mouse_count_min": category.mouse_count_min,
            "mouse_count_max": category.mouse_count_max,
            "participant_mode": category.participant_mode,
            "role_definitions": category.role_definitions,
        } for category in categories],
    }


def scheme_hash(snapshot: dict) -> str:
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_category_scheme(
    request_categories: list[CategorySchemeCategoryIn],
    existing_categories: list[BehaviorCategory],
) -> tuple[list[NormalizedCategory], set[int]]:
    """Apply the one canonical rule set used by create and complete-scheme PUT."""
    existing = {category.id: category for category in existing_categories}
    supplied_ids: set[int] = set()
    names: set[str] = set()
    role_keys: set[str] = set()
    sort_orders = [category.sort_order for category in request_categories]
    if sorted(sort_orders) != list(range(len(sort_orders))):
        raise CategorySchemeError("Category sort_order must be unique and continuous from zero")

    normalized: list[NormalizedCategory] = []
    for request_category in request_categories:
        name = request_category.name.strip()
        group = request_category.group.strip()
        if not name or not group:
            raise CategorySchemeError("Category name and group must not be blank")
        folded = name.casefold()
        if folded in names:
            raise CategorySchemeError("Category names must be unique ignoring case")
        names.add(folded)

        category = None
        existing_definitions: list[dict] = []
        if request_category.id is not None:
            if isinstance(request_category.id, bool) or request_category.id in supplied_ids:
                raise CategorySchemeError("Duplicate category id")
            category = existing.get(request_category.id)
            if category is None:
                raise CategorySchemeError("Unknown category id")
            supplied_ids.add(request_category.id)
            existing_definitions = category.role_definitions or []
        if request_category.participant_mode == "role_based" and (
            "mouse_count_min" in request_category.model_fields_set
            or "mouse_count_max" in request_category.model_fields_set
        ):
            raise CategorySchemeError(
                "role_based mouse count is derived from role_definitions"
            )
        try:
            definitions, derived_min, derived_max = canonicalize_role_definitions(
                request_category.participant_mode,
                request_category.role_definitions,
                existing_definitions=existing_definitions,
            )
        except ParticipantRoleError as exc:
            raise CategorySchemeError(str(exc)) from exc
        for definition in definitions:
            key = definition["key"]
            if key in role_keys:
                raise CategorySchemeError(
                    "Role definition keys must be unique across the project"
                )
            role_keys.add(key)
        if request_category.participant_mode == "unordered":
            minimum = request_category.mouse_count_min or 1
            maximum = request_category.mouse_count_max
            if maximum is not None and maximum < minimum:
                raise CategorySchemeError("mouse_count_max must be at least mouse_count_min")
        else:
            minimum, maximum = derived_min, derived_max
        request_category.name = name
        request_category.group = group
        normalized.append(NormalizedCategory(
            request=request_category,
            definitions=definitions,
            minimum=minimum,
            maximum=maximum,
        ))
    return normalized, supplied_ids


def normalize_and_persist_category_scheme(
    db: Session,
    *,
    project_id: int,
    request_categories: list[CategorySchemeCategoryIn],
    existing_categories: list[BehaviorCategory],
) -> None:
    """Normalize and stage a complete replacement without committing."""
    normalized, supplied_ids = normalize_category_scheme(
        request_categories, existing_categories
    )
    existing = {category.id: category for category in existing_categories}
    for item in normalized:
        request_category = item.request
        category = existing.get(request_category.id) if request_category.id is not None else None
        if category is None:
            category = BehaviorCategory(project_id=project_id)
            db.add(category)
        category.name = request_category.name
        category.group = request_category.group
        category.color = request_category.color
        category.sort_order = request_category.sort_order
        category.is_active = request_category.is_active
        category.participant_mode = request_category.participant_mode
        category.role_definitions = item.definitions
        category.mouse_count_min = item.minimum
        category.mouse_count_max = item.maximum
    for category_id, category in existing.items():
        if category_id not in supplied_ids:
            db.delete(category)
