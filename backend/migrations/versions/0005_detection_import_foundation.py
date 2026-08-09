"""增量：YOLO 检测导入与身份修正基础（Phase 1A，对应《YOLO 检测结果接入与身份校正设计》§10）

现有表新增列（全部带 server_default，旧数据保留）：

- behavior_categories：mouse_count_min（默认 1）、mouse_count_max（可空，NULL=无上限）；
  并补两类数量范围检查约束（min>=1，max 为空或 >=min）。
- videos：detection_import_revision / identity_revision（默认 0，尚无导入/修正），
  media_revision（默认 1，源媒体未变化）。
- annotations：mouse_ids（默认 []）、mouse_id_status（默认 needs_mouse_ids）、
  detection_import_revision / identity_revision（默认 0）——旧标注迁移为“缺参与小鼠”。
- reviews：detection_import_revision / identity_revision（默认 0，审核快照的三修订契约）。
- clips：media_revision（默认 1），供媒体修订与语义修订拆分（避免 mouse_ids 变化无谓重编码）。

新增表：

- video_import_batches：三文件（video/tracks/metadata）独立上传批次与槽位状态。
- detection_imports：一次结构化检测导入修订；UNIQUE(video_id, revision)；
  部分唯一索引保证同一视频只有一个 active 导入。
- raw_detections：导入后不可变的原始检测；
  UNIQUE(detection_import_id, frame_index, frame_detection_index)。
- corrected_tracks：修正轨迹（Split/Merge 产物）；
  部分唯一索引：活动轨迹满足 UNIQUE(detection_import_id, display_track_id)。
- corrected_detection_assignments：物化映射（raw → corrected + identity_revision）；
  UNIQUE(raw_detection_id, identity_revision)。
- identity_edits：Split/Merge/撤销 审计。
- detection_suppressions：误检抑制（冻结 detection 集合，可撤销）。
- suppression_detections：抑制作用的具体检测集合（复合主键）。

数据迁移：按《需求文档》§2.4 的类别数量表设置既有类别的 mouse_count_min/max
（奔跑/行走/静止/孤立行为→1/1，一起/接近/追逐/回避/攻击行为/鼻头接触/鼻尾接触→2/2，
扎堆行为→2/NULL）。

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _category_mouse_count_migration() -> None:
    """按行为类别名称设置参与小鼠数量范围（与 seed 种子数据一致）。"""
    op.execute(
        sa.text(
            "UPDATE behavior_categories SET mouse_count_min = 1, mouse_count_max = 1 "
            "WHERE name IN ('奔跑', '行走', '静止', '孤立行为')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE behavior_categories SET mouse_count_min = 2, mouse_count_max = 2 "
            "WHERE name IN ('一起', '接近', '追逐', '回避', '攻击行为', '鼻头接触', '鼻尾接触')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE behavior_categories SET mouse_count_min = 2, mouse_count_max = NULL "
            "WHERE name = '扎堆行为'"
        )
    )


def upgrade() -> None:
    # ---------- behavior_categories：参与小鼠数量范围 ----------
    with op.batch_alter_table("behavior_categories") as batch_op:
        batch_op.add_column(
            sa.Column("mouse_count_min", sa.Integer(), server_default="1", nullable=False)
        )
        batch_op.add_column(sa.Column("mouse_count_max", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            "ck_behavior_categories_mouse_count_min", "mouse_count_min >= 1"
        )
        batch_op.create_check_constraint(
            "ck_behavior_categories_mouse_count_max",
            "mouse_count_max IS NULL OR mouse_count_max >= mouse_count_min",
        )

    # ---------- videos：三类修订（media / detection import / identity） ----------
    with op.batch_alter_table("videos") as batch_op:
        batch_op.add_column(
            sa.Column("detection_import_revision", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("identity_revision", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("media_revision", sa.Integer(), server_default="1", nullable=False)
        )

    # ---------- annotations：内嵌小鼠 ID 与身份修订 ----------
    with op.batch_alter_table("annotations") as batch_op:
        batch_op.add_column(
            sa.Column("mouse_ids", sa.JSON(), server_default="[]", nullable=False)
        )
        batch_op.add_column(
            sa.Column(
                "mouse_id_status",
                sa.String(length=32),
                server_default="needs_mouse_ids",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("detection_import_revision", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("identity_revision", sa.Integer(), server_default="0", nullable=False)
        )

    # ---------- reviews：审核快照补足三修订 ----------
    with op.batch_alter_table("reviews") as batch_op:
        batch_op.add_column(
            sa.Column("detection_import_revision", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("identity_revision", sa.Integer(), server_default="0", nullable=False)
        )

    # ---------- clips：媒体修订（与语义修订拆分） ----------
    with op.batch_alter_table("clips") as batch_op:
        batch_op.add_column(
            sa.Column("media_revision", sa.Integer(), server_default="1", nullable=False)
        )

    # ---------- video_import_batches：三文件上传批次 ----------
    op.create_table(
        "video_import_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        # uploading / validating / ready / failed
        sa.Column("status", sa.String(length=32), server_default="uploading", nullable=False),
        sa.Column("validation_errors", sa.JSON(), nullable=True),
        # video / tracks / metadata 三个文件槽位状态：pending / uploading / uploaded / validated / failed
        sa.Column(
            "video_upload_state", sa.String(length=32), server_default="pending", nullable=False
        ),
        sa.Column(
            "tracks_upload_state", sa.String(length=32), server_default="pending", nullable=False
        ),
        sa.Column(
            "metadata_upload_state", sa.String(length=32), server_default="pending", nullable=False
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_video_import_batches_project_id", "video_import_batches", ["project_id"]
    )

    # ---------- detection_imports：结构化检测导入修订 ----------
    op.create_table(
        "detection_imports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("video_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        # tracks.jsonl / metadata.json 相对路径与校验和（不可变，供审计与重建）
        sa.Column("tracks_path", sa.String(length=512), nullable=True),
        sa.Column("tracks_sha256", sa.String(length=64), nullable=True),
        sa.Column("metadata_path", sa.String(length=512), nullable=True),
        sa.Column("metadata_sha256", sa.String(length=64), nullable=True),
        # 模型 / tracker 信息
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("model_weights_sha256", sa.String(length=64), nullable=True),
        sa.Column("tracker_name", sa.String(length=128), nullable=True),
        sa.Column("tracker_params", sa.JSON(), nullable=True),
        # 媒体统计与覆盖范围
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("fps", sa.Float(), nullable=True),
        sa.Column("frame_count", sa.Integer(), nullable=True),
        sa.Column("frame_range", sa.JSON(), nullable=True),  # [first_frame, last_frame]
        sa.Column("detection_count", sa.Integer(), nullable=True),
        # pending / imported / failed
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        # 一个视频同一时刻只允许一个 active 导入（部分唯一索引兜底）
        sa.Column("active", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("video_id", "revision", name="uq_detection_imports_video_revision"),
    )
    op.create_index("ix_detection_imports_video_id", "detection_imports", ["video_id"])
    op.create_index(
        "uq_detection_imports_active_video",
        "detection_imports",
        ["video_id"],
        unique=True,
        sqlite_where=sa.text("active = 1"),
    )

    # ---------- raw_detections：不可变原始检测 ----------
    op.create_table(
        "raw_detections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("detection_import_id", sa.Integer(), nullable=False),
        sa.Column("frame_index", sa.Integer(), nullable=False),
        sa.Column("frame_detection_index", sa.Integer(), nullable=False),
        sa.Column("raw_track_id", sa.Integer(), nullable=False),
        sa.Column("box", sa.JSON(), nullable=True),
        sa.Column("keypoints", sa.JSON(), nullable=True),
        sa.Column("detection_confidence", sa.Float(), nullable=True),
        sa.Column("class_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["detection_import_id"], ["detection_imports.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "detection_import_id",
            "frame_index",
            "frame_detection_index",
            name="uq_raw_detections_import_frame_index",
        ),
    )
    op.create_index("ix_raw_detections_import_id", "raw_detections", ["detection_import_id"])
    op.create_index(
        "ix_raw_detections_import_frame", "raw_detections", ["detection_import_id", "frame_index"]
    )
    op.create_index(
        "ix_raw_detections_import_track", "raw_detections", ["detection_import_id", "raw_track_id"]
    )

    # ---------- corrected_tracks：修正轨迹 ----------
    op.create_table(
        "corrected_tracks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("detection_import_id", sa.Integer(), nullable=False),
        sa.Column("display_track_id", sa.Integer(), nullable=False),
        sa.Column("first_frame", sa.Integer(), nullable=True),
        sa.Column("last_frame", sa.Integer(), nullable=True),
        sa.Column("effective_detection_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_identity_revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("merged_into_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["detection_import_id"], ["detection_imports.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["merged_into_id"], ["corrected_tracks.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_corrected_tracks_import_id", "corrected_tracks", ["detection_import_id"])
    # 当前活动轨迹唯一：UNIQUE(detection_import_id, display_track_id) WHERE active=1
    op.create_index(
        "uq_corrected_tracks_active_display",
        "corrected_tracks",
        ["detection_import_id", "display_track_id"],
        unique=True,
        sqlite_where=sa.text("active = 1"),
    )

    # ---------- corrected_detection_assignments：物化映射 ----------
    op.create_table(
        "corrected_detection_assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("raw_detection_id", sa.Integer(), nullable=False),
        sa.Column("corrected_track_id", sa.Integer(), nullable=False),
        sa.Column("identity_revision", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["raw_detection_id"], ["raw_detections.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["corrected_track_id"], ["corrected_tracks.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "raw_detection_id", "identity_revision", name="uq_cda_raw_detection_revision"
        ),
    )
    op.create_index(
        "ix_cda_raw_detection_id", "corrected_detection_assignments", ["raw_detection_id"]
    )
    op.create_index(
        "ix_cda_corrected_track_id", "corrected_detection_assignments", ["corrected_track_id"]
    )

    # ---------- identity_edits：Split / Merge / 撤销 审计 ----------
    op.create_table(
        "identity_edits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("video_id", sa.Integer(), nullable=False),
        sa.Column("detection_import_id", sa.Integer(), nullable=False),
        # split / merge / revert
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("base_identity_revision", sa.Integer(), nullable=False),
        sa.Column("result_identity_revision", sa.Integer(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("affected_detections", sa.JSON(), nullable=True),
        sa.Column("affected_annotations", sa.JSON(), nullable=True),
        sa.Column("operator_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("reverted_edit_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["detection_import_id"], ["detection_imports.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["operator_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reverted_edit_id"], ["identity_edits.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_identity_edits_video_id", "identity_edits", ["video_id"])
    op.create_index(
        "ix_identity_edits_detection_import_id", "identity_edits", ["detection_import_id"]
    )

    # ---------- detection_suppressions：误检抑制（可撤销） ----------
    op.create_table(
        "detection_suppressions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("video_id", sa.Integer(), nullable=False),
        sa.Column("detection_import_id", sa.Integer(), nullable=False),
        sa.Column("base_identity_revision", sa.Integer(), nullable=False),
        sa.Column("result_identity_revision", sa.Integer(), nullable=False),
        # single_detection / corrected_track
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("operator_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("reverted_suppression_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["detection_import_id"], ["detection_imports.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["operator_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["reverted_suppression_id"], ["detection_suppressions.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_detection_suppressions_video_id", "detection_suppressions", ["video_id"])
    op.create_index(
        "ix_detection_suppressions_import_id", "detection_suppressions", ["detection_import_id"]
    )

    # ---------- suppression_detections：抑制冻结的检测集合 ----------
    op.create_table(
        "suppression_detections",
        sa.Column("suppression_id", sa.Integer(), nullable=False),
        sa.Column("raw_detection_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["suppression_id"], ["detection_suppressions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["raw_detection_id"], ["raw_detections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("suppression_id", "raw_detection_id"),
    )

    # ---------- 数据迁移：按名称设置类别参与小鼠数量范围 ----------
    _category_mouse_count_migration()


def downgrade() -> None:
    op.drop_table("suppression_detections")
    op.drop_index("ix_detection_suppressions_import_id", table_name="detection_suppressions")
    op.drop_index("ix_detection_suppressions_video_id", table_name="detection_suppressions")
    op.drop_table("detection_suppressions")
    op.drop_index("ix_identity_edits_detection_import_id", table_name="identity_edits")
    op.drop_index("ix_identity_edits_video_id", table_name="identity_edits")
    op.drop_table("identity_edits")
    op.drop_index("ix_cda_corrected_track_id", table_name="corrected_detection_assignments")
    op.drop_index("ix_cda_raw_detection_id", table_name="corrected_detection_assignments")
    op.drop_table("corrected_detection_assignments")
    op.drop_index("uq_corrected_tracks_active_display", table_name="corrected_tracks")
    op.drop_index("ix_corrected_tracks_import_id", table_name="corrected_tracks")
    op.drop_table("corrected_tracks")
    op.drop_index("ix_raw_detections_import_track", table_name="raw_detections")
    op.drop_index("ix_raw_detections_import_frame", table_name="raw_detections")
    op.drop_index("ix_raw_detections_import_id", table_name="raw_detections")
    op.drop_table("raw_detections")
    op.drop_index("uq_detection_imports_active_video", table_name="detection_imports")
    op.drop_index("ix_detection_imports_video_id", table_name="detection_imports")
    op.drop_table("detection_imports")
    op.drop_index("ix_video_import_batches_project_id", table_name="video_import_batches")
    op.drop_table("video_import_batches")

    with op.batch_alter_table("clips") as batch_op:
        batch_op.drop_column("media_revision")

    with op.batch_alter_table("reviews") as batch_op:
        batch_op.drop_column("identity_revision")
        batch_op.drop_column("detection_import_revision")

    with op.batch_alter_table("annotations") as batch_op:
        batch_op.drop_column("identity_revision")
        batch_op.drop_column("detection_import_revision")
        batch_op.drop_column("mouse_id_status")
        batch_op.drop_column("mouse_ids")

    with op.batch_alter_table("videos") as batch_op:
        batch_op.drop_column("media_revision")
        batch_op.drop_column("identity_revision")
        batch_op.drop_column("detection_import_revision")

    with op.batch_alter_table("behavior_categories") as batch_op:
        batch_op.drop_constraint("ck_behavior_categories_mouse_count_max", type_="check")
        batch_op.drop_constraint("ck_behavior_categories_mouse_count_min", type_="check")
        batch_op.drop_column("mouse_count_max")
        batch_op.drop_column("mouse_count_min")
