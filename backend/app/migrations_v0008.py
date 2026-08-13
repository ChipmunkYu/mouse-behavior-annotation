"""Frozen strict validation/backfill semantics used by Alembic revision 0008.

Do not change this module for runtime behavior after 0008 has shipped.  Later
maintenance code may call it, but migration history must remain stable.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .track_ids import TRACK_ID_UPPER_BOUND


ACTIVE_SUPPRESSION_EXISTS_SQL = """
EXISTS (
    SELECT 1
    FROM suppression_detections AS sd
    JOIN detection_suppressions AS ds ON ds.id = sd.suppression_id
    WHERE sd.raw_detection_id = rd.id
      AND ds.detection_import_id = di.id
      AND ds.reverted_suppression_id IS NULL
      AND ds.base_identity_revision <= v.identity_revision
      AND NOT EXISTS (
          SELECT 1
          FROM detection_suppressions AS rev
          WHERE rev.detection_import_id = di.id
            AND rev.reverted_suppression_id = ds.id
            AND rev.result_identity_revision <= v.identity_revision
      )
)
""".strip()


def _raise_grouped(prefix: str, rows: list[Any]) -> None:
    details = "; ".join(
        f"import={row.detection_import_id},video={row.video_id},"
        f"revision={row.identity_revision},category={row.category},count={row.count}"
        for row in rows
    )
    raise RuntimeError(f"{prefix}: {details}")


def validate_legacy_track_id_domain(connection: Connection) -> None:
    """Reject legacy raw/display IDs outside the canonical domain before DDL."""
    rows = connection.execute(
        text(
            """
            SELECT detection_import_id, 'raw_track_id' AS category, count(*) AS count
            FROM raw_detections
            WHERE raw_track_id < 0 OR raw_track_id >= :upper
            GROUP BY detection_import_id
            UNION ALL
            SELECT detection_import_id, 'display_track_id' AS category, count(*) AS count
            FROM corrected_tracks
            WHERE display_track_id < 0 OR display_track_id >= :upper
            GROUP BY detection_import_id
            """
        ),
        {"upper": TRACK_ID_UPPER_BOUND},
    ).all()
    if rows:
        details = "; ".join(
            f"import={row.detection_import_id},category={row.category},count={row.count}"
            for row in rows
        )
        raise ValueError(
            f"legacy track ID domain validation failed (0 <= id < {TRACK_ID_UPPER_BOUND}): "
            f"{details}"
        )


def validate_legacy_current_state(connection: Connection) -> None:
    """Require exactly one valid active same-import current CDA per active raw row."""
    rows = connection.execute(
        text(
            """
            WITH current_cda_counts AS (
                SELECT di.id AS detection_import_id, di.video_id, v.identity_revision,
                       rd.id AS raw_detection_id, count(cda.id) AS cda_count
                FROM detection_imports AS di
                JOIN videos AS v ON v.id = di.video_id
                JOIN raw_detections AS rd ON rd.detection_import_id = di.id
                LEFT JOIN corrected_detection_assignments AS cda
                  ON cda.raw_detection_id = rd.id
                 AND cda.identity_revision = v.identity_revision
                WHERE di.active = 1
                GROUP BY di.id, di.video_id, v.identity_revision, rd.id
            )
            SELECT detection_import_id, video_id, identity_revision,
                   CASE WHEN cda_count = 0 THEN 'missing_cda' ELSE 'ambiguous_cda' END AS category,
                   count(*) AS count
            FROM current_cda_counts
            WHERE cda_count <> 1
            GROUP BY detection_import_id, video_id, identity_revision,
                     CASE WHEN cda_count = 0 THEN 'missing_cda' ELSE 'ambiguous_cda' END
            UNION ALL
            SELECT di.id, di.video_id, v.identity_revision, 'missing_track', count(*)
            FROM detection_imports AS di
            JOIN videos AS v ON v.id = di.video_id
            JOIN raw_detections AS rd ON rd.detection_import_id = di.id
            JOIN corrected_detection_assignments AS cda
              ON cda.raw_detection_id = rd.id
             AND cda.identity_revision = v.identity_revision
            LEFT JOIN corrected_tracks AS ct ON ct.id = cda.corrected_track_id
            WHERE di.active = 1 AND ct.id IS NULL
            GROUP BY di.id, di.video_id, v.identity_revision
            UNION ALL
            SELECT di.id, di.video_id, v.identity_revision, 'cross_import_track', count(*)
            FROM detection_imports AS di
            JOIN videos AS v ON v.id = di.video_id
            JOIN raw_detections AS rd ON rd.detection_import_id = di.id
            JOIN corrected_detection_assignments AS cda
              ON cda.raw_detection_id = rd.id
             AND cda.identity_revision = v.identity_revision
            JOIN corrected_tracks AS ct ON ct.id = cda.corrected_track_id
            WHERE di.active = 1 AND ct.detection_import_id <> di.id
            GROUP BY di.id, di.video_id, v.identity_revision
            UNION ALL
            SELECT di.id, di.video_id, v.identity_revision, 'inactive_track', count(*)
            FROM detection_imports AS di
            JOIN videos AS v ON v.id = di.video_id
            JOIN raw_detections AS rd ON rd.detection_import_id = di.id
            JOIN corrected_detection_assignments AS cda
              ON cda.raw_detection_id = rd.id
             AND cda.identity_revision = v.identity_revision
            JOIN corrected_tracks AS ct ON ct.id = cda.corrected_track_id
            WHERE di.active = 1 AND ct.active = 0
            GROUP BY di.id, di.video_id, v.identity_revision
            """
        )
    ).all()
    if rows:
        _raise_grouped("legacy current detection validation failed", rows)


def rebuild_sparse_detection_state(connection: Connection) -> dict[str, int]:
    """Rebuild active sparse state and all historical cursors after strict validation."""
    validate_legacy_track_id_domain(connection)
    validate_legacy_current_state(connection)

    connection.execute(
        text(
            """
            UPDATE detection_imports
            SET next_display_track_id = COALESCE(
                (SELECT MAX(track_id) FROM (
                    SELECT rd.raw_track_id AS track_id FROM raw_detections AS rd
                    WHERE rd.detection_import_id = detection_imports.id
                    UNION ALL
                    SELECT ct.display_track_id AS track_id FROM corrected_tracks AS ct
                    WHERE ct.detection_import_id = detection_imports.id
                ) AS historical_track_ids), -1
            ) + 1
            """
        )
    )
    # Only active imports may own draft state.  Clearing all rows also removes
    # stale overrides left behind when a formerly-active import was replaced.
    connection.execute(text("DELETE FROM detection_state_overrides"))
    connection.execute(text("UPDATE detection_imports SET edit_version = 0"))
    connection.execute(
        text(
            f"""
            INSERT INTO detection_state_overrides (
                raw_detection_id, detection_import_id, display_track_id,
                suppressed, updated_edit_version
            )
            SELECT rd.id, di.id, ct.display_track_id,
                   CASE WHEN {ACTIVE_SUPPRESSION_EXISTS_SQL} THEN 1 ELSE 0 END,
                   1
            FROM detection_imports AS di
            JOIN videos AS v ON v.id = di.video_id
            JOIN raw_detections AS rd ON rd.detection_import_id = di.id
            JOIN corrected_detection_assignments AS cda
              ON cda.raw_detection_id = rd.id
             AND cda.identity_revision = v.identity_revision
            JOIN corrected_tracks AS ct
              ON ct.id = cda.corrected_track_id
             AND ct.detection_import_id = di.id
             AND ct.active = 1
            WHERE di.active = 1
              AND (ct.display_track_id <> rd.raw_track_id
                   OR {ACTIVE_SUPPRESSION_EXISTS_SQL})
            """
        )
    )
    connection.execute(
        text(
            """
            UPDATE detection_imports SET edit_version = 1
            WHERE active = 1 AND EXISTS (
                SELECT 1 FROM detection_state_overrides AS dso
                WHERE dso.detection_import_id = detection_imports.id
            )
            """
        )
    )

    diff_count = connection.execute(
        text(
            f"""
            SELECT count(*) FROM (
                SELECT rd.id
                FROM detection_imports AS di
                JOIN videos AS v ON v.id = di.video_id
                JOIN raw_detections AS rd ON rd.detection_import_id = di.id
                JOIN corrected_detection_assignments AS cda
                  ON cda.raw_detection_id = rd.id
                 AND cda.identity_revision = v.identity_revision
                JOIN corrected_tracks AS ct
                  ON ct.id = cda.corrected_track_id
                 AND ct.detection_import_id = di.id AND ct.active = 1
                LEFT JOIN detection_state_overrides AS dso ON dso.raw_detection_id = rd.id
                WHERE di.active = 1 AND (
                    COALESCE(dso.display_track_id, rd.raw_track_id) <> ct.display_track_id
                    OR COALESCE(dso.suppressed, 0) <>
                       CASE WHEN {ACTIVE_SUPPRESSION_EXISTS_SQL} THEN 1 ELSE 0 END
                )
            ) AS differences
            """
        )
    ).scalar_one()
    override_count = connection.execute(
        text("SELECT count(*) FROM detection_state_overrides")
    ).scalar_one()
    return {
        "override_count": int(override_count),
        "shadow_difference_count": int(diff_count),
    }
