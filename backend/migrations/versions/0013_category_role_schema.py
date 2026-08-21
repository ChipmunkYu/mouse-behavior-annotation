"""Project category scheme, participant-role persistence, audit, and barriers.

Revision ID: 0013; Revises: 0012.
"""
from alembic import op
import sqlalchemy as sa

from app.authority_triggers import (
    drop_sqlite_authority_triggers,
    install_sqlite_authority_triggers,
)

revision = "0013"
down_revision = "0012"
branch_labels = depends_on = None


def upgrade():
    conn = op.get_bind()
    drop_sqlite_authority_triggers(conn)

    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("category_scheme_version", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("category_scheme_locked_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("category_scheme_locked_by", sa.Integer(), nullable=True))
        batch.create_check_constraint("ck_projects_category_scheme_version", "category_scheme_version >= 0")
        batch.create_check_constraint(
            "ck_projects_category_scheme_lock_pair",
            "(category_scheme_locked_at IS NULL) = (category_scheme_locked_by IS NULL)",
        )
        batch.create_foreign_key(
            "fk_projects_category_scheme_locked_by_users", "users",
            ["category_scheme_locked_by"], ["id"], ondelete="RESTRICT",
        )

    with op.batch_alter_table("behavior_categories") as batch:
        batch.add_column(sa.Column("participant_mode", sa.String(32), nullable=False, server_default="unordered"))
        batch.add_column(sa.Column("role_definitions", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        batch.create_check_constraint(
            "ck_behavior_categories_participant_mode",
            "participant_mode IN ('unordered', 'role_based')",
        )

    with op.batch_alter_table("annotations") as batch:
        batch.add_column(sa.Column("participant_roles", sa.JSON(), nullable=True, server_default=sa.text("'{}'")))
        batch.add_column(sa.Column("participant_status", sa.String(32), nullable=False, server_default="valid"))
        batch.create_check_constraint(
            "ck_annotations_participant_status",
            "participant_status IN ('valid', 'needs_participants')",
        )

    with op.batch_alter_table("submission_annotations") as batch:
        batch.add_column(sa.Column("category_group", sa.String(64), nullable=True))
        batch.add_column(sa.Column("category_participant_mode", sa.String(32), nullable=False, server_default="unordered"))
        batch.add_column(sa.Column("role_definitions_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        batch.add_column(sa.Column("participant_roles_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
        batch.create_check_constraint(
            "ck_submission_annotations_participant_mode",
            "category_participant_mode IN ('unordered', 'role_based')",
        )

    op.create_table(
        "category_scheme_audits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("scheme_version", sa.Integer(), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=False),
        sa.Column("scheme_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("scheme_version >= 0", name="ck_category_scheme_audits_version"),
        sa.CheckConstraint("action IN ('replace', 'lock')", name="ck_category_scheme_audits_action"),
    )
    op.create_index(
        "ix_category_scheme_audits_project_created",
        "category_scheme_audits", ["project_id", "created_at"],
    )
    install_sqlite_authority_triggers(conn)


def downgrade():
    conn = op.get_bind()
    drop_sqlite_authority_triggers(conn)
    op.drop_index("ix_category_scheme_audits_project_created", table_name="category_scheme_audits")
    op.drop_table("category_scheme_audits")
    with op.batch_alter_table("submission_annotations") as batch:
        batch.drop_constraint("ck_submission_annotations_participant_mode", type_="check")
        batch.drop_column("participant_roles_snapshot")
        batch.drop_column("role_definitions_snapshot")
        batch.drop_column("category_participant_mode")
        batch.drop_column("category_group")
    with op.batch_alter_table("annotations") as batch:
        batch.drop_constraint("ck_annotations_participant_status", type_="check")
        batch.drop_column("participant_status")
        batch.drop_column("participant_roles")
    with op.batch_alter_table("behavior_categories") as batch:
        batch.drop_constraint("ck_behavior_categories_participant_mode", type_="check")
        batch.drop_column("role_definitions")
        batch.drop_column("participant_mode")
    with op.batch_alter_table("projects") as batch:
        batch.drop_constraint("fk_projects_category_scheme_locked_by_users", type_="foreignkey")
        batch.drop_constraint("ck_projects_category_scheme_lock_pair", type_="check")
        batch.drop_constraint("ck_projects_category_scheme_version", type_="check")
        batch.drop_column("category_scheme_locked_by")
        batch.drop_column("category_scheme_locked_at")
        batch.drop_column("category_scheme_version")
    install_sqlite_authority_triggers(conn)
