"""Add optional video distance calibration.

Revision ID: 0020; Revises: 0019.
"""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = depends_on = None


def upgrade() -> None:
    op.add_column("videos", sa.Column("distance_calibration", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("videos", "distance_calibration")
