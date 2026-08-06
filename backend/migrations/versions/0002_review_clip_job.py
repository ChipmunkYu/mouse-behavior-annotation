"""增量：Video 工作流字段 + Review / Clip / BackgroundJob 三张新表

- videos 新增：workflow_status（默认 draft）、annotation_revision（>=1，默认 1）、
  submitted_at / approved_at / approved_by（可空）。
  使用 SQLite 批处理（重建表并拷贝数据），已有数据与原有列完整保留。
- reviews：审核历史表（可空 reviewer_id + ON DELETE SET NULL，用户删除不销毁历史）。
- clips：片段表，修订隔离：annotation_id + source_revision 唯一，修改标注后新修订生成新 clip。
- background_jobs：后台任务表（clip / export / cleanup 共用），progress 0..100 检查约束。

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------- videos：新增工作流字段（批处理重建表，保留数据） ----------
    with op.batch_alter_table("videos") as batch_op:
        batch_op.add_column(
            sa.Column("workflow_status", sa.String(length=32), server_default="draft", nullable=False)
        )
        batch_op.add_column(
            sa.Column("annotation_revision", sa.Integer(), server_default="1", nullable=False)
        )
        batch_op.add_column(sa.Column("submitted_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("approved_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "approved_by",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_videos_approved_by_users"),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "ck_videos_annotation_revision_min", "annotation_revision >= 1"
        )

    # ---------- reviews：审核历史 ----------
    op.create_table(
        "reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("video_id", sa.Integer(), nullable=False),
        sa.Column("reviewer_id", sa.Integer(), nullable=True),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("annotation_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewer_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_reviews_project_id", "reviews", ["project_id"])
    op.create_index("ix_reviews_video_id", "reviews", ["video_id"])
    op.create_index("ix_reviews_video_revision", "reviews", ["video_id", "annotation_revision"])
    op.create_index("ix_reviews_reviewer_id", "reviews", ["reviewer_id"])

    # ---------- clips：片段（修订隔离） ----------
    op.create_table(
        "clips",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("annotation_id", sa.Integer(), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("clip_path", sa.String(length=512), nullable=True),
        sa.Column("thumbnail_path", sa.String(length=512), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["annotation_id"], ["annotations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("annotation_id", "source_revision", name="uq_clip_annotation_revision"),
    )
    op.create_index("ix_clips_project_id", "clips", ["project_id"])
    op.create_index("ix_clips_annotation_id", "clips", ["annotation_id"])
    op.create_index("ix_clips_status", "clips", ["status"])

    # ---------- background_jobs：后台任务 ----------
    op.create_table(
        "background_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("result_path", sa.String(length=512), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "progress >= 0 AND progress <= 100", name="ck_background_jobs_progress_range"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_background_jobs_project_id", "background_jobs", ["project_id"])
    op.create_index("ix_background_jobs_status", "background_jobs", ["status"])
    op.create_index("ix_background_jobs_type_status", "background_jobs", ["job_type", "status"])


def downgrade() -> None:
    op.drop_index("ix_background_jobs_type_status", table_name="background_jobs")
    op.drop_index("ix_background_jobs_status", table_name="background_jobs")
    op.drop_index("ix_background_jobs_project_id", table_name="background_jobs")
    op.drop_table("background_jobs")
    op.drop_index("ix_clips_status", table_name="clips")
    op.drop_index("ix_clips_annotation_id", table_name="clips")
    op.drop_index("ix_clips_project_id", table_name="clips")
    op.drop_table("clips")
    op.drop_index("ix_reviews_reviewer_id", table_name="reviews")
    op.drop_index("ix_reviews_video_revision", table_name="reviews")
    op.drop_index("ix_reviews_video_id", table_name="reviews")
    op.drop_index("ix_reviews_project_id", table_name="reviews")
    op.drop_table("reviews")
    with op.batch_alter_table("videos") as batch_op:
        batch_op.drop_constraint("ck_videos_annotation_revision_min", type_="check")
        batch_op.drop_column("approved_by")
        batch_op.drop_column("approved_at")
        batch_op.drop_column("submitted_at")
        batch_op.drop_column("annotation_revision")
        batch_op.drop_column("workflow_status")
