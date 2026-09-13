"""Add per rejected-feedback '标记已修改' status records.

Revision ID: 0018; Revises: 0017.

Existing rows need no backfill: absence of a ``feedback_marks`` row is the
unmarked default, so all pre-existing rejected feedback stays unmarked.
"""
from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = depends_on = None


def upgrade() -> None:
    op.create_table(
        "feedback_marks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "submission_annotation_id", sa.Integer(),
            sa.ForeignKey("submission_annotations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "marked_by", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("marked_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("submission_annotation_id", name="uq_feedback_marks_snapshot"),
    )


def downgrade() -> None:
    op.drop_table("feedback_marks")
