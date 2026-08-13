"""Maintenance-window reconciliation immediately before sparse writer cutover."""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import create_engine, text

from .migrations_v0008 import rebuild_sparse_detection_state


def reconcile_detection_state(
    database_url: str,
    *,
    legacy_writer_stopped: bool,
    _post_mutation_hook: Callable[[], None] | None = None,
) -> dict[str, int]:
    """Run on a fresh connection under explicit BEGIN IMMEDIATE.

    The caller must stop all legacy identity/suppression writers first.  This is
    an internal maintenance operation, not an HTTP or normal request service.
    """
    if not legacy_writer_stopped:
        raise RuntimeError("legacy writer must be stopped before reconciliation")
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                stack_count = connection.execute(
                    text(
                        "SELECT (SELECT count(*) FROM draft_identity_edits) + "
                        "(SELECT count(*) FROM draft_detection_changes)"
                    )
                ).scalar_one()
                if stack_count:
                    raise RuntimeError(
                        f"reconciliation requires an empty draft stack; rows={stack_count}"
                    )
                # A prior successful preparation normalized Video.identity_revision
                # to edit_version.  For repeatability during the same stopped-writer
                # window, recover the frozen legacy revision from its latest CDA.
                connection.execute(
                    text(
                        """
                        UPDATE videos
                        SET identity_revision = COALESCE((
                            SELECT max(cda.identity_revision)
                            FROM detection_imports AS di
                            JOIN raw_detections AS rd ON rd.detection_import_id = di.id
                            JOIN corrected_detection_assignments AS cda
                              ON cda.raw_detection_id = rd.id
                            WHERE di.video_id = videos.id AND di.active = 1
                        ), identity_revision)
                        WHERE EXISTS (
                            SELECT 1 FROM detection_imports AS di
                            WHERE di.video_id = videos.id AND di.active = 1
                        )
                        """
                    )
                )
                summary = rebuild_sparse_detection_state(connection)
                if _post_mutation_hook is not None:
                    _post_mutation_hook()
                if summary["shadow_difference_count"] != 0:
                    raise RuntimeError(
                        "reconciliation shadow mismatch: "
                        f"count={summary['shadow_difference_count']}"
                    )
                # Shadow equality proves semantics did not change.  Normalize
                # compatibility projections only after that check passes.
                connection.execute(
                    text(
                        """
                        UPDATE videos
                        SET identity_revision = (
                            SELECT di.edit_version FROM detection_imports AS di
                            WHERE di.video_id = videos.id AND di.active = 1
                        )
                        WHERE EXISTS (
                            SELECT 1 FROM detection_imports AS di
                            WHERE di.video_id = videos.id AND di.active = 1
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        UPDATE annotations
                        SET identity_revision = (
                            SELECT di.edit_version FROM detection_imports AS di
                            WHERE di.video_id = annotations.video_id AND di.active = 1
                        )
                        WHERE EXISTS (
                            SELECT 1 FROM detection_imports AS di
                            WHERE di.video_id = annotations.video_id AND di.active = 1
                        )
                        """
                    )
                )
                connection.commit()
                return summary
            except BaseException:
                connection.rollback()
                raise
    finally:
        engine.dispose()
