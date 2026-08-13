"""Stable source identity and immutable authority barriers. Revision ID: 0011; Revises: 0010."""
from alembic import op
import sqlalchemy as sa
from app.authority_triggers import install_sqlite_authority_triggers, drop_sqlite_authority_triggers
revision = "0011"
down_revision = "0010"
branch_labels = depends_on = None
def upgrade():
    with op.batch_alter_table("submissions") as batch:
        for name in ("source_file_size", "source_mtime_ns", "source_device", "source_inode"):
            batch.add_column(sa.Column(name, sa.BigInteger(), nullable=False, server_default="0"))
    conn = op.get_bind()
    if conn.dialect.name == "sqlite": install_sqlite_authority_triggers(conn)
def downgrade():
    conn = op.get_bind()
    if conn.dialect.name == "sqlite": drop_sqlite_authority_triggers(conn)
    with op.batch_alter_table("submissions") as batch:
        for name in ("source_inode", "source_device", "source_mtime_ns", "source_file_size"):
            batch.drop_column(name)
