"""Owner-only aggregate category-scheme configuration and audit API."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..category_scheme_service import (
    CategorySchemeError,
    categories_for_project,
    normalize_and_persist_category_scheme,
    scheme_hash,
    scheme_snapshot,
)
from ..database import get_db
from ..deps import project_access
from ..models import BehaviorCategory, CategorySchemeAudit, Project, User
from ..participant_roles import ParticipantRoleError, canonicalize_role_definitions
from ..project_write_gate import project_scheme_write_gate
from ..schemas import (
    CategoryOut,
    CategorySchemeAuditOut,
    CategorySchemeLock,
    CategorySchemeOut,
    CategorySchemePut,
)

router = APIRouter(prefix="/api/projects/{project_id}/category-scheme", tags=["category-scheme"])


def _require_active_owner(access: tuple[Project, object]) -> Project:
    project, membership = access
    if membership.role != "owner":
        raise HTTPException(status_code=403, detail="Active project owner access required")
    return project


def _categories(db: Session, project_id: int) -> list[BehaviorCategory]:
    return categories_for_project(db, project_id)


def _validate_persisted_scheme(categories: list[BehaviorCategory]) -> None:
    """Reject a raw-SQL-corrupted draft before making it permanent."""
    if not categories:
        raise HTTPException(status_code=422, detail="Category scheme must contain at least one category")
    if [category.sort_order for category in categories] != list(range(len(categories))):
        raise HTTPException(status_code=422, detail="Category sort_order must be unique and continuous from zero")
    names: set[str] = set()
    role_keys: set[str] = set()
    for category in categories:
        if (
            not isinstance(category.name, str) or category.name != category.name.strip()
            or not category.name or len(category.name) > 64
            or not isinstance(category.group, str) or category.group != category.group.strip()
            or not category.group or len(category.group) > 64
        ):
            raise HTTPException(status_code=422, detail="Persisted category name or group is invalid")
        folded = category.name.casefold()
        if folded in names:
            raise HTTPException(status_code=422, detail="Persisted category names are not unique")
        names.add(folded)
        try:
            definitions, derived_min, derived_max = canonicalize_role_definitions(
                category.participant_mode,
                category.role_definitions,
                existing_definitions=category.role_definitions or [],
            )
        except ParticipantRoleError as exc:
            raise HTTPException(status_code=422, detail=f"Persisted role definitions are invalid: {exc}") from exc
        if definitions != (category.role_definitions or []):
            raise HTTPException(status_code=422, detail="Persisted role definitions are not canonical")
        for definition in definitions:
            key = definition["key"]
            if key in role_keys:
                raise HTTPException(
                    status_code=422,
                    detail="Persisted role definition keys must be unique across the project",
                )
            role_keys.add(key)
        if category.participant_mode == "role_based":
            valid_counts = (
                category.mouse_count_min == derived_min
                and category.mouse_count_max == derived_max
            )
        else:
            minimum = category.mouse_count_min
            maximum = category.mouse_count_max
            valid_counts = (
                isinstance(minimum, int) and not isinstance(minimum, bool) and minimum >= 1
                and (maximum is None or (
                    isinstance(maximum, int) and not isinstance(maximum, bool) and maximum >= minimum
                ))
            )
        if not valid_counts:
            raise HTTPException(status_code=422, detail="Persisted category mouse counts are invalid")


def _out(project: Project, categories: list[BehaviorCategory]) -> CategorySchemeOut:
    return CategorySchemeOut(
        project_id=project.id,
        category_scheme_version=project.category_scheme_version,
        category_scheme_locked_at=project.category_scheme_locked_at,
        category_scheme_locked_by=project.category_scheme_locked_by,
        categories=[CategoryOut.model_validate(category) for category in categories],
    )


@router.get("", response_model=CategorySchemeOut)
def get_category_scheme(
    project_id: int,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> CategorySchemeOut:
    project = _require_active_owner(access)
    return _out(project, _categories(db, project_id))


@router.put("", response_model=CategorySchemeOut)
def replace_category_scheme(
    project_id: int,
    body: CategorySchemePut,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CategorySchemeOut:
    with project_scheme_write_gate(db, project_id=project_id, actor_id=user.id) as (project, _membership):
        current = _categories(db, project_id)
        if project.category_scheme_locked_at is not None:
            raise HTTPException(status_code=409, detail="Category scheme is permanently locked")
        if project.category_scheme_version != body.expected_version:
            raise HTTPException(status_code=409, detail="Category scheme version changed concurrently")
        before = scheme_snapshot(project, current)
        try:
            normalize_and_persist_category_scheme(
                db,
                project_id=project_id,
                request_categories=body.categories,
                existing_categories=current,
            )
        except CategorySchemeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        project.category_scheme_version += 1
        db.flush()
        db.expire_all()
        project = db.get(Project, project_id)
        after_categories = _categories(db, project_id)
        after = scheme_snapshot(project, after_categories)
        db.add(CategorySchemeAudit(
            project_id=project_id, actor_id=user.id, action="replace",
            scheme_version=project.category_scheme_version,
            before_json=before, after_json=after, scheme_hash=scheme_hash(after),
        ))
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail="Category scheme conflicts with existing data") from exc
        project = db.get(Project, project_id)
        return _out(project, _categories(db, project_id))


@router.post("/lock", response_model=CategorySchemeOut)
def lock_category_scheme(
    project_id: int,
    body: CategorySchemeLock,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CategorySchemeOut:
    with project_scheme_write_gate(db, project_id=project_id, actor_id=user.id) as (project, _membership):
        categories = _categories(db, project_id)
        if project.category_scheme_locked_at is not None:
            return _out(project, categories)
        if project.category_scheme_version != body.expected_version:
            raise HTTPException(status_code=409, detail="Category scheme version changed concurrently")
        _validate_persisted_scheme(categories)
        before = scheme_snapshot(project, categories)
        project.category_scheme_locked_at = datetime.utcnow()
        project.category_scheme_locked_by = user.id
        db.flush()
        after = scheme_snapshot(project, categories)
        db.add(CategorySchemeAudit(
            project_id=project_id, actor_id=user.id, action="lock",
            scheme_version=project.category_scheme_version,
            before_json=before, after_json=after, scheme_hash=scheme_hash(after),
        ))
        db.commit()
        project = db.get(Project, project_id)
        return _out(project, _categories(db, project_id))


@router.get("/audit", response_model=list[CategorySchemeAuditOut])
def list_category_scheme_audit(
    project_id: int,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> list[CategorySchemeAudit]:
    _require_active_owner(access)
    return db.query(CategorySchemeAudit).filter_by(project_id=project_id).order_by(
        CategorySchemeAudit.id
    ).all()
