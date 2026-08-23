"""Import-batch creator ownership and activity timestamps.

Revision ID: 0014; Revises: 0013.
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table("video_import_batches") as batch:
        batch.add_column(sa.Column("created_by", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))
        batch.create_foreign_key(
            "fk_video_import_batches_created_by_users", "users",
            ["created_by"], ["id"], ondelete="SET NULL",
        )
        batch.create_index("ix_video_import_batches_created_by", ["created_by"])
    op.execute(
        "UPDATE video_import_batches "
        "SET updated_at = COALESCE(created_at, CURRENT_TIMESTAMP)"
    )
    with op.batch_alter_table("video_import_batches") as batch:
        batch.alter_column("updated_at", existing_type=sa.DateTime(), nullable=False)


def downgrade():
    with op.batch_alter_table("video_import_batches") as batch:
        batch.drop_index("ix_video_import_batches_created_by")
        batch.drop_constraint("fk_video_import_batches_created_by_users", type_="foreignkey")
        batch.drop_column("updated_at")
        batch.drop_column("created_by")
