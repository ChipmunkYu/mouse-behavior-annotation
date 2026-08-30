"""Add low-bitrate display proxy state and persistent job ownership.

Revision ID: 0016; Revises: 0015.
"""
from alembic import op
import sqlalchemy as sa

from app.assignee_triggers import drop_sqlite_assignee_triggers, install_sqlite_assignee_triggers
from app.authority_triggers import drop_sqlite_authority_triggers, install_sqlite_authority_triggers


revision = "0016"
down_revision = "0015"
branch_labels = depends_on = None


VIDEO_CHECKS = (
    ("ck_videos_display_status", "display_status IN ('pending', 'processing', 'ready', 'failed')"),
    ("ck_videos_source_sha256", "source_sha256 IS NULL OR (length(source_sha256) = 64 AND source_sha256 NOT GLOB '*[^0-9a-f]*')"),
    ("ck_videos_display_source_sha256", "display_source_sha256 IS NULL OR (length(display_source_sha256) = 64 AND display_source_sha256 NOT GLOB '*[^0-9a-f]*')"),
    ("ck_videos_display_source_required", "display_status NOT IN ('processing', 'ready') OR source_sha256 IS NOT NULL"),
    ("ck_videos_display_ready", "display_status <> 'ready' OR (display_path IS NOT NULL AND length(display_path) > 0 AND display_profile_version IS NOT NULL AND length(display_profile_version) > 0 AND display_source_sha256 IS NOT NULL AND display_source_sha256 = source_sha256 AND display_generated_at IS NOT NULL AND display_error IS NULL)"),
    ("ck_videos_display_nonready", "display_status = 'ready' OR (display_path IS NULL AND display_profile_version IS NULL AND display_source_sha256 IS NULL AND display_generated_at IS NULL)"),
    ("ck_videos_display_active_error", "display_status NOT IN ('pending', 'processing') OR display_error IS NULL"),
    ("ck_videos_display_failed_error", "display_status <> 'failed' OR (display_error IS NOT NULL AND length(display_error) > 0)"),
    ("ck_videos_display_path_safe", "display_status <> 'ready' OR (substr(display_path, 1, 1) <> '/' AND instr(display_path, '/') = 0 AND instr(display_path, '\\') = 0 AND instr(display_path, char(0)) = 0 AND instr(display_path, '..') = 0 AND instr(display_path, ':') = 0)"),
)

BACKGROUND_JOB_RUN_TOKEN_CHECK = (
    "ck_background_jobs_display_proxy_run_token",
    "job_type <> 'display_proxy' OR "
    "((status = 'running' AND run_token IS NOT NULL AND length(run_token) > 0) OR "
    "(status <> 'running' AND run_token IS NULL))",
)


def _drop_video_triggers(conn) -> None:
    drop_sqlite_authority_triggers(conn)
    drop_sqlite_assignee_triggers(conn)


def _install_video_triggers(conn) -> None:
    install_sqlite_authority_triggers(conn)
    install_sqlite_assignee_triggers(conn)


def upgrade() -> None:
    conn = op.get_bind()
    _drop_video_triggers(conn)
    with op.batch_alter_table("videos") as batch:
        batch.add_column(sa.Column("display_path", sa.String(512), nullable=True))
        batch.add_column(sa.Column("display_status", sa.String(32), nullable=False, server_default="pending"))
        batch.add_column(sa.Column("display_error", sa.Text(), nullable=True))
        batch.add_column(sa.Column("display_profile_version", sa.String(64), nullable=True))
        batch.add_column(sa.Column("source_sha256", sa.String(64), nullable=True))
        batch.add_column(sa.Column("display_source_sha256", sa.String(64), nullable=True))
        batch.add_column(sa.Column("display_generated_at", sa.DateTime(), nullable=True))
        for name, condition in VIDEO_CHECKS:
            batch.create_check_constraint(name, condition)
    _install_video_triggers(conn)
    with op.batch_alter_table("background_jobs") as batch:
        batch.add_column(sa.Column("run_token", sa.String(64), nullable=True))
        batch.create_check_constraint(*BACKGROUND_JOB_RUN_TOKEN_CHECK)


def downgrade() -> None:
    conn = op.get_bind()
    with op.batch_alter_table("background_jobs") as batch:
        batch.drop_constraint(BACKGROUND_JOB_RUN_TOKEN_CHECK[0], type_="check")
        batch.drop_column("run_token")
    _drop_video_triggers(conn)
    with op.batch_alter_table("videos") as batch:
        for name, _condition in reversed(VIDEO_CHECKS):
            batch.drop_constraint(name, type_="check")
        batch.drop_column("display_generated_at")
        batch.drop_column("display_source_sha256")
        batch.drop_column("source_sha256")
        batch.drop_column("display_profile_version")
        batch.drop_column("display_error")
        batch.drop_column("display_status")
        batch.drop_column("display_path")
    _install_video_triggers(conn)
