"""Add behavior-level review audit and material comparison evidence.

Revision ID: 0017; Revises: 0016.
"""
import hashlib
import json

from alembic import op
import sqlalchemy as sa

from app.authority_triggers import drop_sqlite_authority_triggers, install_sqlite_authority_triggers

revision = "0017"
down_revision = "0016"
branch_labels = depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    drop_sqlite_authority_triggers(conn)
    with op.batch_alter_table("annotations") as batch:
        batch.add_column(sa.Column("material_revision", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("material_digest", sa.String(64), nullable=True))
        batch.add_column(sa.Column("material_state", sa.JSON(), nullable=True))
    with op.batch_alter_table("submissions") as batch:
        batch.add_column(sa.Column("decision_revision", sa.Integer(), nullable=False, server_default="0"))
    with op.batch_alter_table("submission_annotations") as batch:
        batch.add_column(sa.Column("source_annotation_key", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("source_material_revision", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("material_digest", sa.String(64), nullable=False, server_default="legacy"))
        batch.create_index("ix_submission_annotations_source_annotation_key", ["source_annotation_key"])
    conn.exec_driver_sql(
        "UPDATE submission_annotations SET source_annotation_key=COALESCE(source_annotation_id, -id)"
    )
    with op.batch_alter_table("submission_annotations") as batch:
        batch.alter_column("source_annotation_key", existing_type=sa.Integer(), nullable=False)
    op.create_table(
        "behavior_review_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("submission_annotation_id", sa.Integer(), sa.ForeignKey("submission_annotations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("reviewer_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("carried_from_decision_id", sa.Integer(), sa.ForeignKey("behavior_review_decisions.id", ondelete="SET NULL"), nullable=True),
        sa.CheckConstraint("status IN ('pending','approved','rejected')", name="ck_behavior_decisions_status"),
        sa.CheckConstraint("status <> 'rejected' OR (feedback IS NOT NULL AND length(trim(feedback)) > 0)", name="ck_behavior_decisions_rejected_feedback"),
        sa.UniqueConstraint("submission_annotation_id", "sequence", name="uq_behavior_decisions_snapshot_sequence"),
    )
    op.create_index("ix_behavior_decisions_snapshot_sequence", "behavior_review_decisions", ["submission_annotation_id", "sequence"])
    op.create_table(
        "behavior_review_reopens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("submission_id", sa.Integer(), sa.ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_behavior_review_reopens_submission_id", "behavior_review_reopens", ["submission_id"])
    # 0016 allowed a later rejected attempt to coexist with an older approved one.
    # Only the newest attempt is current authority; preserve the Review audit while
    # normalizing superseded approval status before runtime gates become stricter.
    conn.execute(sa.text(
        "UPDATE submissions AS old SET status='superseded' "
        "WHERE old.status='approved' AND EXISTS ("
        "SELECT 1 FROM submissions newer WHERE newer.video_id=old.video_id "
        "AND newer.attempt_no>old.attempt_no)"
    ))
    # Freeze the recoverable legacy material; old video rejection proves no per-row rejection.
    for table in ("annotations", "submission_annotations"):
        for row in conn.execute(sa.text(f"SELECT * FROM {table}")).mappings().all():
            payload = {key: row[key] for key in
                       ("category_id", "start_frame", "end_frame", "confidence")}
            for key in ("crop_region", "mouse_ids", "participant_roles"):
                column = "participant_roles_snapshot" if table == "submission_annotations" and key == "participant_roles" else key
                value = row[column]
                payload[key] = json.loads(value) if isinstance(value, str) else value
            payload["mouse_ids"] = payload["mouse_ids"] or []
            payload["participant_roles"] = payload["participant_roles"] or {}
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            extra = ", material_state=:state" if table == "annotations" else ""
            conn.execute(sa.text(f"UPDATE {table} SET material_digest=:digest{extra} WHERE id=:id"),
                         {"digest": digest, "state": encoded, "id": row["id"]})
    for submission in conn.execute(sa.text(
            "SELECT s.id, r.reviewer_id, r.created_at FROM submissions s "
            "JOIN reviews r ON r.submission_id=s.id WHERE r.result='approved'"
    )).mappings().all():
        ids = conn.execute(sa.text(
            "SELECT id FROM submission_annotations WHERE submission_id=:id ORDER BY id"
        ), {"id": submission["id"]}).scalars().all()
        for sequence, snapshot_id in enumerate(ids, 1):
            conn.execute(sa.text(
                "INSERT INTO behavior_review_decisions "
                "(submission_annotation_id,status,sequence,reviewer_id,decided_at,origin) "
                "VALUES (:snapshot,'approved',:sequence,:reviewer,:at,'legacy')"
            ), {"snapshot": snapshot_id, "sequence": sequence,
                "reviewer": submission["reviewer_id"], "at": submission["created_at"]})
        conn.execute(sa.text("UPDATE submissions SET decision_revision=:revision WHERE id=:id"),
                     {"revision": len(ids), "id": submission["id"]})
    install_sqlite_authority_triggers(conn)


def downgrade() -> None:
    conn = op.get_bind()
    drop_sqlite_authority_triggers(conn)
    op.drop_table("behavior_review_reopens")
    op.drop_table("behavior_review_decisions")
    with op.batch_alter_table("submission_annotations") as batch:
        batch.drop_index("ix_submission_annotations_source_annotation_key")
        batch.drop_column("material_digest")
        batch.drop_column("source_material_revision")
        batch.drop_column("source_annotation_key")
    with op.batch_alter_table("submissions") as batch:
        batch.drop_column("decision_revision")
    with op.batch_alter_table("annotations") as batch:
        batch.drop_column("material_state")
        batch.drop_column("material_digest")
        batch.drop_column("material_revision")
    install_sqlite_authority_triggers(conn)
