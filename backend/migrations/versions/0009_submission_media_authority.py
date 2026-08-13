"""Allow new Clip rows to target only immutable SubmissionAnnotation.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("clips") as batch_op:
        batch_op.alter_column("project_id", existing_type=sa.Integer(), nullable=True)
        batch_op.alter_column("annotation_id", existing_type=sa.Integer(), nullable=True)
        batch_op.alter_column("source_revision", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    connection = op.get_bind()
    invalid = connection.execute(sa.text(
        "SELECT count(*) FROM clips WHERE project_id IS NULL OR annotation_id IS NULL OR source_revision IS NULL"
    )).scalar_one()
    if invalid:
        raise RuntimeError(
            "Cannot downgrade 0009 while SubmissionAnnotation-only Clip rows exist; "
            "remove only those transitional rows after an explicit backup"
        )
    with op.batch_alter_table("clips") as batch_op:
        batch_op.alter_column("source_revision", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("annotation_id", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("project_id", existing_type=sa.Integer(), nullable=False)
