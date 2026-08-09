"""增量：VideoImportBatch 文件路径与视频关联（Phase 1B）

新增列（全部可空，旧批次不受影响）：
- video_path / video_filename：上传视频的文件路径（相对 videos_dir）与原始文件名
- tracks_path / metadata_path：上传 tracks/metadata 文件路径（相对 detection_imports_dir）
- created_video_id：批次完成后创建的 Video（FK → videos.id，SET NULL）

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("video_import_batches") as batch_op:
        batch_op.add_column(sa.Column("video_path", sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column("video_filename", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("tracks_path", sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column("metadata_path", sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column("created_video_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_video_import_batches_created_video_id",
            "videos",
            ["created_video_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("video_import_batches") as batch_op:
        batch_op.drop_constraint("fk_video_import_batches_created_video_id", type_="foreignkey")
        batch_op.drop_column("created_video_id")
        batch_op.drop_column("metadata_path")
        batch_op.drop_column("tracks_path")
        batch_op.drop_column("video_filename")
        batch_op.drop_column("video_path")
