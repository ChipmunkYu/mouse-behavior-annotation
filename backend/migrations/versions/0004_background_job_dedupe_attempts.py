"""增量：BackgroundJob 幂等去重键与重试计数（批次 4 媒体任务）

- background_jobs 新增：
  - `dedupe_key`（可空 + 唯一索引）：同一视频+修订的媒体任务只允许一行 Job，
    并发 submit/review 重复竞态由该唯一索引兜底防重复任务；SQLite 唯一索引
    允许多个 NULL（非媒体任务不受影响）。
  - `attempts`（NOT NULL 默认 0）：任务领取/中断重排次数，用于重启恢复时
    “running 视为中断 → 重排或判失败”的重试上限判定。

使用 SQLite 批处理（重建表并拷贝数据），已有数据与原有列完整保留。

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("background_jobs") as batch_op:
        batch_op.add_column(sa.Column("dedupe_key", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column("attempts", sa.Integer(), server_default="0", nullable=False)
        )
    # 唯一索引承担 dedupe 约束（nullable 列在 SQLite 允许多个 NULL）
    op.create_index("ix_background_jobs_dedupe_key", "background_jobs", ["dedupe_key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_background_jobs_dedupe_key", table_name="background_jobs")
    with op.batch_alter_table("background_jobs") as batch_op:
        batch_op.drop_column("attempts")
        batch_op.drop_column("dedupe_key")
