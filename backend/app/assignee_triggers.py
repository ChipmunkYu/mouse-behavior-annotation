"""SQLite assignee integrity barriers shared by Alembic and ``create_all``."""

from sqlalchemy.exc import IntegrityError


ASSIGNEE_CONFLICT_DETAIL = "Assignee is no longer an active member of this project"
_ASSIGNEE_TRIGGER_ERROR = "assignee must be an active membership in the video project"
_ASSIGNEE_FK_NAME = "fk_videos_assignee_project"

TRIGGERS = {
    "trg_videos_active_assignee_insert": """
        BEFORE INSERT ON videos WHEN NEW.assignee_membership_id IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'assignee must be an active membership in the video project')
        WHERE NOT EXISTS (SELECT 1 FROM project_memberships m
          WHERE m.id=NEW.assignee_membership_id AND m.project_id=NEW.project_id AND m.status='active'); END
    """,
    "trg_videos_active_assignee_update": """
        BEFORE UPDATE OF assignee_membership_id, project_id ON videos
        WHEN NEW.assignee_membership_id IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'assignee must be an active membership in the video project')
        WHERE NOT EXISTS (SELECT 1 FROM project_memberships m
          WHERE m.id=NEW.assignee_membership_id AND m.project_id=NEW.project_id AND m.status='active'); END
    """,
    "trg_membership_assignee_stays_active": """
        BEFORE UPDATE OF status, project_id ON project_memberships
        WHEN (NEW.status <> 'active' OR NEW.project_id <> OLD.project_id)
          AND EXISTS (SELECT 1 FROM videos v WHERE v.assignee_membership_id=OLD.id)
        BEGIN SELECT RAISE(ABORT, 'assigned membership must remain active in its project'); END
    """,
}


def install_sqlite_assignee_triggers(connection) -> None:
    if connection.dialect.name != "sqlite":
        return
    for name, body in TRIGGERS.items():
        connection.exec_driver_sql(f"CREATE TRIGGER IF NOT EXISTS {name} {body}")


def drop_sqlite_assignee_triggers(connection) -> None:
    if connection.dialect.name != "sqlite":
        return
    for name in TRIGGERS:
        connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")


def is_assignee_write_conflict(exc: IntegrityError) -> bool:
    """Identify only the video-assignee FK/trigger failure, not arbitrary DB errors."""
    constraint_name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    if constraint_name == _ASSIGNEE_FK_NAME:
        return True
    message = str(exc.orig).lower()
    return _ASSIGNEE_TRIGGER_ERROR in message or _ASSIGNEE_FK_NAME in message
