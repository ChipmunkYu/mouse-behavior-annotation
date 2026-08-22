"""行为类别接口：仅项目成员可读，返回启用类别。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import project_access
from ..models import BehaviorCategory
from ..schemas import CategoryOut

router = APIRouter(tags=["categories"])


@router.get("/api/projects/{project_id}/categories", response_model=list[CategoryOut])
def list_categories(
    project_id: int,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> list[BehaviorCategory]:
    if access[0].category_scheme_locked_at is None:
        raise HTTPException(status_code=409, detail="Category scheme is not locked")
    return (
        db.query(BehaviorCategory)
        .filter(BehaviorCategory.project_id == project_id, BehaviorCategory.is_active.is_(True))
        .order_by(BehaviorCategory.sort_order, BehaviorCategory.id)
        .all()
    )
