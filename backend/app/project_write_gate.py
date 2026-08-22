"""SQLite writer serialization for project category-scheme mutations."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .models import Project, ProjectMembership


def _before_project_lock() -> None:
    """Test synchronization point before acquiring the SQLite writer lock."""


def _after_project_lock() -> None:
    """Test synchronization point after acquiring the SQLite writer lock."""


def _is_locked_error(exc: OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


@contextmanager
def project_scheme_write_gate(
    db: Session, *, project_id: int, actor_id: int
) -> Iterator[tuple[Project, ProjectMembership]]:
    """Acquire the Project row writer gate, then re-read and authorize active owner."""
    try:
        _before_project_lock()
        result = db.execute(
            update(Project).where(Project.id == project_id).values(id=Project.id)
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=404, detail="Project not found")
        _after_project_lock()
        db.expire_all()
        project = db.get(Project, project_id)
        membership = db.query(ProjectMembership).filter_by(
            project_id=project_id, user_id=actor_id
        ).one_or_none()
        if membership is None or membership.status != "active" or membership.role != "owner":
            raise HTTPException(status_code=403, detail="Active project owner access required")
        yield project, membership
    except OperationalError as exc:
        db.rollback()
        if _is_locked_error(exc):
            raise HTTPException(
                status_code=409, detail="Category scheme is being modified; retry the request"
            ) from exc
        raise
    except BaseException:
        db.rollback()
        raise
