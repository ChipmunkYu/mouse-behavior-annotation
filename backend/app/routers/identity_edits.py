"""身份编辑（Phase 2）：Split / Merge / 撤销 / 检查。

Split：在当前帧将一条修正轨迹拆分为前后两条。
Merge：将多条修正轨迹合并为一条（保留首次出现最早的 ID）。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import project_access
from ..models import (
    Annotation,
    CorrectedDetectionAssignment,
    CorrectedTrack,
    DetectionImport,
    IdentityEdit,
    RawDetection,
    SuppressionDetection,
    Video,
)
from ..schemas import (
    IdentityEditCheckRequest,
    IdentityEditCommitRequest,
    IdentityEditOut,
    IdentityEditRevertRequest,
)

router = APIRouter(tags=["identity-edits"])


def _require_video(db: Session, video_id: int, project_id: int) -> Video:
    video = db.get(Video, video_id)
    if video is None or video.project_id != project_id:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


def _get_active_import(db: Session, video_id: int) -> DetectionImport:
    imp = (
        db.query(DetectionImport)
        .filter(DetectionImport.video_id == video_id, DetectionImport.active == True)
        .first()
    )
    if imp is None:
        raise HTTPException(status_code=400, detail="No active detection import for this video")
    return imp


def _invalidate_review_state(db: Session, video: Video, affected_annotation_ids: list[int]) -> None:
    for ann_id in affected_annotation_ids:
        ann = db.get(Annotation, ann_id)
        if ann is None:
            continue
        if ann.review_status == "approved":
            ann.review_status = "pending"
            ann.reviewer_id = None
    if video.workflow_status in ("submitted", "approved"):
        video.workflow_status = "draft"
        video.submitted_at = None
        video.approved_at = None
        video.approved_by = None


def _check_revisions(video: Video, base_di_rev: int, base_id_rev: int) -> None:
    if video.detection_import_revision != base_di_rev:
        raise HTTPException(
            status_code=409,
            detail=f"Detection import revision mismatch: expected {video.detection_import_revision}, got {base_di_rev}",
        )
    if video.identity_revision != base_id_rev:
        raise HTTPException(
            status_code=409,
            detail=f"Identity revision mismatch: expected {video.identity_revision}, got {base_id_rev}",
        )


def _materialize_cda_snapshot(db: Session, imp_id: int, old_rev: int, new_rev: int) -> None:
    """将 old_rev 的所有 CDA 行复制到 new_rev，形成完整的物化快照。"""
    cda_rows = (
        db.query(CorrectedDetectionAssignment)
        .join(RawDetection, CorrectedDetectionAssignment.raw_detection_id == RawDetection.id)
        .filter(
            CorrectedDetectionAssignment.identity_revision == old_rev,
            RawDetection.detection_import_id == imp_id,
        )
        .all()
    )
    batch = []
    for cda in cda_rows:
        new_cda = CorrectedDetectionAssignment(
            raw_detection_id=cda.raw_detection_id,
            corrected_track_id=cda.corrected_track_id,
            identity_revision=new_rev,
        )
        batch.append(new_cda)
        if len(batch) >= 500:
            db.add_all(batch)
            db.flush()
            batch = []
    if batch:
        db.add_all(batch)
        db.flush()


def _revalidate_video_annotations(
    db: Session,
    imp: DetectionImport,
    video: Video,
    identity_revision: int,
    force_needs_mouse_ids: set[int] | None = None,
) -> list[int]:
    """按新身份快照重校验该视频的全部标注，并推进其修订链。"""
    forced = force_needs_mouse_ids or set()
    needs_ids: list[int] = []
    annotations = db.query(Annotation).filter(Annotation.video_id == video.id).all()

    for ann in annotations:
        valid = ann.id not in forced and bool(ann.mouse_ids)
        category = ann.category
        if valid and category is not None:
            mouse_count = len(ann.mouse_ids)
            valid = mouse_count >= category.mouse_count_min and (
                category.mouse_count_max is None or mouse_count <= category.mouse_count_max
            )

        if valid:
            for display_track_id in ann.mouse_ids:
                coverage = (
                    db.query(RawDetection.id)
                    .join(
                        CorrectedDetectionAssignment,
                        CorrectedDetectionAssignment.raw_detection_id == RawDetection.id,
                    )
                    .join(
                        CorrectedTrack,
                        CorrectedTrack.id == CorrectedDetectionAssignment.corrected_track_id,
                    )
                    .outerjoin(
                        SuppressionDetection,
                        SuppressionDetection.raw_detection_id == RawDetection.id,
                    )
                    .filter(
                        RawDetection.detection_import_id == imp.id,
                        RawDetection.frame_index >= ann.start_frame,
                        RawDetection.frame_index <= ann.end_frame,
                        CorrectedDetectionAssignment.identity_revision == identity_revision,
                        CorrectedTrack.active == True,
                        CorrectedTrack.display_track_id == display_track_id,
                        SuppressionDetection.raw_detection_id == None,
                    )
                    .first()
                )
                if coverage is None:
                    valid = False
                    break

        ann.mouse_id_status = "valid" if valid else "needs_mouse_ids"
        ann.detection_import_revision = video.detection_import_revision
        ann.identity_revision = identity_revision
        if not valid:
            needs_ids.append(ann.id)

    return needs_ids


# ---------------------------------------------------------------------------
# 纯校验函数（Fix 7：集中化，check 和 commit 均调用）
# ---------------------------------------------------------------------------

def _get_detections_without_suppressed(
    db: Session, imp: DetectionImport, identity_rev: int
):
    """返回当前修订下未被抑制的 CDA 的 raw_detection_id 集合。"""
    suppressed = set(
        r[0]
        for r in db.query(SuppressionDetection.raw_detection_id)
        .all()
    )
    return suppressed


def _validate_split(
    db: Session,
    imp: DetectionImport,
    video: Video,
    track_ids: list[int],
    frame: int | None,
) -> dict:
    """校验 Split 输入；失败抛 400。返回校验结果字典。"""
    if track_ids is None or len(track_ids) != 1:
        raise HTTPException(status_code=400, detail="Split requires exactly one track_id")
    old_tid = track_ids[0]
    if frame is None:
        raise HTTPException(status_code=400, detail="Split requires a frame")

    track = (
        db.query(CorrectedTrack)
        .filter(
            CorrectedTrack.detection_import_id == imp.id,
            CorrectedTrack.display_track_id == old_tid,
            CorrectedTrack.active == True,
        )
        .first()
    )
    if track is None:
        raise HTTPException(status_code=400, detail=f"Track {old_tid} is not an active corrected track")

    if track.first_frame is None or track.last_frame is None:
        raise HTTPException(status_code=400, detail="Track has no detection data")

    if frame <= track.first_frame or frame > track.last_frame:
        raise HTTPException(
            status_code=400,
            detail=f"Split frame {frame} must be within track range ({track.first_frame}, {track.last_frame}]",
        )

    id_rev = video.identity_revision
    suppressed_ids = _get_detections_without_suppressed(db, imp, id_rev)

    before_count = (
        db.query(CorrectedDetectionAssignment)
        .join(RawDetection, CorrectedDetectionAssignment.raw_detection_id == RawDetection.id)
        .filter(
            CorrectedDetectionAssignment.corrected_track_id == track.id,
            CorrectedDetectionAssignment.identity_revision == id_rev,
            RawDetection.frame_index < frame,
            ~CorrectedDetectionAssignment.raw_detection_id.in_(suppressed_ids) if suppressed_ids else True,
        )
        .count()
    )
    after_count = (
        db.query(CorrectedDetectionAssignment)
        .join(RawDetection, CorrectedDetectionAssignment.raw_detection_id == RawDetection.id)
        .filter(
            CorrectedDetectionAssignment.corrected_track_id == track.id,
            CorrectedDetectionAssignment.identity_revision == id_rev,
            RawDetection.frame_index >= frame,
            ~CorrectedDetectionAssignment.raw_detection_id.in_(suppressed_ids) if suppressed_ids else True,
        )
        .count()
    )

    if before_count == 0:
        raise HTTPException(status_code=400, detail="No detections before split frame")
    if after_count == 0:
        raise HTTPException(status_code=400, detail="No detections after split frame")

    max_display = (
        db.query(CorrectedTrack.display_track_id)
        .filter(CorrectedTrack.detection_import_id == imp.id, CorrectedTrack.active == True)
        .order_by(CorrectedTrack.display_track_id.desc())
        .first()
    )
    new_display_id = (max_display[0] if max_display else 0) + 1

    affected_ann_count = 0
    for ann in (
        db.query(Annotation)
        .filter(Annotation.video_id == video.id)
        .all()
    ):
        if old_tid in (ann.mouse_ids or []) and ann.end_frame >= frame:
            affected_ann_count += 1

    return {
        "operation": "split",
        "old_display_track_id": old_tid,
        "new_display_track_id": new_display_id,
        "split_frame": frame,
        "detections_before": before_count,
        "detections_after": after_count,
        "affected_annotation_count": affected_ann_count,
    }


def _validate_merge(
    db: Session,
    imp: DetectionImport,
    video: Video,
    track_ids: list[int],
) -> dict:
    """校验 Merge 输入；失败抛 400。返回校验结果字典。"""
    if len(set(track_ids)) < 2:
        raise HTTPException(status_code=400, detail="Merge requires at least two distinct track_ids")

    tracks = (
        db.query(CorrectedTrack)
        .filter(
            CorrectedTrack.detection_import_id == imp.id,
            CorrectedTrack.display_track_id.in_(track_ids),
            CorrectedTrack.active == True,
        )
        .all()
    )
    found_ids = {t.display_track_id for t in tracks}
    missing = set(track_ids) - found_ids
    if missing:
        raise HTTPException(status_code=400, detail=f"Track IDs not found or not active: {sorted(missing)}")

    sorted_tracks = sorted(tracks, key=lambda t: (
        t.first_frame if t.first_frame is not None else 0,
        t.display_track_id,
    ))
    retained = sorted_tracks[0]
    merged = [t for t in sorted_tracks if t.id != retained.id]

    id_rev = video.identity_revision
    suppressed_ids = _get_detections_without_suppressed(db, imp, id_rev)

    conflict_frames: list[int] = []
    for i, t1 in enumerate(tracks):
        for t2 in tracks[i + 1:]:
            query = (
                db.query(RawDetection.frame_index)
                .join(
                    CorrectedDetectionAssignment,
                    CorrectedDetectionAssignment.raw_detection_id == RawDetection.id,
                )
                .filter(
                    RawDetection.detection_import_id == imp.id,
                    CorrectedDetectionAssignment.corrected_track_id.in_([t1.id, t2.id]),
                    CorrectedDetectionAssignment.identity_revision == id_rev,
                )
            )
            if suppressed_ids:
                query = query.filter(
                    ~CorrectedDetectionAssignment.raw_detection_id.in_(suppressed_ids)
                )
            common = (
                query
                .group_by(RawDetection.frame_index)
                .having(func.count(RawDetection.id.distinct()) > 1)
                .all()
            )
            for (f,) in common:
                if f not in conflict_frames:
                    conflict_frames.append(f)

    conflict_frames.sort()

    affected_det_count = sum(
        db.query(CorrectedDetectionAssignment)
        .filter(
            CorrectedDetectionAssignment.corrected_track_id == t.id,
            CorrectedDetectionAssignment.identity_revision == id_rev,
        )
        .count()
        for t in merged
    )

    affected_ann_count = 0
    all_anns = (
        db.query(Annotation)
        .filter(Annotation.video_id == imp.video_id)
        .all()
    )
    for t in merged:
        for ann in all_anns:
            if t.display_track_id in (ann.mouse_ids or []):
                affected_ann_count += 1

    return {
        "operation": "merge",
        "retained_display_track_id": retained.display_track_id,
        "merged_display_track_ids": [t.display_track_id for t in merged],
        "affected_detection_count": affected_det_count,
        "affected_annotation_count": affected_ann_count,
        "conflict_frames": conflict_frames,
    }


# ---------------------------------------------------------------------------
# POST /check — 预检 Split/Merge，不持久化
# ---------------------------------------------------------------------------

@router.post("/api/projects/{project_id}/videos/{video_id}/identity-edits/check")
def check_identity_edit(
    project_id: int,
    video_id: int,
    body: IdentityEditCheckRequest,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> dict:
    video = _require_video(db, video_id, project_id)
    imp = _get_active_import(db, video_id)

    if body.operation == "split":
        return _validate_split(db, imp, video, body.track_ids, body.frame)
    elif body.operation == "merge":
        return _validate_merge(db, imp, video, body.track_ids)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown operation: {body.operation}")


# ---------------------------------------------------------------------------
# POST / — 提交 Split/Merge
# ---------------------------------------------------------------------------

@router.post("/api/projects/{project_id}/videos/{video_id}/identity-edits")
def commit_identity_edit(
    project_id: int,
    video_id: int,
    body: IdentityEditCommitRequest,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> dict:
    video = _require_video(db, video_id, project_id)
    imp = _get_active_import(db, video_id)
    _check_revisions(video, body.base_detection_import_revision, body.base_identity_revision)

    if body.operation == "split":
        _validate_split(db, imp, video, body.track_ids, body.frame)
        return _commit_split(db, imp, video, body, access[1].user_id)
    elif body.operation == "merge":
        info = _validate_merge(db, imp, video, body.track_ids)
        if info.get("conflict_frames"):
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Merge has conflict frames",
                    "conflict_frames": info["conflict_frames"],
                },
            )
        return _commit_merge(db, imp, video, body, access[1].user_id)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown operation: {body.operation}")


def _commit_split(
    db: Session,
    imp: DetectionImport,
    video: Video,
    body: IdentityEditCommitRequest,
    user_id: int,
) -> dict:
    old_tid = body.track_ids[0]
    frame = body.frame
    assert frame is not None

    track = (
        db.query(CorrectedTrack)
        .filter(
            CorrectedTrack.detection_import_id == imp.id,
            CorrectedTrack.display_track_id == old_tid,
            CorrectedTrack.active == True,
        )
        .first()
    )
    if track is None:
        raise HTTPException(status_code=400, detail=f"Track {old_tid} not found")

    max_display = (
        db.query(CorrectedTrack.display_track_id)
        .filter(CorrectedTrack.detection_import_id == imp.id, CorrectedTrack.active == True)
        .order_by(CorrectedTrack.display_track_id.desc())
        .first()
    )
    new_display_id = (max_display[0] if max_display else 0) + 1

    old_rev = video.identity_revision
    new_rev = old_rev + 1

    new_track = CorrectedTrack(
        detection_import_id=imp.id,
        display_track_id=new_display_id,
        first_frame=frame,
        last_frame=track.last_frame,
        effective_detection_count=0,
        created_identity_revision=new_rev,
        active=True,
    )
    db.add(new_track)
    db.flush()

    _materialize_cda_snapshot(db, imp.id, old_rev, new_rev)

    split_cda_rows = (
        db.query(CorrectedDetectionAssignment)
        .join(RawDetection, CorrectedDetectionAssignment.raw_detection_id == RawDetection.id)
        .filter(
            CorrectedDetectionAssignment.corrected_track_id == track.id,
            CorrectedDetectionAssignment.identity_revision == new_rev,
            RawDetection.frame_index >= frame,
        )
        .all()
    )
    affected_detection_ids = []
    for cda in split_cda_rows:
        affected_detection_ids.append(cda.raw_detection_id)
        cda.corrected_track_id = new_track.id

    if split_cda_rows:
        frames = [cda.raw_detection.frame_index for cda in split_cda_rows]
        new_track.first_frame = min(frames)
        new_track.last_frame = max(frames)
    new_track.effective_detection_count = len(split_cda_rows)

    before_cda = (
        db.query(CorrectedDetectionAssignment)
        .join(RawDetection, CorrectedDetectionAssignment.raw_detection_id == RawDetection.id)
        .filter(
            CorrectedDetectionAssignment.corrected_track_id == track.id,
            CorrectedDetectionAssignment.identity_revision == new_rev,
            RawDetection.frame_index < frame,
        )
        .all()
    )
    if before_cda:
        frames = [cda.raw_detection.frame_index for cda in before_cda]
        track.first_frame = min(frames)
        track.last_frame = max(frames)
    else:
        track.first_frame = None
        track.last_frame = None
    track.effective_detection_count = len(before_cda)

    affected_ann_ids = []
    affected_annotations_snapshot: dict[str, dict] = {}
    for ann in (
        db.query(Annotation)
        .filter(Annotation.video_id == video.id)
        .all()
    ):
        if old_tid in (ann.mouse_ids or []) and ann.end_frame >= frame:
            affected_annotations_snapshot[str(ann.id)] = {
                "mouse_ids": ann.mouse_ids,
                "mouse_id_status": ann.mouse_id_status,
            }
            ann.mouse_id_status = "needs_mouse_ids"
            affected_ann_ids.append(ann.id)

    needs_ids_ann_ids = _revalidate_video_annotations(
        db, imp, video, new_rev, set(affected_ann_ids)
    )
    video.identity_revision = new_rev

    _invalidate_review_state(
        db, video, sorted(set(affected_ann_ids) | set(needs_ids_ann_ids))
    )

    edit = IdentityEdit(
        video_id=video.id,
        detection_import_id=imp.id,
        operation="split",
        base_identity_revision=old_rev,
        result_identity_revision=new_rev,
        params={
            "old_display_track_id": old_tid,
            "new_display_track_id": new_display_id,
            "split_frame": frame,
            "affected_annotations_snapshot": affected_annotations_snapshot,
        },
        affected_detections=affected_detection_ids,
        affected_annotations=affected_ann_ids,
        operator_id=user_id,
    )
    db.add(edit)
    db.commit()

    return {
        "edit_id": edit.id,
        "identity_revision": new_rev,
        "old_display_track_id": old_tid,
        "new_display_track_id": new_display_id,
        "affected_detection_count": len(affected_detection_ids),
        "affected_annotation_count": len(affected_ann_ids),
        "needs_mouse_ids_annotation_ids": needs_ids_ann_ids,
    }


def _commit_merge(
    db: Session,
    imp: DetectionImport,
    video: Video,
    body: IdentityEditCommitRequest,
    user_id: int,
) -> dict:
    tids = body.track_ids
    tracks = (
        db.query(CorrectedTrack)
        .filter(
            CorrectedTrack.detection_import_id == imp.id,
            CorrectedTrack.display_track_id.in_(tids),
            CorrectedTrack.active == True,
        )
        .all()
    )
    if len(tracks) != len(set(tids)):
        found = {t.display_track_id for t in tracks}
        raise HTTPException(status_code=400, detail=f"Some track IDs not found: {set(tids) - found}")

    sorted_tracks = sorted(tracks, key=lambda t: (
        t.first_frame if t.first_frame is not None else 0,
        t.display_track_id,
    ))
    retained = sorted_tracks[0]
    merged = [t for t in sorted_tracks if t.id != retained.id]

    old_rev = video.identity_revision
    new_rev = old_rev + 1

    _materialize_cda_snapshot(db, imp.id, old_rev, new_rev)

    merged_track_ids = [mt.id for mt in merged]
    affected_detection_ids = []

    original_assignment_map: dict[str, list[int]] = {}
    for mt in merged:
        orig_ids = [
            r[0]
            for r in db.query(CorrectedDetectionAssignment.raw_detection_id)
            .filter(
                CorrectedDetectionAssignment.corrected_track_id == mt.id,
                CorrectedDetectionAssignment.identity_revision == old_rev,
            )
            .all()
        ]
        original_assignment_map[str(mt.display_track_id)] = orig_ids

    cda_to_reassign = (
        db.query(CorrectedDetectionAssignment)
        .filter(
            CorrectedDetectionAssignment.corrected_track_id.in_(merged_track_ids),
            CorrectedDetectionAssignment.identity_revision == new_rev,
        )
        .all()
    )
    for cda in cda_to_reassign:
        affected_detection_ids.append(cda.raw_detection_id)
        cda.corrected_track_id = retained.id

    retained_cda = (
        db.query(CorrectedDetectionAssignment.raw_detection_id)
        .join(RawDetection, CorrectedDetectionAssignment.raw_detection_id == RawDetection.id)
        .filter(
            CorrectedDetectionAssignment.corrected_track_id == retained.id,
            CorrectedDetectionAssignment.identity_revision == new_rev,
        )
        .all()
    )
    if retained_cda:
        frame_rows = (
            db.query(RawDetection.frame_index)
            .filter(RawDetection.id.in_([r[0] for r in retained_cda]))
            .all()
        )
        frames = [r[0] for r in frame_rows]
        retained.first_frame = min(frames)
        retained.last_frame = max(frames)
    retained.effective_detection_count = len(retained_cda)

    for mt in merged:
        mt.active = False
        mt.merged_into_id = retained.id

    merged_display_ids = [mt.display_track_id for mt in merged]
    affected_annotations = (
        db.query(Annotation)
        .filter(Annotation.video_id == video.id)
        .all()
    )
    affected_ann_ids = []
    needs_ids_ann_ids = []
    affected_annotations_snapshot: dict[str, dict] = {}

    for ann in affected_annotations:
        if not ann.mouse_ids:
            continue
        ids = list(ann.mouse_ids)
        changed = False
        for mid in merged_display_ids:
            if mid in ids:
                changed = True
        if not changed:
            continue

        affected_annotations_snapshot[str(ann.id)] = {
            "mouse_ids": ann.mouse_ids,
            "mouse_id_status": ann.mouse_id_status,
        }

        new_ids_set = set()
        for mid in ids:
            if mid in merged_display_ids:
                new_ids_set.add(retained.display_track_id)
            else:
                new_ids_set.add(mid)
        new_ids = sorted(new_ids_set)
        ann.mouse_ids = new_ids
        affected_ann_ids.append(ann.id)
    needs_ids_ann_ids = _revalidate_video_annotations(db, imp, video, new_rev)
    video.identity_revision = new_rev

    _invalidate_review_state(
        db, video, sorted(set(affected_ann_ids) | set(needs_ids_ann_ids))
    )

    edit = IdentityEdit(
        video_id=video.id,
        detection_import_id=imp.id,
        operation="merge",
        base_identity_revision=old_rev,
        result_identity_revision=new_rev,
        params={
            "retained_display_track_id": retained.display_track_id,
            "merged_display_track_ids": merged_display_ids,
            "original_assignment_map": original_assignment_map,
            "affected_annotations_snapshot": affected_annotations_snapshot,
        },
        affected_detections=affected_detection_ids,
        affected_annotations=affected_ann_ids,
        operator_id=user_id,
    )
    db.add(edit)
    db.commit()

    return {
        "edit_id": edit.id,
        "identity_revision": new_rev,
        "retained_display_track_id": retained.display_track_id,
        "merged_display_track_ids": merged_display_ids,
        "affected_detection_count": len(affected_detection_ids),
        "affected_annotation_count": len(affected_ann_ids),
        "needs_mouse_ids_annotation_ids": needs_ids_ann_ids,
    }


# ---------------------------------------------------------------------------
# GET /history — 获取最近的身份编辑历史，供前端撤销按钮使用
# ---------------------------------------------------------------------------

@router.get(
    "/api/projects/{project_id}/videos/{video_id}/identity-edits/history",
    response_model=list[IdentityEditOut],
)
def get_identity_edit_history(
    project_id: int,
    video_id: int,
    limit: int = 1,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> list[IdentityEdit]:
    video = _require_video(db, video_id, project_id)
    edits = (
        db.query(IdentityEdit)
        .filter(
            IdentityEdit.video_id == video.id,
            IdentityEdit.reverted_edit_id == None,
        )
        .order_by(IdentityEdit.created_at.desc())
        .limit(max(1, limit))
        .all()
    )
    return edits


# ---------------------------------------------------------------------------
# POST /{eid}/revert — 撤销
# ---------------------------------------------------------------------------

@router.post("/api/projects/{project_id}/videos/{video_id}/identity-edits/{edit_id}/revert")
def revert_identity_edit(
    project_id: int,
    video_id: int,
    edit_id: int,
    body: IdentityEditRevertRequest,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> dict:
    video = _require_video(db, video_id, project_id)
    imp = _get_active_import(db, video_id)
    _check_revisions(video, body.base_detection_import_revision, body.base_identity_revision)

    edit = db.get(IdentityEdit, edit_id)
    if edit is None or edit.video_id != video_id:
        raise HTTPException(status_code=404, detail="Identity edit not found")
    if edit.operation not in ("split", "merge"):
        raise HTTPException(status_code=400, detail=f"Cannot revert operation '{edit.operation}'")

    existing_revert = (
        db.query(IdentityEdit)
        .filter(IdentityEdit.reverted_edit_id == edit_id)
        .first()
    )
    if existing_revert is not None:
        raise HTTPException(status_code=409, detail="This edit has already been reverted")

    old_rev = video.identity_revision
    new_rev = old_rev + 1

    _materialize_cda_snapshot(db, imp.id, old_rev, new_rev)

    if edit.operation == "split":
        _revert_split(db, imp, video, edit, new_rev)
    elif edit.operation == "merge":
        _revert_merge(db, imp, video, edit, new_rev)

    _restore_annotation_snapshots(db, edit)

    db.flush()
    needs_ids = _revalidate_video_annotations(db, imp, video, new_rev)

    revert_edit = IdentityEdit(
        video_id=video.id,
        detection_import_id=imp.id,
        operation="revert",
        base_identity_revision=old_rev,
        result_identity_revision=new_rev,
        params={"reverted_operation": edit.operation, "original_edit_id": edit.id},
        operator_id=access[1].user_id,
        reverted_edit_id=edit.id,
    )
    db.add(revert_edit)
    video.identity_revision = new_rev

    affected = sorted(set(edit.affected_annotations or []) | set(needs_ids))
    _invalidate_review_state(db, video, affected)

    db.commit()

    return {
        "identity_revision": new_rev,
        "reverted_edit_id": edit.id,
        "message": f"Reverted {edit.operation} (edit {edit.id})",
    }


def _restore_annotation_snapshots(db: Session, edit: IdentityEdit) -> None:
    params = edit.params or {}
    snapshot = params.get("affected_annotations_snapshot", {})
    if not snapshot:
        return
    for ann_id_str, before in snapshot.items():
        ann = db.get(Annotation, int(ann_id_str))
        if ann is None:
            continue
        ann.mouse_ids = before.get("mouse_ids", ann.mouse_ids)
        ann.mouse_id_status = before.get("mouse_id_status", ann.mouse_id_status)


def _revert_split(
    db: Session,
    imp: DetectionImport,
    video: Video,
    edit: IdentityEdit,
    new_rev: int,
) -> None:
    params = edit.params or {}
    new_display_id = params.get("new_display_track_id")

    new_track_obj = (
        db.query(CorrectedTrack)
        .filter(
            CorrectedTrack.detection_import_id == imp.id,
            CorrectedTrack.display_track_id == new_display_id,
            CorrectedTrack.active == True,
        )
        .first()
    )
    if new_track_obj is None:
        raise HTTPException(status_code=400, detail="Split created track not found; cannot revert")

    old_tid = params.get("old_display_track_id")
    old_track = (
        db.query(CorrectedTrack)
        .filter(
            CorrectedTrack.detection_import_id == imp.id,
            CorrectedTrack.display_track_id == old_tid,
            CorrectedTrack.active == True,
        )
        .first()
    )
    if old_track is None:
        raise HTTPException(status_code=400, detail="Original track not found; cannot revert")

    cda_rows = (
        db.query(CorrectedDetectionAssignment)
        .filter(
            CorrectedDetectionAssignment.corrected_track_id == new_track_obj.id,
            CorrectedDetectionAssignment.identity_revision == new_rev,
        )
        .all()
    )
    for cda in cda_rows:
        cda.corrected_track_id = old_track.id

    all_frames = (
        db.query(RawDetection.frame_index)
        .join(
            CorrectedDetectionAssignment,
            CorrectedDetectionAssignment.raw_detection_id == RawDetection.id,
        )
        .filter(
            CorrectedDetectionAssignment.corrected_track_id == old_track.id,
            CorrectedDetectionAssignment.identity_revision == new_rev,
        )
        .all()
    )
    if all_frames:
        frames = [r[0] for r in all_frames]
        old_track.first_frame = min(frames)
        old_track.last_frame = max(frames)
    old_track.effective_detection_count = len(all_frames)

    new_track_obj.active = False
    new_track_obj.merged_into_id = old_track.id


def _revert_merge(
    db: Session,
    imp: DetectionImport,
    video: Video,
    edit: IdentityEdit,
    new_rev: int,
) -> None:
    params = edit.params or {}
    merged_ids = params.get("merged_display_track_ids", [])
    retained_id = params.get("retained_display_track_id")
    assignment_map = params.get("original_assignment_map", {})

    retained_track = (
        db.query(CorrectedTrack)
        .filter(
            CorrectedTrack.detection_import_id == imp.id,
            CorrectedTrack.display_track_id == retained_id,
            CorrectedTrack.active == True,
        )
        .first()
    )
    if retained_track is None:
        raise HTTPException(status_code=400, detail="Retained track not found; cannot revert")

    for mid in merged_ids:
        merged_track = (
            db.query(CorrectedTrack)
            .filter(
                CorrectedTrack.detection_import_id == imp.id,
                CorrectedTrack.display_track_id == mid,
            )
            .first()
        )
        if merged_track is None:
            continue
        merged_track.active = True
        merged_track.merged_into_id = None

        orig_det_ids = assignment_map.get(str(mid), [])
        if orig_det_ids:
            cda_rows = (
                db.query(CorrectedDetectionAssignment)
                .filter(
                    CorrectedDetectionAssignment.corrected_track_id == retained_track.id,
                    CorrectedDetectionAssignment.identity_revision == new_rev,
                    CorrectedDetectionAssignment.raw_detection_id.in_(orig_det_ids),
                )
                .all()
            )
            for cda in cda_rows:
                cda.corrected_track_id = merged_track.id

        track_frames = (
            db.query(RawDetection.frame_index)
            .join(
                CorrectedDetectionAssignment,
                CorrectedDetectionAssignment.raw_detection_id == RawDetection.id,
            )
            .filter(
                CorrectedDetectionAssignment.corrected_track_id == merged_track.id,
                CorrectedDetectionAssignment.identity_revision == new_rev,
            )
            .all()
        )
        if track_frames:
            frames = [r[0] for r in track_frames]
            merged_track.first_frame = min(frames)
            merged_track.last_frame = max(frames)
        merged_track.effective_detection_count = len(track_frames)

    retained_frames = (
        db.query(RawDetection.frame_index)
        .join(
            CorrectedDetectionAssignment,
            CorrectedDetectionAssignment.raw_detection_id == RawDetection.id,
        )
        .filter(
            CorrectedDetectionAssignment.corrected_track_id == retained_track.id,
            CorrectedDetectionAssignment.identity_revision == new_rev,
        )
        .all()
    )
    if retained_frames:
        frames = [r[0] for r in retained_frames]
        retained_track.first_frame = min(frames)
        retained_track.last_frame = max(frames)
    retained_track.effective_detection_count = len(retained_frames)
