"""Assignments, unified membership roles, and project invites.

Revision ID: 0012; Revises: 0011.
"""
import secrets

from alembic import op
import sqlalchemy as sa

from app.assignee_triggers import (
    drop_sqlite_assignee_triggers,
    install_sqlite_assignee_triggers,
)

revision = "0012"
down_revision = "0011"
branch_labels = depends_on = None


def upgrade():
    conn = op.get_bind()
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("invite_code", sa.String(64), nullable=True))
    project_ids = [row[0] for row in conn.execute(sa.text("SELECT id FROM projects"))]
    for project_id in project_ids:
        conn.execute(sa.text("UPDATE projects SET invite_code=:code WHERE id=:id"),
                     {"code": secrets.token_urlsafe(32), "id": project_id})
    with op.batch_alter_table("projects") as batch:
        batch.alter_column("invite_code", nullable=False)
        batch.create_unique_constraint("uq_projects_invite_code", ["invite_code"])

    with op.batch_alter_table("project_memberships") as batch:
        batch.add_column(sa.Column("can_review", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_unique_constraint("uq_membership_id_project", ["id", "project_id"])
    conn.execute(sa.text("UPDATE project_memberships SET can_review=1 WHERE role='reviewer'"))
    conn.execute(sa.text("UPDATE project_memberships SET role='member' WHERE role IN ('reviewer','annotator')"))
    with op.batch_alter_table("project_memberships") as batch:
        batch.create_check_constraint("ck_membership_role", "role IN ('owner', 'admin', 'member')")

    with op.batch_alter_table("videos") as batch:
        batch.add_column(sa.Column("assignee_membership_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_videos_assignee_project", "project_memberships",
            ["assignee_membership_id", "project_id"], ["id", "project_id"], ondelete="RESTRICT",
        )
        batch.create_index("ix_videos_assignee_membership_id", ["assignee_membership_id"])
    install_sqlite_assignee_triggers(conn)


def downgrade():
    conn = op.get_bind()
    drop_sqlite_assignee_triggers(conn)
    with op.batch_alter_table("videos") as batch:
        batch.drop_index("ix_videos_assignee_membership_id")
        batch.drop_constraint("fk_videos_assignee_project", type_="foreignkey")
        batch.drop_column("assignee_membership_id")
    with op.batch_alter_table("project_memberships") as batch:
        batch.drop_constraint("ck_membership_role", type_="check")
    conn.execute(sa.text(
        "UPDATE project_memberships SET role=CASE "
        "WHEN role='member' AND can_review=1 THEN 'reviewer' "
        "WHEN role='member' THEN 'annotator' ELSE role END"
    ))
    with op.batch_alter_table("project_memberships") as batch:
        batch.drop_constraint("uq_membership_id_project", type_="unique")
        batch.drop_column("can_review")
    with op.batch_alter_table("projects") as batch:
        batch.drop_constraint("uq_projects_invite_code", type_="unique")
        batch.drop_column("invite_code")
