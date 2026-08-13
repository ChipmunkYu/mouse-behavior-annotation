"""Add sparse detection state and immutable submission foundation.

This is intentionally additive.  Legacy identity, suppression, review, clip,
and export paths remain in place.  The backfill represents only the current
effective state because reverted legacy suppression detail rows were deleted.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from app.migrations_v0008 import (
    rebuild_sparse_detection_state,
    validate_legacy_current_state,
    validate_legacy_track_id_domain,
)
from app.track_ids import TRACK_ID_UPPER_BOUND

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backfill_detection_state() -> None:
    """Backfill monotonic ID cursors and active imports' current sparse state."""
    rebuild_sparse_detection_state(op.get_bind())


def upgrade() -> None:
    # Fail before any additive DDL.  Silent raw-ID fallback would make the new
    # current state disagree with the legacy authority.
    connection = op.get_bind()
    validate_legacy_track_id_domain(connection)
    validate_legacy_current_state(connection)

    with op.batch_alter_table("detection_imports") as batch_op:
        batch_op.add_column(
            sa.Column("edit_version", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("next_display_track_id", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.create_check_constraint(
            "ck_detection_imports_edit_version", "edit_version >= 0"
        )
        batch_op.create_check_constraint(
            "ck_detection_imports_next_display_track_id",
            f"next_display_track_id >= 0 AND next_display_track_id <= {TRACK_ID_UPPER_BOUND}",
        )

    with op.batch_alter_table("raw_detections") as batch_op:
        batch_op.create_unique_constraint(
            "uq_raw_detections_id_import", ["id", "detection_import_id"]
        )
        batch_op.create_check_constraint(
            "ck_raw_detections_frame_index", "frame_index >= 0"
        )
        batch_op.create_check_constraint(
            "ck_raw_detections_frame_detection_index", "frame_detection_index >= 0"
        )
        batch_op.create_check_constraint(
            "ck_raw_detections_track_id",
            f"raw_track_id >= 0 AND raw_track_id < {TRACK_ID_UPPER_BOUND}",
        )
    op.create_index(
        "ix_raw_detections_import_track_frame",
        "raw_detections",
        ["detection_import_id", "raw_track_id", "frame_index"],
    )

    op.create_table(
        "detection_state_overrides",
        sa.Column("raw_detection_id", sa.Integer(), primary_key=True),
        sa.Column("detection_import_id", sa.Integer(), nullable=False),
        sa.Column("display_track_id", sa.Integer(), nullable=False),
        sa.Column("suppressed", sa.Boolean(), nullable=False),
        sa.Column("updated_edit_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["detection_import_id"], ["detection_imports.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["raw_detection_id", "detection_import_id"],
            ["raw_detections.id", "raw_detections.detection_import_id"],
            ondelete="CASCADE",
            name="fk_detection_state_overrides_raw_import",
        ),
        sa.CheckConstraint(
            f"display_track_id >= 0 AND display_track_id < {TRACK_ID_UPPER_BOUND}",
            name="ck_detection_state_overrides_display",
        ),
        sa.CheckConstraint(
            "updated_edit_version >= 1", name="ck_detection_state_overrides_edit_version"
        ),
    )
    op.create_index(
        "ix_detection_state_overrides_import_display_suppressed",
        "detection_state_overrides",
        ["detection_import_id", "display_track_id", "suppressed"],
    )
    op.create_index(
        "ix_detection_state_overrides_import_version",
        "detection_state_overrides",
        ["detection_import_id", "updated_edit_version"],
    )

    op.create_table(
        "draft_identity_edits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("detection_import_id", sa.Integer(), nullable=False),
        sa.Column("applied_edit_version", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("operator_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["detection_import_id"], ["detection_imports.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["operator_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "detection_import_id",
            "applied_edit_version",
            name="uq_draft_identity_edits_import_version",
        ),
        sa.UniqueConstraint(
            "id", "detection_import_id", name="uq_draft_identity_edits_id_import"
        ),
        sa.CheckConstraint(
            "applied_edit_version >= 1", name="ck_draft_identity_edits_applied_version"
        ),
        sa.CheckConstraint(
            "operation IN ('split', 'merge', 'suppress_track')",
            name="ck_draft_identity_edits_operation",
        ),
    )
    op.create_index(
        "ix_draft_identity_edits_import_version",
        "draft_identity_edits",
        ["detection_import_id", sa.text("applied_edit_version DESC")],
    )

    op.create_table(
        "draft_detection_changes",
        sa.Column("edit_id", sa.Integer(), nullable=False),
        sa.Column("raw_detection_id", sa.Integer(), nullable=False),
        sa.Column("detection_import_id", sa.Integer(), nullable=False),
        sa.Column("before_override_exists", sa.Boolean(), nullable=False),
        sa.Column("before_display_track_id", sa.Integer(), nullable=True),
        sa.Column("before_suppressed", sa.Boolean(), nullable=True),
        sa.Column("after_override_exists", sa.Boolean(), nullable=False),
        sa.Column("after_display_track_id", sa.Integer(), nullable=True),
        sa.Column("after_suppressed", sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint("edit_id", "raw_detection_id"),
        sa.ForeignKeyConstraint(
            ["detection_import_id"], ["detection_imports.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["edit_id", "detection_import_id"],
            ["draft_identity_edits.id", "draft_identity_edits.detection_import_id"],
            ondelete="CASCADE",
            name="fk_draft_detection_changes_edit_import",
        ),
        sa.ForeignKeyConstraint(
            ["raw_detection_id", "detection_import_id"],
            ["raw_detections.id", "raw_detections.detection_import_id"],
            ondelete="CASCADE",
            name="fk_draft_detection_changes_raw_import",
        ),
        sa.CheckConstraint(
            "(before_override_exists = 0 AND before_display_track_id IS NULL "
            "AND before_suppressed IS NULL) OR "
            f"(before_override_exists = 1 AND before_display_track_id >= 0 "
            f"AND before_display_track_id < {TRACK_ID_UPPER_BOUND} "
            "AND before_suppressed IS NOT NULL)",
            name="ck_draft_detection_changes_before",
        ),
        sa.CheckConstraint(
            "(after_override_exists = 0 AND after_display_track_id IS NULL "
            "AND after_suppressed IS NULL) OR "
            f"(after_override_exists = 1 AND after_display_track_id >= 0 "
            f"AND after_display_track_id < {TRACK_ID_UPPER_BOUND} "
            "AND after_suppressed IS NOT NULL)",
            name="ck_draft_detection_changes_after",
        ),
    )
    op.create_index(
        "ix_draft_detection_changes_import_raw",
        "draft_detection_changes",
        ["detection_import_id", "raw_detection_id"],
    )

    op.create_table(
        "detection_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("detection_import_id", sa.Integer(), nullable=False),
        sa.Column("source_edit_version", sa.Integer(), nullable=False),
        sa.Column("raw_detection_count", sa.Integer(), nullable=False),
        sa.Column("override_count", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("fps", sa.Float(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("frame_count", sa.Integer(), nullable=False),
        sa.Column("keypoint_names", sa.JSON(), nullable=False),
        sa.Column("skeleton_edges", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["detection_import_id"], ["detection_imports.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "detection_import_id",
            "source_edit_version",
            name="uq_detection_snapshots_import_version",
        ),
        sa.UniqueConstraint("id", "detection_import_id", name="uq_detection_snapshots_id_import"),
        sa.CheckConstraint(
            "source_edit_version >= 0", name="ck_detection_snapshots_edit_version"
        ),
        sa.CheckConstraint(
            "raw_detection_count >= 0", name="ck_detection_snapshots_raw_count"
        ),
        sa.CheckConstraint(
            "override_count >= 0 AND override_count <= raw_detection_count",
            name="ck_detection_snapshots_override_count",
        ),
        sa.CheckConstraint("schema_version >= 1", name="ck_detection_snapshots_schema_version"),
        sa.CheckConstraint("fps > 0", name="ck_detection_snapshots_fps"),
        sa.CheckConstraint(
            "width > 0 AND height > 0", name="ck_detection_snapshots_dimensions"
        ),
        sa.CheckConstraint("frame_count >= 0", name="ck_detection_snapshots_frame_count"),
    )

    op.create_table(
        "detection_snapshot_states",
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("raw_detection_id", sa.Integer(), nullable=False),
        sa.Column("detection_import_id", sa.Integer(), nullable=False),
        sa.Column("display_track_id", sa.Integer(), nullable=False),
        sa.Column("suppressed", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_id", "raw_detection_id"),
        sa.ForeignKeyConstraint(
            ["detection_import_id"], ["detection_imports.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "detection_import_id"],
            ["detection_snapshots.id", "detection_snapshots.detection_import_id"],
            ondelete="RESTRICT",
            name="fk_detection_snapshot_states_snapshot_import",
        ),
        sa.ForeignKeyConstraint(
            ["raw_detection_id", "detection_import_id"],
            ["raw_detections.id", "raw_detections.detection_import_id"],
            ondelete="RESTRICT",
            name="fk_detection_snapshot_states_raw_import",
        ),
        sa.CheckConstraint(
            f"display_track_id >= 0 AND display_track_id < {TRACK_ID_UPPER_BOUND}",
            name="ck_detection_snapshot_states_display",
        ),
    )
    op.create_index(
        "ix_detection_snapshot_states_snapshot_display_suppressed",
        "detection_snapshot_states",
        ["snapshot_id", "display_track_id", "suppressed"],
    )

    op.create_table(
        "submissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("video_id", sa.Integer(), nullable=False),
        sa.Column("detection_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("source_annotation_version", sa.Integer(), nullable=False),
        sa.Column("source_media_revision", sa.Integer(), nullable=False),
        sa.Column("source_video_filename", sa.String(length=255), nullable=False),
        sa.Column("source_storage_key", sa.String(length=512), nullable=False),
        sa.Column("source_video_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("submitted_by", sa.Integer(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("legacy_backfill", sa.Boolean(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["detection_snapshot_id"], ["detection_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["submitted_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("video_id", "attempt_no", name="uq_submissions_video_attempt"),
        sa.CheckConstraint("attempt_no >= 1", name="ck_submissions_attempt_no"),
        sa.CheckConstraint(
            "source_annotation_version >= 0", name="ck_submissions_annotation_version"
        ),
        sa.CheckConstraint(
            "source_media_revision >= 0", name="ck_submissions_media_revision"
        ),
        sa.CheckConstraint(
            "status IN ('submitted', 'withdrawn', 'approved', 'rejected', 'superseded')",
            name="ck_submissions_status",
        ),
        sa.CheckConstraint(
            "length(source_video_sha256) = 64 "
            "AND source_video_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_submissions_video_sha256",
        ),
        sa.CheckConstraint(
            "length(source_storage_key) > 0 AND substr(source_storage_key, 1, 1) <> '/' "
            "AND source_storage_key NOT LIKE '%\\%' "
            "AND instr(source_storage_key, ':') = 0 "
            "AND source_storage_key NOT LIKE '%//%' "
            "AND ('/' || source_storage_key || '/') NOT LIKE '%/./%' "
            "AND ('/' || source_storage_key || '/') NOT LIKE '%/../%'",
            name="ck_submissions_storage_key",
        ),
    )
    op.create_index(
        "uq_submissions_video_submitted",
        "submissions",
        ["video_id"],
        unique=True,
        sqlite_where=sa.text("status = 'submitted'"),
    )
    op.create_index(
        "uq_submissions_video_approved",
        "submissions",
        ["video_id"],
        unique=True,
        sqlite_where=sa.text("status = 'approved'"),
    )
    op.create_index(
        "ix_submissions_detection_snapshot_id", "submissions", ["detection_snapshot_id"]
    )
    op.create_index(
        "ix_submissions_status_submitted_at", "submissions", ["status", "submitted_at"]
    )

    op.create_table(
        "submission_annotations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("submission_id", sa.Integer(), nullable=False),
        sa.Column("source_annotation_id", sa.Integer(), nullable=True),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("category_name", sa.String(length=64), nullable=False),
        sa.Column("start_time", sa.Float(), nullable=False),
        sa.Column("end_time", sa.Float(), nullable=False),
        sa.Column("start_frame", sa.Integer(), nullable=False),
        sa.Column("end_frame", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.String(length=32), nullable=False),
        sa.Column("crop_region", sa.JSON(), nullable=True),
        sa.Column("mouse_ids", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["submission_id"], ["submissions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_annotation_id"], ["annotations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["category_id"], ["behavior_categories.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "start_time >= 0 AND end_time > start_time",
            name="ck_submission_annotations_time_range",
        ),
        sa.CheckConstraint(
            "start_frame >= 0 AND end_frame >= start_frame",
            name="ck_submission_annotations_frame_range",
        ),
        sa.CheckConstraint(
            "confidence IN ('certain', 'uncertain', 'occluded')",
            name="ck_submission_annotations_confidence",
        ),
    )
    op.create_index(
        "ix_submission_annotations_submission_category",
        "submission_annotations",
        ["submission_id", "category_id"],
    )
    op.create_index(
        "ix_submission_annotations_category_submission",
        "submission_annotations",
        ["category_id", "submission_id"],
    )
    op.create_index(
        "uq_submission_annotations_submission_source",
        "submission_annotations",
        ["submission_id", "source_annotation_id"],
        unique=True,
        sqlite_where=sa.text("source_annotation_id IS NOT NULL"),
    )

    with op.batch_alter_table("reviews") as batch_op:
        batch_op.add_column(sa.Column("submission_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_reviews_submission_id", "submissions", ["submission_id"], ["id"], ondelete="RESTRICT"
        )
    op.create_index(
        "uq_reviews_submission_not_null",
        "reviews",
        ["submission_id"],
        unique=True,
        sqlite_where=sa.text("submission_id IS NOT NULL"),
    )

    with op.batch_alter_table("clips") as batch_op:
        batch_op.add_column(sa.Column("submission_annotation_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_clips_submission_annotation_id",
            "submission_annotations",
            ["submission_annotation_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.create_index(
        "uq_clips_submission_annotation_not_null",
        "clips",
        ["submission_annotation_id"],
        unique=True,
        sqlite_where=sa.text("submission_annotation_id IS NOT NULL"),
    )

    _backfill_detection_state()


def downgrade() -> None:
    op.drop_index("uq_clips_submission_annotation_not_null", table_name="clips")
    with op.batch_alter_table("clips") as batch_op:
        batch_op.drop_constraint("fk_clips_submission_annotation_id", type_="foreignkey")
        batch_op.drop_column("submission_annotation_id")

    op.drop_index("uq_reviews_submission_not_null", table_name="reviews")
    with op.batch_alter_table("reviews") as batch_op:
        batch_op.drop_constraint("fk_reviews_submission_id", type_="foreignkey")
        batch_op.drop_column("submission_id")

    op.drop_index(
        "uq_submission_annotations_submission_source", table_name="submission_annotations"
    )
    op.drop_index(
        "ix_submission_annotations_category_submission", table_name="submission_annotations"
    )
    op.drop_index(
        "ix_submission_annotations_submission_category", table_name="submission_annotations"
    )
    op.drop_table("submission_annotations")
    op.drop_index("ix_submissions_status_submitted_at", table_name="submissions")
    op.drop_index("ix_submissions_detection_snapshot_id", table_name="submissions")
    op.drop_index("uq_submissions_video_approved", table_name="submissions")
    op.drop_index("uq_submissions_video_submitted", table_name="submissions")
    op.drop_table("submissions")
    op.drop_index(
        "ix_detection_snapshot_states_snapshot_display_suppressed",
        table_name="detection_snapshot_states",
    )
    op.drop_table("detection_snapshot_states")
    op.drop_table("detection_snapshots")
    op.drop_index(
        "ix_draft_detection_changes_import_raw", table_name="draft_detection_changes"
    )
    op.drop_table("draft_detection_changes")
    op.drop_index("ix_draft_identity_edits_import_version", table_name="draft_identity_edits")
    op.drop_table("draft_identity_edits")
    op.drop_index(
        "ix_detection_state_overrides_import_version", table_name="detection_state_overrides"
    )
    op.drop_index(
        "ix_detection_state_overrides_import_display_suppressed",
        table_name="detection_state_overrides",
    )
    op.drop_table("detection_state_overrides")

    op.drop_index("ix_raw_detections_import_track_frame", table_name="raw_detections")
    with op.batch_alter_table("raw_detections") as batch_op:
        batch_op.drop_constraint("ck_raw_detections_track_id", type_="check")
        batch_op.drop_constraint("ck_raw_detections_frame_detection_index", type_="check")
        batch_op.drop_constraint("ck_raw_detections_frame_index", type_="check")
        batch_op.drop_constraint("uq_raw_detections_id_import", type_="unique")

    with op.batch_alter_table("detection_imports") as batch_op:
        batch_op.drop_constraint("ck_detection_imports_next_display_track_id", type_="check")
        batch_op.drop_constraint("ck_detection_imports_edit_version", type_="check")
        batch_op.drop_column("next_display_track_id")
        batch_op.drop_column("edit_version")
