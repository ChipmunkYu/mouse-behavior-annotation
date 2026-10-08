"""Add soft-retirement metadata to clips.

Revision ID: 0019; Revises: 0018.
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = depends_on = None


def upgrade() -> None:
    op.add_column("clips", sa.Column("retired_at", sa.DateTime(), nullable=True))
    op.add_column("clips", sa.Column("retired_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("clips", "retired_reason")
    op.drop_column("clips", "retired_at")
