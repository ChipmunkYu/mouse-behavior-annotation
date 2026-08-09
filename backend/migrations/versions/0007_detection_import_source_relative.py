"""增量：DetectionImport 源视频相对路径

已部署的 0005/0006 数据库缺少 ORM 使用的 source_relative 列。该列必须通过
新的前向迁移补充，不能回写已经发布的历史迁移。

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("detection_imports") as batch_op:
        batch_op.add_column(sa.Column("source_relative", sa.String(length=512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("detection_imports") as batch_op:
        batch_op.drop_column("source_relative")
