"""SQL-level effective detection reads for draft and future snapshot authority."""
from __future__ import annotations

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from .models import (
    DetectionSnapshot,
    DetectionSnapshotState,
    DetectionStateOverride,
    RawDetection,
)


def effective_detection_query(
    db: Session,
    detection_import_id: int,
    *,
    start_frame: int | None = None,
    end_frame: int | None = None,
    display_track_id: int | None = None,
    include_suppressed: bool = False,
    snapshot_id: int | None = None,
):
    """Return RawDetection plus SQL-computed display ID/suppressed state.

    `snapshot_id=None` reads the current draft sparse override.  Snapshot mode is
    read-only groundwork for Phase 3 and creates no Submission.
    """
    if snapshot_id is None:
        state = DetectionStateOverride
        join_condition = state.raw_detection_id == RawDetection.id
    else:
        state = DetectionSnapshotState
        join_condition = and_(
            state.raw_detection_id == RawDetection.id,
            state.snapshot_id == snapshot_id,
        )

    display_expr = func.coalesce(state.display_track_id, RawDetection.raw_track_id)
    suppressed_expr = func.coalesce(state.suppressed, False)
    query = (
        db.query(
            RawDetection,
            display_expr.label("display_track_id"),
            suppressed_expr.label("suppressed"),
            state.display_track_id.label("override_display_track_id"),
            state.suppressed.label("override_suppressed"),
        )
        .outerjoin(state, join_condition)
        .filter(RawDetection.detection_import_id == detection_import_id)
    )
    if snapshot_id is not None:
        query = query.filter(
            db.query(DetectionSnapshot.id)
            .filter(
                DetectionSnapshot.id == snapshot_id,
                DetectionSnapshot.detection_import_id == detection_import_id,
            )
            .exists()
        )
    if start_frame is not None:
        query = query.filter(RawDetection.frame_index >= start_frame)
    if end_frame is not None:
        query = query.filter(RawDetection.frame_index <= end_frame)
    if display_track_id is not None:
        query = query.filter(display_expr == display_track_id)
    if not include_suppressed:
        query = query.filter(suppressed_expr == False)
    return query


def effective_track_summary_query(
    db: Session,
    detection_import_id: int,
    *,
    include_suppressed: bool = False,
):
    state = DetectionStateOverride
    display_expr = func.coalesce(state.display_track_id, RawDetection.raw_track_id)
    suppressed_expr = func.coalesce(state.suppressed, False)
    query = (
        db.query(
            display_expr.label("display_track_id"),
            func.min(RawDetection.frame_index).label("first_frame"),
            func.max(RawDetection.frame_index).label("last_frame"),
            func.count(RawDetection.id).label("detection_count"),
        )
        .outerjoin(state, state.raw_detection_id == RawDetection.id)
        .filter(RawDetection.detection_import_id == detection_import_id)
    )
    if not include_suppressed:
        query = query.filter(suppressed_expr == False)
    return query.group_by(display_expr)


def has_effective_detection(
    db: Session,
    detection_import_id: int,
    display_track_id: int,
    start_frame: int,
    end_frame: int,
) -> bool:
    return (
        effective_detection_query(
            db,
            detection_import_id,
            start_frame=start_frame,
            end_frame=end_frame,
            display_track_id=display_track_id,
        ).first()
        is not None
    )
