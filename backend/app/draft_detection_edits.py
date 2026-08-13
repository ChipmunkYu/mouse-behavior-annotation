"""Sparse draft Split/Merge/Suppress/LIFO Undo transaction service."""
from __future__ import annotations

from datetime import datetime
from functools import wraps

from fastapi import HTTPException
from sqlalchemy import func, text, update
from sqlalchemy.orm import Session

from .effective_detections import (
    effective_detection_query,
    effective_track_summary_query,
    has_effective_detection,
)
from .models import (
    Annotation,
    DetectionImport,
    DetectionStateOverride,
    DraftDetectionChange,
    DraftIdentityEdit,
    RawDetection,
    Video,
)
from .track_ids import TRACK_ID_UPPER_BOUND, is_valid_track_id


def _after_set_based_delta() -> None:
    """Test injection point after mutation and before projection/commit."""


def _rollback_on_error(function):
    @wraps(function)
    def wrapped(db: Session, *args, **kwargs):
        try:
            return function(db, *args, **kwargs)
        except BaseException:
            db.rollback()
            raise
    return wrapped


def _claim_version(
    db: Session,
    detection_import: DetectionImport,
    expected_version: int,
    *,
    allocate_track_id: bool = False,
) -> tuple[int, int | None]:
    """First business write: CAS edit version and optionally consume the cursor."""
    new_version = expected_version + 1
    values = {"edit_version": new_version}
    allocated: int | None = None
    conditions = [
        DetectionImport.id == detection_import.id,
        DetectionImport.edit_version == expected_version,
    ]
    if allocate_track_id:
        if detection_import.next_display_track_id >= TRACK_ID_UPPER_BOUND:
            raise HTTPException(status_code=409, detail="Display track ID space is exhausted")
        allocated = detection_import.next_display_track_id
        values["next_display_track_id"] = detection_import.next_display_track_id + 1
        conditions.append(
            DetectionImport.next_display_track_id == detection_import.next_display_track_id
        )
    result = db.execute(update(DetectionImport).where(*conditions).values(**values))
    if result.rowcount != 1:
        raise HTTPException(status_code=409, detail="Identity revision changed concurrently")
    return new_version, allocated


def _set_based_apply(
    db: Session,
    edit: DraftIdentityEdit,
    *,
    predicate_sql: str,
    after_display_sql: str,
    after_suppressed: bool,
    params: dict,
) -> int:
    """Insert deltas and replace affected sparse rows with a constant SQL statement count."""
    values = {
        **params,
        "edit_id": edit.id,
        "import_id": edit.detection_import_id,
        "after_suppressed": 1 if after_suppressed else 0,
        "version": edit.applied_edit_version,
    }
    db.execute(text(f"""
        INSERT INTO draft_detection_changes (
            edit_id, raw_detection_id, detection_import_id,
            before_override_exists, before_display_track_id, before_suppressed,
            after_override_exists, after_display_track_id, after_suppressed
        )
        SELECT :edit_id, rd.id, rd.detection_import_id,
               CASE WHEN dso.raw_detection_id IS NULL THEN 0 ELSE 1 END,
               dso.display_track_id, dso.suppressed,
               CASE WHEN ({after_display_sql}) <> rd.raw_track_id OR :after_suppressed=1
                    THEN 1 ELSE 0 END,
               CASE WHEN ({after_display_sql}) <> rd.raw_track_id OR :after_suppressed=1
                    THEN ({after_display_sql}) ELSE NULL END,
               CASE WHEN ({after_display_sql}) <> rd.raw_track_id OR :after_suppressed=1
                    THEN :after_suppressed ELSE NULL END
        FROM raw_detections AS rd
        LEFT JOIN detection_state_overrides AS dso ON dso.raw_detection_id=rd.id
        WHERE rd.detection_import_id=:import_id AND ({predicate_sql})
    """), values)
    count = db.execute(text(
        "SELECT count(*) FROM draft_detection_changes WHERE edit_id=:edit_id"
    ), {"edit_id": edit.id}).scalar_one()
    db.execute(text("""
        DELETE FROM detection_state_overrides
        WHERE raw_detection_id IN (
            SELECT raw_detection_id FROM draft_detection_changes WHERE edit_id=:edit_id
        )
    """), {"edit_id": edit.id})
    db.execute(text("""
        INSERT INTO detection_state_overrides (
            raw_detection_id,detection_import_id,display_track_id,suppressed,updated_edit_version
        )
        SELECT raw_detection_id,detection_import_id,after_display_track_id,
               after_suppressed,:version
        FROM draft_detection_changes
        WHERE edit_id=:edit_id AND after_override_exists=1
    """), {"edit_id": edit.id, "version": edit.applied_edit_version})
    return int(count)


def revalidate_annotations(
    db: Session,
    detection_import: DetectionImport,
    video: Video,
    edit_version: int,
    *,
    force_needs_mouse_ids: set[int] | None = None,
) -> list[int]:
    forced = force_needs_mouse_ids or set()
    needs: list[int] = []
    for annotation in db.query(Annotation).filter(Annotation.video_id == video.id).all():
        valid = annotation.id not in forced and bool(annotation.mouse_ids)
        category = annotation.category
        if valid and category is not None:
            count = len(annotation.mouse_ids)
            valid = count >= category.mouse_count_min and (
                category.mouse_count_max is None or count <= category.mouse_count_max
            )
        if valid:
            valid = all(
                has_effective_detection(
                    db,
                    detection_import.id,
                    track_id,
                    annotation.start_frame,
                    annotation.end_frame,
                )
                for track_id in annotation.mouse_ids
            )
        annotation.mouse_id_status = "valid" if valid else "needs_mouse_ids"
        annotation.detection_import_revision = video.detection_import_revision
        annotation.identity_revision = edit_version
        if not valid:
            needs.append(annotation.id)
    return needs


def invalidate_compatibility_review(
    db: Session, video: Video, affected_annotation_ids: set[int]
) -> None:
    for annotation_id in affected_annotation_ids:
        annotation = db.get(Annotation, annotation_id)
        if annotation is not None and annotation.review_status in ("approved", "rejected"):
            annotation.review_status = "pending"
            annotation.reviewer_id = None
    if video.workflow_status in ("approved", "rejected"):
        video.workflow_status = "draft"
        video.submitted_at = None
        video.approved_at = None
        video.approved_by = None


def split_preview(
    db: Session, detection_import: DetectionImport, track_id: int, frame: int | None
) -> dict:
    if not is_valid_track_id(track_id) or frame is None:
        raise HTTPException(status_code=400, detail="Split requires one valid track_id and frame")
    before = effective_detection_query(
        db,
        detection_import.id,
        end_frame=frame - 1,
        display_track_id=track_id,
    ).count()
    after = effective_detection_query(
        db,
        detection_import.id,
        start_frame=frame,
        display_track_id=track_id,
    ).count()
    if before == 0 or after == 0:
        raise HTTPException(
            status_code=400,
            detail="Split frame must leave unsuppressed detections on both sides",
        )
    return {
        "operation": "split",
        "old_display_track_id": track_id,
        "new_display_track_id": detection_import.next_display_track_id,
        "split_frame": frame,
        "detections_before": before,
        "detections_after": after,
    }


def _merge_plan(db: Session, detection_import: DetectionImport, track_ids: list[int]) -> dict:
    requested = sorted(set(track_ids))
    if len(requested) < 2:
        raise HTTPException(status_code=400, detail="Merge requires at least two distinct track_ids")
    if any(not is_valid_track_id(track_id) for track_id in requested):
        raise HTTPException(status_code=400, detail="Merge requires valid track_ids")
    summaries = {
        row.display_track_id: row
        for row in effective_track_summary_query(db, detection_import.id).all()
        if row.display_track_id in requested
    }
    missing = set(requested) - set(summaries)
    if missing:
        raise HTTPException(status_code=400, detail=f"Track IDs not found: {sorted(missing)}")
    state = DetectionStateOverride
    display_expr = func.coalesce(state.display_track_id, RawDetection.raw_track_id)
    suppressed_expr = func.coalesce(state.suppressed, False)
    conflicts = (
        db.query(RawDetection.frame_index)
        .outerjoin(state, state.raw_detection_id == RawDetection.id)
        .filter(
            RawDetection.detection_import_id == detection_import.id,
            display_expr.in_(requested),
            suppressed_expr == False,
        )
        .group_by(RawDetection.frame_index)
        .having(func.count(func.distinct(display_expr)) > 1)
        .order_by(RawDetection.frame_index)
        .all()
    )
    retained = min(
        requested,
        key=lambda track_id: (summaries[track_id].first_frame, track_id),
    )
    return {
        "operation": "merge",
        "retained_display_track_id": retained,
        "merged_display_track_ids": [track_id for track_id in requested if track_id != retained],
        "affected_detection_count": sum(
            summaries[track_id].detection_count for track_id in requested if track_id != retained
        ),
        "conflict_frames": [row.frame_index for row in conflicts],
    }


def merge_preview(db: Session, detection_import: DetectionImport, track_ids: list[int]) -> dict:
    return _merge_plan(db, detection_import, track_ids)


@_rollback_on_error
def commit_split(
    db: Session,
    detection_import: DetectionImport,
    video: Video,
    *,
    track_id: int,
    frame: int,
    expected_version: int,
    operator_id: int,
) -> dict:
    new_version, new_track_id = _claim_version(
        db, detection_import, expected_version, allocate_track_id=True
    )
    assert new_track_id is not None
    split_preview(db, detection_import, track_id, frame)
    affected_annotations = {
        annotation.id
        for annotation in db.query(Annotation).filter(Annotation.video_id == video.id).all()
        if track_id in (annotation.mouse_ids or []) and annotation.end_frame >= frame
    }
    edit = DraftIdentityEdit(
        detection_import_id=detection_import.id,
        applied_edit_version=new_version,
        operation="split",
        params={
            "old_display_track_id": track_id,
            "new_display_track_id": new_track_id,
            "split_frame": frame,
        },
        operator_id=operator_id,
        created_at=datetime.utcnow(),
    )
    db.add(edit)
    db.flush()
    affected_count = _set_based_apply(
        db, edit,
        predicate_sql=(
            "COALESCE(dso.display_track_id,rd.raw_track_id)=:track_id "
            "AND COALESCE(dso.suppressed,0)=0 AND rd.frame_index>=:frame"
        ),
        after_display_sql=":new_track_id",
        after_suppressed=False,
        params={"track_id": track_id, "frame": frame, "new_track_id": new_track_id},
    )
    _after_set_based_delta()
    needs = revalidate_annotations(
        db,
        detection_import,
        video,
        new_version,
        force_needs_mouse_ids=affected_annotations,
    )
    video.identity_revision = new_version
    invalidate_compatibility_review(db, video, affected_annotations | set(needs))
    db.commit()
    return {
        "edit_id": edit.id,
        "identity_revision": new_version,
        "old_display_track_id": track_id,
        "new_display_track_id": new_track_id,
        "affected_detection_count": affected_count,
        "affected_annotation_count": len(affected_annotations),
        "needs_mouse_ids_annotation_ids": needs,
    }


@_rollback_on_error
def commit_merge(
    db: Session,
    detection_import: DetectionImport,
    video: Video,
    *,
    track_ids: list[int],
    expected_version: int,
    operator_id: int,
) -> dict:
    new_version, _ = _claim_version(db, detection_import, expected_version)
    plan = _merge_plan(db, detection_import, track_ids)
    if plan["conflict_frames"]:
        raise HTTPException(
            status_code=400,
            detail={"message": "Merge has conflict frames", "conflict_frames": plan["conflict_frames"]},
        )
    retained = plan["retained_display_track_id"]
    merged = plan["merged_display_track_ids"]
    annotation_snapshots: dict[str, list[int]] = {}
    affected_annotations: set[int] = set()
    for annotation in db.query(Annotation).filter(Annotation.video_id == video.id).all():
        if any(track_id in (annotation.mouse_ids or []) for track_id in merged):
            annotation_snapshots[str(annotation.id)] = list(annotation.mouse_ids or [])
            annotation.mouse_ids = sorted(
                {retained if track_id in merged else track_id for track_id in annotation.mouse_ids}
            )
            affected_annotations.add(annotation.id)
    edit = DraftIdentityEdit(
        detection_import_id=detection_import.id,
        applied_edit_version=new_version,
        operation="merge",
        params={
            "retained_display_track_id": retained,
            "merged_display_track_ids": merged,
            "annotation_mouse_ids_before": annotation_snapshots,
        },
        operator_id=operator_id,
        created_at=datetime.utcnow(),
    )
    db.add(edit)
    db.flush()
    merge_params = {f"merged_{index}": value for index, value in enumerate(merged)}
    placeholders = ",".join(f":merged_{index}" for index in range(len(merged)))
    affected_count = _set_based_apply(
        db, edit,
        predicate_sql=(
            f"COALESCE(dso.display_track_id,rd.raw_track_id) IN ({placeholders}) "
            "AND COALESCE(dso.suppressed,0)=0"
        ),
        after_display_sql=":retained",
        after_suppressed=False,
        params={**merge_params, "retained": retained},
    )
    _after_set_based_delta()
    needs = revalidate_annotations(db, detection_import, video, new_version)
    video.identity_revision = new_version
    invalidate_compatibility_review(db, video, affected_annotations | set(needs))
    db.commit()
    return {
        "edit_id": edit.id,
        "identity_revision": new_version,
        "retained_display_track_id": retained,
        "merged_display_track_ids": merged,
        "affected_detection_count": affected_count,
        "affected_annotation_count": len(affected_annotations),
        "needs_mouse_ids_annotation_ids": needs,
    }


@_rollback_on_error
def commit_suppress(
    db: Session,
    detection_import: DetectionImport,
    video: Video,
    *,
    track_id: int,
    expected_version: int,
    operator_id: int,
) -> dict:
    new_version, _ = _claim_version(db, detection_import, expected_version)
    row_count = effective_detection_query(
        db, detection_import.id, display_track_id=track_id
    ).count()
    if not row_count:
        raise HTTPException(status_code=409, detail="Track is already fully suppressed")
    edit = DraftIdentityEdit(
        detection_import_id=detection_import.id,
        applied_edit_version=new_version,
        operation="suppress_track",
        params={"display_track_id": track_id},
        operator_id=operator_id,
        created_at=datetime.utcnow(),
    )
    db.add(edit)
    db.flush()
    affected_count = _set_based_apply(
        db, edit,
        predicate_sql=(
            "COALESCE(dso.display_track_id,rd.raw_track_id)=:track_id "
            "AND COALESCE(dso.suppressed,0)=0"
        ),
        after_display_sql="COALESCE(dso.display_track_id,rd.raw_track_id)",
        after_suppressed=True,
        params={"track_id": track_id},
    )
    _after_set_based_delta()
    needs = revalidate_annotations(db, detection_import, video, new_version)
    video.identity_revision = new_version
    invalidate_compatibility_review(db, video, set(needs))
    db.commit()
    return {
        "suppression_id": edit.id,
        "identity_revision": new_version,
        "frozen_detection_count": affected_count,
        "affected_track_ids": [track_id],
        "needs_mouse_ids_annotation_ids": needs,
    }


@_rollback_on_error
def undo_latest(
    db: Session,
    detection_import: DetectionImport,
    video: Video,
    *,
    requested_edit_id: int,
    expected_operation: str,
    expected_version: int,
) -> dict:
    latest = (
        db.query(DraftIdentityEdit)
        .filter(DraftIdentityEdit.detection_import_id == detection_import.id)
        .order_by(DraftIdentityEdit.applied_edit_version.desc())
        .first()
    )
    if latest is None or latest.id != requested_edit_id:
        raise HTTPException(status_code=409, detail="Only the latest draft edit can be undone")
    if latest.operation != expected_operation:
        raise HTTPException(status_code=409, detail="Latest draft edit type does not match this endpoint")
    new_version, _ = _claim_version(db, detection_import, expected_version)
    params = latest.params or {}
    for annotation_id, mouse_ids in params.get("annotation_mouse_ids_before", {}).items():
        annotation = db.get(Annotation, int(annotation_id))
        if annotation is not None:
            annotation.mouse_ids = mouse_ids
    change_count = db.execute(text(
        "SELECT count(*) FROM draft_detection_changes WHERE edit_id=:edit_id"
    ), {"edit_id": latest.id}).scalar_one()
    db.execute(text("""
        DELETE FROM detection_state_overrides WHERE raw_detection_id IN (
            SELECT raw_detection_id FROM draft_detection_changes WHERE edit_id=:edit_id
        )
    """), {"edit_id": latest.id})
    db.execute(text("""
        INSERT INTO detection_state_overrides (
            raw_detection_id,detection_import_id,display_track_id,suppressed,updated_edit_version
        )
        SELECT raw_detection_id,detection_import_id,before_display_track_id,
               before_suppressed,:version
        FROM draft_detection_changes
        WHERE edit_id=:edit_id AND before_override_exists=1
    """), {"edit_id": latest.id, "version": new_version})
    db.delete(latest)
    db.flush()
    needs = revalidate_annotations(db, detection_import, video, new_version)
    video.identity_revision = new_version
    invalidate_compatibility_review(db, video, set(needs))
    db.commit()
    return {
        "identity_revision": new_version,
        "reverted_edit_id": requested_edit_id,
        "freed_detection_count": int(change_count),
        "needs_mouse_ids_annotation_ids": needs,
        "message": f"Reverted {expected_operation} (edit {requested_edit_id})",
    }
