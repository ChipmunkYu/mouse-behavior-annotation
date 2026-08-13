"""Add immutable DetectionSnapshot integrity digests.

Revision ID: 0010
Revises: 0009
"""
from typing import Sequence, Union
import json
import sqlalchemy as sa
from alembic import op
from app.integrity_canonical import canonical_digest, canonical_rows_digest, validate_pose_metadata

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    # Validate all 0009 authority before adding/blessing digest columns.
    snapshots = connection.execute(sa.text("SELECT * FROM detection_snapshots ORDER BY id")).mappings().all()
    for snapshot in snapshots:
        raw_count = connection.execute(sa.text(
            "SELECT count(*) FROM raw_detections WHERE detection_import_id=:i"),
            {"i": snapshot["detection_import_id"]}).scalar_one()
        state_count = connection.execute(sa.text(
            "SELECT count(*) FROM detection_snapshot_states WHERE snapshot_id=:s"),
            {"s": snapshot["id"]}).scalar_one()
        cross = connection.execute(sa.text(
            "SELECT count(*) FROM detection_snapshot_states s LEFT JOIN raw_detections r ON r.id=s.raw_detection_id "
            "WHERE s.snapshot_id=:s AND (s.detection_import_id<>:i OR r.id IS NULL OR r.detection_import_id<>:i)"),
            {"s": snapshot["id"], "i": snapshot["detection_import_id"]}).scalar_one()
        try:
            names, edges = json.loads(snapshot["keypoint_names"]), json.loads(snapshot["skeleton_edges"])
            validate_pose_metadata(names, edges)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"snapshot {snapshot['id']} failed 0010 integrity preflight: invalid pose metadata") from exc
        valid_header = (snapshot["raw_detection_count"] == raw_count
                        and snapshot["override_count"] == state_count
                        and 0 <= snapshot["override_count"] <= raw_count
                        and snapshot["schema_version"] >= 1 and snapshot["fps"] > 0
                        and snapshot["width"] > 0 and snapshot["height"] > 0
                        and snapshot["frame_count"] >= 0)
        if cross or not valid_header:
            raise RuntimeError(f"snapshot {snapshot['id']} failed 0010 integrity preflight")
    with op.batch_alter_table("detection_snapshots") as batch_op:
        for name in ("raw_digest", "state_digest", "metadata_digest"):
            batch_op.add_column(sa.Column(name, sa.String(length=64), nullable=True))
    snapshots = connection.execute(sa.text("SELECT * FROM detection_snapshots ORDER BY id")).mappings()
    for snapshot in snapshots:
        raw_rows = connection.execute(sa.text(
            "SELECT id, frame_index, frame_detection_index, raw_track_id, box, keypoints, "
            "detection_confidence, class_id FROM raw_detections WHERE detection_import_id=:id ORDER BY id"
        ), {"id": snapshot["detection_import_id"]}).mappings()
        raw = ([r["id"], r["frame_index"], r["frame_detection_index"], r["raw_track_id"],
                json.loads(r["box"]) if r["box"] else None,
                json.loads(r["keypoints"]) if r["keypoints"] else None,
                r["detection_confidence"], r["class_id"]] for r in raw_rows)
        states = connection.execute(sa.text(
            "SELECT raw_detection_id, display_track_id, suppressed FROM detection_snapshot_states "
            "WHERE snapshot_id=:id ORDER BY raw_detection_id"), {"id": snapshot["id"]}).fetchall()
        metadata = {key: snapshot[key] for key in
                    ("schema_version", "fps", "width", "height", "frame_count")}
        metadata["keypoint_names"] = json.loads(snapshot["keypoint_names"])
        metadata["skeleton_edges"] = json.loads(snapshot["skeleton_edges"])
        connection.execute(sa.text(
            "UPDATE detection_snapshots SET raw_digest=:raw, state_digest=:state, metadata_digest=:metadata WHERE id=:id"
        ), {"id": snapshot["id"], "raw": canonical_rows_digest(raw),
            "state": canonical_rows_digest(([r[0], r[1], bool(r[2])] for r in states)),
            "metadata": canonical_digest(metadata)})
    with op.batch_alter_table("detection_snapshots") as batch_op:
        for name in ("raw_digest", "state_digest", "metadata_digest"):
            batch_op.alter_column(name, existing_type=sa.String(length=64), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("detection_snapshots") as batch_op:
        batch_op.drop_column("metadata_digest")
        batch_op.drop_column("state_digest")
        batch_op.drop_column("raw_digest")
