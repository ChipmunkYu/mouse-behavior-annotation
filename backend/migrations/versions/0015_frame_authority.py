"""Enforce multi-frame inclusive annotation intervals.

Revision ID: 0015; Revises: 0014.
"""
from alembic import op
import sqlalchemy as sa

from app.authority_triggers import drop_sqlite_authority_triggers, install_sqlite_authority_triggers

revision = "0015"
down_revision = "0014"
branch_labels = depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dirty_live = conn.execute(sa.text(
        "SELECT count(*) FROM annotations WHERE end_frame <= start_frame"
    )).scalar_one()
    dirty_frozen = conn.execute(sa.text(
        "SELECT count(*) FROM submission_annotations WHERE end_frame <= start_frame"
    )).scalar_one()
    if dirty_live or dirty_frozen:
        raise RuntimeError(
            "Cannot upgrade 0015 while single-frame or reversed intervals exist "
            f"(annotations={dirty_live}, submission_annotations={dirty_frozen}); "
            "back up the database and explicitly correct or remove those rows before retrying"
        )
    drop_sqlite_authority_triggers(conn)
    with op.batch_alter_table("annotations") as batch:
        batch.create_check_constraint(
            "ck_annotations_frame_range", "start_frame >= 0 AND end_frame > start_frame"
        )
    with op.batch_alter_table("submission_annotations") as batch:
        batch.drop_constraint("ck_submission_annotations_frame_range", type_="check")
        batch.create_check_constraint(
            "ck_submission_annotations_frame_range",
            "start_frame >= 0 AND end_frame > start_frame",
        )
    install_sqlite_authority_triggers(conn)


def downgrade() -> None:
    conn = op.get_bind()
    drop_sqlite_authority_triggers(conn)
    with op.batch_alter_table("submission_annotations") as batch:
        batch.drop_constraint("ck_submission_annotations_frame_range", type_="check")
        batch.create_check_constraint(
            "ck_submission_annotations_frame_range",
            "start_frame >= 0 AND end_frame >= start_frame",
        )
    with op.batch_alter_table("annotations") as batch:
        batch.drop_constraint("ck_annotations_frame_range", type_="check")
    install_sqlite_authority_triggers(conn)
