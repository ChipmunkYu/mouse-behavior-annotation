"""项目、成员和邀请码接口。"""
from __future__ import annotations

import secrets

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
from ..models import CategorySchemeAudit, Project, ProjectMembership, User
from ..permissions import can_review, require_manager
from ..schemas import (
    AssigneeDirectoryItem, InviteOut, JoinProjectRequest, MembershipOut, MembershipUpdate,
    ProjectCreate, ProjectOut,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])
_INVITE_RESET_ATTEMPTS = 5


def _is_membership_unique_conflict(exc: IntegrityError) -> bool:
    message = str(exc.orig).lower()
    return (
        "uq_membership_user_project" in message
        or "project_memberships.user_id, project_memberships.project_id" in message
    )


def _is_invite_unique_conflict(exc: IntegrityError) -> bool:
    message = str(exc.orig).lower()
    return "uq_projects_invite_code" in message or "projects.invite_code" in message


def _membership_out(m: ProjectMembership) -> MembershipOut:
    return MembershipOut(
        id=m.id, project_id=m.project_id, user_id=m.user_id, username=m.user.username,
        role=m.role, can_review=can_review(m), status=m.status, created_at=m.created_at,
    )


def _project_out(p: Project, m: ProjectMembership) -> ProjectOut:
    return ProjectOut(
        id=p.id, name=p.name, description=p.description, status=p.status,
        created_at=p.created_at, role=m.role, membership_id=m.id, can_review=can_review(m),
        category_scheme_version=p.category_scheme_version,
        category_scheme_locked_at=p.category_scheme_locked_at,
        category_scheme_locked_by=p.category_scheme_locked_by,
    )


@router.get("", response_model=list[ProjectOut])
def list_projects(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ProjectOut]:
    memberships = (
        db.query(ProjectMembership)
        .filter(ProjectMembership.user_id == user.id)
        .order_by(ProjectMembership.created_at.desc())
        .all()
    )
    result: list[ProjectOut] = []
    for m in memberships:
        p = m.project
        result.append(_project_out(p, m))
    return result


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    body: ProjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectOut:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name must not be empty")

    try:
        project = Project(
            name=name, description=body.description, status="active", created_by=user.id
        )
        db.add(project)
        db.flush()
        membership = ProjectMembership(
            project_id=project.id, user_id=user.id, role="owner"
        )
        db.add(membership)
        db.flush()
        before = scheme_snapshot(project, [])
        normalize_and_persist_category_scheme(
            db,
            project_id=project.id,
            request_categories=body.categories,
            existing_categories=[],
        )
        db.flush()
        categories = categories_for_project(db, project.id)
        after = scheme_snapshot(project, categories)
        db.add(CategorySchemeAudit(
            project_id=project.id,
            actor_id=user.id,
            action="replace",
            scheme_version=0,
            before_json=before,
            after_json=after,
            scheme_hash=scheme_hash(after),
        ))
        db.commit()
    except CategorySchemeError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Project or category scheme conflicts") from exc
    db.refresh(project)
    db.refresh(membership)
    return _project_out(project, membership)


@router.post("/join", response_model=MembershipOut)
def join_project(body: JoinProjectRequest, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)) -> MembershipOut:
    project = db.query(Project).filter(Project.invite_code == body.invite_code).one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Invite code not found")
    existing = db.query(ProjectMembership).filter_by(project_id=project.id, user_id=user.id).one_or_none()
    if existing is not None:
        return _membership_out(existing)
    membership = ProjectMembership(project_id=project.id, user_id=user.id, role="member", can_review=False)
    db.add(membership)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if not _is_membership_unique_conflict(exc):
            raise
        existing = db.query(ProjectMembership).filter_by(
            project_id=project.id, user_id=user.id
        ).one_or_none()
        if existing is None:
            raise
        return _membership_out(existing)
    db.refresh(membership)
    return _membership_out(membership)


@router.get("/{project_id}/members", response_model=list[MembershipOut])
def list_members(project_id: int, access: tuple = Depends(project_access),
                 db: Session = Depends(get_db)) -> list[MembershipOut]:
    require_manager(access[1])
    rows = db.query(ProjectMembership).filter_by(project_id=project_id).order_by(ProjectMembership.id).all()
    return [_membership_out(row) for row in rows]


@router.get("/{project_id}/assignees", response_model=list[AssigneeDirectoryItem])
def list_assignees(project_id: int, access: tuple = Depends(project_access),
                   db: Session = Depends(get_db)) -> list[AssigneeDirectoryItem]:
    rows = db.query(ProjectMembership).filter_by(
        project_id=project_id, status="active"
    ).order_by(ProjectMembership.id).all()
    return [AssigneeDirectoryItem(membership_id=row.id, username=row.user.username) for row in rows]


@router.patch("/{project_id}/members/{membership_id}", response_model=MembershipOut)
def update_member(project_id: int, membership_id: int, body: MembershipUpdate,
                  access: tuple = Depends(project_access), db: Session = Depends(get_db)) -> MembershipOut:
    require_manager(access[1])
    target = db.get(ProjectMembership, membership_id)
    if target is None or target.project_id != project_id:
        raise HTTPException(status_code=404, detail="Membership not found")
    if target.role == "owner":
        raise HTTPException(status_code=409, detail="Project owner cannot be changed")
    if body.role is not None:
        target.role = body.role
    if body.can_review is not None:
        target.can_review = body.can_review
    db.commit(); db.refresh(target)
    return _membership_out(target)


@router.delete("/{project_id}/members/{membership_id}", status_code=204)
def remove_member(project_id: int, membership_id: int, access: tuple = Depends(project_access),
                  db: Session = Depends(get_db)) -> None:
    require_manager(access[1])
    target = db.get(ProjectMembership, membership_id)
    if target is None or target.project_id != project_id:
        raise HTTPException(status_code=404, detail="Membership not found")
    if target.role == "owner":
        raise HTTPException(status_code=409, detail="Project owner cannot be removed")
    from ..models import Video
    if db.query(Video.id).filter(Video.assignee_membership_id == target.id).first():
        raise HTTPException(status_code=409, detail="Member is still assigned to videos")
    db.delete(target)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Member is still assigned to videos") from None


@router.get("/{project_id}/invite", response_model=InviteOut)
def get_invite(project_id: int, access: tuple = Depends(project_access)) -> InviteOut:
    require_manager(access[1])
    return InviteOut(invite_code=access[0].invite_code)


@router.post("/{project_id}/invite/reset", response_model=InviteOut)
def reset_invite(project_id: int, access: tuple = Depends(project_access),
                 db: Session = Depends(get_db)) -> InviteOut:
    require_manager(access[1])
    project_id = access[0].id
    for _attempt in range(_INVITE_RESET_ATTEMPTS):
        project = db.get(Project, project_id)
        project.invite_code = secrets.token_urlsafe(32)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            if _is_invite_unique_conflict(exc):
                continue
            raise
        db.refresh(project)
        return InviteOut(invite_code=project.invite_code)
    raise HTTPException(status_code=409, detail="Could not generate a unique invite code")
