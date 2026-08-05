"""项目接口：仅返回当前用户成员项目及其项目内角色。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import Project, ProjectMembership, User
from ..schemas import ProjectCreate, ProjectOut
from ..seed import init_project_categories

router = APIRouter(prefix="/api/projects", tags=["projects"])


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
        result.append(
            ProjectOut(
                id=p.id,
                name=p.name,
                description=p.description,
                status=p.status,
                created_at=p.created_at,
                role=m.role,
            )
        )
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

    project = Project(name=name, description=body.description, status="active", created_by=user.id)
    db.add(project)
    db.flush()  # 获取 project.id

    db.add(
        ProjectMembership(project_id=project.id, user_id=user.id, role="owner")
    )
    init_project_categories(db, project.id)  # 自动初始化 12 类
    db.commit()
    db.refresh(project)

    return ProjectOut(
        id=project.id,
        name=project.name,
        description=project.description,
        status=project.status,
        created_at=project.created_at,
        role="owner",
    )
