"""项目权限的唯一语义入口。"""
from fastapi import HTTPException

from .models import ProjectMembership

MANAGER_ROLES = frozenset({"owner", "admin"})


def is_manager(membership: ProjectMembership) -> bool:
    return membership.role in MANAGER_ROLES


def can_edit(membership: ProjectMembership) -> bool:
    return membership.status == "active"


def can_review(membership: ProjectMembership) -> bool:
    return is_manager(membership) or membership.can_review


def require_manager(membership: ProjectMembership, detail: str = "Only owner/admin may perform this action") -> None:
    if not is_manager(membership):
        raise HTTPException(status_code=403, detail=detail)


def require_editor(membership: ProjectMembership, detail: str = "Only active project members may modify data") -> None:
    if not can_edit(membership):
        raise HTTPException(status_code=403, detail=detail)


def require_reviewer(membership: ProjectMembership, detail: str = "Review permission is required") -> None:
    if not can_review(membership):
        raise HTTPException(status_code=403, detail=detail)
