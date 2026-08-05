"""公共依赖：项目存在性与成员权限。"""
from __future__ import annotations

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from .auth import get_current_user
from .database import get_db
from .models import Project, ProjectMembership, User


def require_project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def require_membership(db: Session, project_id: int, user_id: int) -> ProjectMembership:
    membership = (
        db.query(ProjectMembership)
        .filter(ProjectMembership.project_id == project_id, ProjectMembership.user_id == user_id)
        .first()
    )
    if membership is None:
        raise HTTPException(
            status_code=403, detail="You are not a member of this project"
        )
    return membership


def project_access(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> tuple[Project, ProjectMembership]:
    """仅项目成员可用的端点依赖；返回 (project, membership)。"""
    project = require_project(db, project_id)
    membership = require_membership(db, project_id, user.id)
    return project, membership
