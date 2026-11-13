"""Persist immutable heuristic input and the fourth batch slot.

Revision ID: 0021; Revises: 0020.
"""
from alembic import op
import sqlalchemy as sa

from app.authority_triggers import (drop_sqlite_authority_triggers,
                                    install_sqlite_authority_triggers)

revision = "0021"
down_revision = "0020"
branch_labels = depends_on = None


def upgrade():
    drop_sqlite_authority_triggers(op.get_bind())
    op.add_column("videos", sa.Column("source", sa.String(16), sa.CheckConstraint(
        "source IN ('manual','heuristic')", name="ck_videos_source"),
        nullable=False, server_default="manual"))
    op.add_column("videos", sa.Column("judgment_status", sa.String(16), nullable=True))
    op.add_column("videos", sa.Column("heuristic_input", sa.JSON(none_as_null=True), sa.CheckConstraint(
        "(source='manual' AND judgment_status IS NULL AND heuristic_input IS NULL) OR "
        "(source='heuristic' AND judgment_status IN ('pending','usable','unusable') "
        "AND judgment_status IS NOT NULL AND heuristic_input IS NOT NULL)", name="ck_videos_judgment"), nullable=True))
    op.add_column("video_import_batches", sa.Column("source", sa.String(16), nullable=False, server_default="manual"))
    op.add_column("video_import_batches", sa.Column("annotation_upload_state", sa.String(32), nullable=False, server_default="pending"))
    op.add_column("video_import_batches", sa.Column("annotation_path", sa.String(512), nullable=True))
    with op.batch_alter_table("submissions") as batch:
        batch.drop_constraint("ck_submissions_status", type_="check")
        batch.create_check_constraint("ck_submissions_status",
            "status IN ('submitted', 'withdrawn', 'approved', 'rejected', 'unusable', 'superseded')")
    install_sqlite_authority_triggers(op.get_bind())


def downgrade():
    connection = op.get_bind()
    if (connection.exec_driver_sql("SELECT 1 FROM videos WHERE source='heuristic' LIMIT 1").first()
            or connection.exec_driver_sql("SELECT 1 FROM video_import_batches WHERE source='heuristic' LIMIT 1").first()):
        raise RuntimeError("Cannot downgrade while heuristic evidence exists")
    # External triggers referencing videos also block SQLite's table rebuild.
    triggers = connection.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger'").all()
    for name, _sql in triggers:
        connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
    with op.batch_alter_table("submissions") as batch:
        batch.drop_constraint("ck_submissions_status", type_="check")
        batch.create_check_constraint("ck_submissions_status",
            "status IN ('submitted', 'withdrawn', 'approved', 'rejected', 'superseded')")
    for column in ("annotation_path", "annotation_upload_state", "source"):
        op.drop_column("video_import_batches", column)
    # SQLite DROP COLUMN cannot remove a column referenced by a named CHECK.
    with op.batch_alter_table("videos") as batch:
        batch.drop_constraint("ck_videos_judgment", type_="check")
        batch.drop_constraint("ck_videos_source", type_="check")
        for column in ("heuristic_input", "judgment_status", "source"):
            batch.drop_column(column)
    for name, sql in triggers:
        if not name.startswith("trg_heuristic_"):
            connection.exec_driver_sql(sql)
