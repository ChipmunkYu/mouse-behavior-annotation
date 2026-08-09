"""Phase 2 验收：身份编辑（Split/Merge/检查/撤销）、检测抑制、标注 mouse_ids。

覆盖：
- Split: CorrectedTrack 创建、分配拆分、display_track_id=max+1、
  identity_revision 递增、跨 F 标注进入 needs_mouse_ids
- Merge: 轨迹合并、保留最早 ID、同帧冲突检测、identity_revision 递增、
  Annotation.mouse_ids 更新
- Check: 返回正确信息但不持久化
- Revert: 恢复状态、可逆性
- 409 stale base revision
- 抑制: 检测冻结、受影响标注更新
- Annotation create/update with mouse_ids: 合法、非法数量、不存在的 track_id、无覆盖
- 行为类别约束: 个体 1只、社交 2只、扎堆 >=2
"""
from __future__ import annotations

import json

import pytest

from app import models


# ---------------------------------------------------------------------------
# 测试数据工厂（复用 Phase 1B 的 SAMPLE_METADATA + make_tracks_jsonl）
# ---------------------------------------------------------------------------

SAMPLE_METADATA = {
    "schema_version": "1.0",
    "video_id": "test-mouse-video",
    "width": 1280,
    "height": 720,
    "fps": 25.0,
    "frame_count": 5,
    "model_name": "yolov8s-pose",
    "model_weights_sha256": "a" * 64,
    "tracker_name": "botsort",
    "tracker_params": {"track_high_thresh": 0.5},
    "keypoint_names": ["nose", "left_ear", "right_ear"],
    "skeleton_edges": [[0, 1], [0, 2]],
}


def _make_frame_line(frame_index: int, detections: list[dict]) -> str:
    return json.dumps({
        "schema_version": "1.0",
        "video_id": "test-mouse-video",
        "frame_index": frame_index,
        "timestamp_sec": frame_index / 25.0,
        "detection_count": len(detections),
        "detections": detections,
    })


def _det(track_id: int, x1=100, y1=200, x2=180, y2=310, confidence=0.85) -> dict:
    return {
        "track_id": track_id,
        "box_xyxy_px": [x1, y1, x2, y2],
        "box_xywhn": [0.25, 0.35, 0.04, 0.10],
        "area_n": 0.004,
        "detection_confidence": confidence,
        "class_id": 0,
        "keypoints": [
            {"x_px": 140.0, "y_px": 250.0, "confidence": 0.95},
            {"x_px": 150.0, "y_px": 260.0, "confidence": 0.88},
            {"x_px": 130.0, "y_px": 240.0, "confidence": 0.91},
        ],
    }


def make_tracks_jsonl() -> str:
    """5 frames, 3 track IDs: frame 0: 1,2; frame 1: 1,2,3; frame 2: 1,3;
    frame 3: 2,3; frame 4: 1,2,3."""
    lines = [
        _make_frame_line(0, [_det(1), _det(2)]),
        _make_frame_line(1, [_det(1), _det(2), _det(3)]),
        _make_frame_line(2, [_det(1), _det(3)]),
        _make_frame_line(3, [_det(2), _det(3)]),
        _make_frame_line(4, [_det(1), _det(2), _det(3)]),
    ]
    return "\n".join(lines) + "\n"


def _make_mergeable_jsonl() -> str:
    """5 frames, 3 tracks with disjoint frame ranges (no conflicts for merge).
    Track 1: frames 0,1; Track 2: frames 3,4; Track 3: frames 2."""
    lines = [
        _make_frame_line(0, [_det(1)]),
        _make_frame_line(1, [_det(1)]),
        _make_frame_line(2, [_det(3)]),
        _make_frame_line(3, [_det(2)]),
        _make_frame_line(4, [_det(2)]),
    ]
    return "\n".join(lines) + "\n"


_MERGEABLE_METADATA = {
    "schema_version": "1.0",
    "video_id": "test-mouse-video",
    "width": 1280,
    "height": 720,
    "fps": 25.0,
    "frame_count": 5,
    "model_name": "yolov8s-pose",
    "model_weights_sha256": "a" * 64,
    "tracker_name": "botsort",
    "tracker_params": {"track_high_thresh": 0.5},
    "keypoint_names": ["nose", "left_ear", "right_ear"],
    "skeleton_edges": [[0, 1], [0, 2]],
}


def _make_mergeable_metadata_json() -> str:
    return json.dumps(_MERGEABLE_METADATA)


def _setup_mergeable_import(ctx, login_headers) -> tuple[dict, int, int]:
    """Helper: setup import with non-overlapping tracks for merge tests."""
    headers = login_headers()
    project = ctx.client.post(
        "/api/projects", json={"name": "Merge测试项目", "description": "test"}, headers=headers
    ).json()
    pid = project["id"]

    batch = ctx.client.post(
        f"/api/projects/{pid}/video-import-batches", headers=headers
    )
    assert batch.status_code == 201, batch.text
    bid = batch.json()["id"]

    ctx.client.put(
        f"/api/projects/{pid}/video-import-batches/{bid}/files/video",
        files={"file": ("clip.mp4", b"FAKE-MP4")},
        headers=headers,
    )
    ctx.client.put(
        f"/api/projects/{pid}/video-import-batches/{bid}/files/tracks",
        files={"file": ("tracks.jsonl", _make_mergeable_jsonl().encode())},
        headers=headers,
    )
    ctx.client.put(
        f"/api/projects/{pid}/video-import-batches/{bid}/files/metadata",
        files={"file": ("metadata.json", _make_mergeable_metadata_json().encode())},
        headers=headers,
    )
    resp = ctx.client.post(
        f"/api/projects/{pid}/video-import-batches/{bid}/complete",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    vid = resp.json()["video_id"]
    return headers, pid, vid


def make_metadata_json() -> str:
    return json.dumps(SAMPLE_METADATA)


# Track coverage:
# Track 1: frames 0,1,2,4  (first=0, last=4)
# Track 2: frames 0,1,3,4  (first=0, last=4)
# Track 3: frames 1,2,3,4  (first=1, last=4)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _setup_complete_import(ctx, login_headers) -> tuple[dict, int, int]:
    headers = login_headers()
    project = ctx.client.post(
        "/api/projects", json={"name": "Phase2 测试项目", "description": "test"}, headers=headers
    ).json()
    pid = project["id"]

    batch = ctx.client.post(
        f"/api/projects/{pid}/video-import-batches", headers=headers
    )
    assert batch.status_code == 201, batch.text
    bid = batch.json()["id"]

    ctx.client.put(
        f"/api/projects/{pid}/video-import-batches/{bid}/files/video",
        files={"file": ("clip.mp4", b"FAKE-MP4")},
        headers=headers,
    )
    ctx.client.put(
        f"/api/projects/{pid}/video-import-batches/{bid}/files/tracks",
        files={"file": ("tracks.jsonl", make_tracks_jsonl().encode())},
        headers=headers,
    )
    ctx.client.put(
        f"/api/projects/{pid}/video-import-batches/{bid}/files/metadata",
        files={"file": ("metadata.json", make_metadata_json().encode())},
        headers=headers,
    )
    resp = ctx.client.post(
        f"/api/projects/{pid}/video-import-batches/{bid}/complete",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    vid = resp.json()["video_id"]
    return headers, pid, vid


def _get_identity_url(pid: int, vid: int) -> str:
    return f"/api/projects/{pid}/videos/{vid}/identity-edits"


def _get_annotations_url(pid: int, vid: int) -> str:
    return f"/api/projects/{pid}/videos/{vid}/annotations"


def _get_suppression_url(pid: int, vid: int) -> str:
    return f"/api/projects/{pid}/videos/{vid}/detection-suppressions"


def _seed_legacy_detection_suppression(ctx, vid: int, detection_id: int) -> int:
    """直接写入旧版单框抑制，并物化其 identity revision 快照。"""
    with ctx.session_factory() as db:
        video = db.get(models.Video, vid)
        imp = db.query(models.DetectionImport).filter(
            models.DetectionImport.video_id == vid,
            models.DetectionImport.active == True,
        ).one()
        assert video.identity_revision == 0
        assignments = db.query(models.CorrectedDetectionAssignment).filter(
            models.CorrectedDetectionAssignment.identity_revision == 0,
            models.CorrectedDetectionAssignment.raw_detection_id.in_(
                db.query(models.RawDetection.id).filter(
                    models.RawDetection.detection_import_id == imp.id
                )
            ),
        ).all()
        db.add_all([
            models.CorrectedDetectionAssignment(
                raw_detection_id=row.raw_detection_id,
                corrected_track_id=row.corrected_track_id,
                identity_revision=1,
            )
            for row in assignments
        ])
        suppression = models.DetectionSuppression(
            video_id=vid,
            detection_import_id=imp.id,
            base_identity_revision=0,
            result_identity_revision=1,
            scope="detection",
        )
        db.add(suppression)
        db.flush()
        db.add(models.SuppressionDetection(
            suppression_id=suppression.id,
            raw_detection_id=detection_id,
        ))
        video.identity_revision = 1
        db.commit()
        return suppression.id


def _get_category(ctx, pid, name="静止") -> dict:
    from .conftest import auth_headers as _ah
    hdrs = _ah(ctx.client)
    cats = ctx.client.get(f"/api/projects/{pid}/categories", headers=hdrs).json()
    for c in cats:
        if c["name"] == name:
            return c
    return cats[0]


def _get_social_category(ctx, pid) -> dict:
    from .conftest import auth_headers as _ah
    hdrs = _ah(ctx.client)
    cats = ctx.client.get(f"/api/projects/{pid}/categories", headers=hdrs).json()
    for c in cats:
        if c["mouse_count_min"] == 2 and c["mouse_count_max"] == 2:
            return c
    return cats[1]


# ---------------------------------------------------------------------------
# Split tests
# ---------------------------------------------------------------------------

def test_split_check_returns_correct_info(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    # Split track 1 at frame 2: frames <2 should be 2 detections (frame 0,1), >=2 should be 2 (frame 2,4)
    resp = ctx.client.post(
        f"{_get_identity_url(pid, vid)}/check",
        json={
            "operation": "split",
            "track_ids": [1],
            "frame": 2,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation"] == "split"
    assert body["old_display_track_id"] == 1
    assert body["new_display_track_id"] == 4  # max existing is 3
    assert body["detections_before"] == 2  # frame 0, 1
    assert body["detections_after"] == 2  # frame 2, 4
    assert body["split_frame"] == 2

    # Verify nothing was persisted
    with ctx.session_factory() as db:
        tracks = db.query(models.CorrectedTrack).filter(
            models.CorrectedTrack.detection_import_id ==
            db.query(models.DetectionImport).filter(
                models.DetectionImport.video_id == vid, models.DetectionImport.active == True
            ).first().id,
            models.CorrectedTrack.active == True,
        ).count()
        assert tracks == 3  # still only 3 tracks


def test_split_check_rejects_invalid_frame(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    # Split at frame 0 (not after first_frame): should reject
    resp = ctx.client.post(
        f"{_get_identity_url(pid, vid)}/check",
        json={
            "operation": "split",
            "track_ids": [1],
            "frame": 0,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 400, resp.text

    # Split at frame 5 (after last_frame): should reject
    resp = ctx.client.post(
        f"{_get_identity_url(pid, vid)}/check",
        json={
            "operation": "split",
            "track_ids": [1],
            "frame": 5,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 400, resp.text


def test_split_commit(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    # Split track 1 at frame 2
    resp = ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "split",
            "track_ids": [1],
            "frame": 2,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["identity_revision"] == 1
    assert body["old_display_track_id"] == 1
    assert body["new_display_track_id"] == 4
    assert body["affected_detection_count"] == 2

    with ctx.session_factory() as db:
        imp = db.query(models.DetectionImport).filter(
            models.DetectionImport.video_id == vid, models.DetectionImport.active == True
        ).first()
        active_tracks = db.query(models.CorrectedTrack).filter(
            models.CorrectedTrack.detection_import_id == imp.id,
            models.CorrectedTrack.active == True,
        ).all()
        display_ids = {t.display_track_id for t in active_tracks}
        assert display_ids == {1, 2, 3, 4}

        video = db.get(models.Video, vid)
        assert video.identity_revision == 1

        # Check assignments: old track 1 should have frames 0,1; new track 4 should have frames 2,4
        old_track = next(t for t in active_tracks if t.display_track_id == 1)
        new_track = next(t for t in active_tracks if t.display_track_id == 4)

        import statistics

        old_frames = [
            r[0] for r in db.query(models.RawDetection.frame_index)
            .join(models.CorrectedDetectionAssignment,
                  models.CorrectedDetectionAssignment.raw_detection_id == models.RawDetection.id)
            .filter(
                models.CorrectedDetectionAssignment.corrected_track_id == old_track.id,
                models.CorrectedDetectionAssignment.identity_revision == 1,
            ).all()
        ]
        new_frames = [
            r[0] for r in db.query(models.RawDetection.frame_index)
            .join(models.CorrectedDetectionAssignment,
                  models.CorrectedDetectionAssignment.raw_detection_id == models.RawDetection.id)
            .filter(
                models.CorrectedDetectionAssignment.corrected_track_id == new_track.id,
                models.CorrectedDetectionAssignment.identity_revision == 1,
            ).all()
        ]
        assert sorted(old_frames) == [0, 1]
        assert sorted(new_frames) == [2, 4]

        # Check IdentityEdit audit row
        edit = db.query(models.IdentityEdit).filter(
            models.IdentityEdit.video_id == vid,
            models.IdentityEdit.operation == "split",
        ).first()
        assert edit is not None
        assert edit.base_identity_revision == 0
        assert edit.result_identity_revision == 1


def test_split_annotations_crossing_F_get_needs_mouse_ids(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    # Create an annotation that contains track 1 and crosses frame 2
    cat = _get_category(ctx, pid, "静止")
    ann_resp = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": cat["id"],
            "start_time": 0.0,
            "end_time": 4.0,
            "start_frame": 0,
            "end_frame": 4,
            "mouse_ids": [1],
            "detection_import_revision": 1,
            "identity_revision": 0,
        },
        headers=headers,
    )
    assert ann_resp.status_code == 201, ann_resp.text
    ann_id = ann_resp.json()["id"]
    assert ann_resp.json()["mouse_id_status"] == "valid"

    # Split track 1 at frame 2
    resp = ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "split",
            "track_ids": [1],
            "frame": 2,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["needs_mouse_ids_annotation_ids"]) == 1

    # Verify annotation is now needs_mouse_ids
    ann = ctx.client.get(_get_annotations_url(pid, vid), headers=headers).json()
    found = next(a for a in ann if a["id"] == ann_id)
    assert found["mouse_id_status"] == "needs_mouse_ids"


def test_partial_split_advances_unrelated_valid_annotation_and_can_submit(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    cat = _get_category(ctx, pid, "静止")
    ann_resp = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": cat["id"],
            "start_time": 0.0,
            "end_time": 0.16,
            "start_frame": 0,
            "end_frame": 4,
            "mouse_ids": [2],
        },
        headers=headers,
    )
    assert ann_resp.status_code == 201, ann_resp.text

    split = ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "split",
            "track_ids": [1],
            "frame": 2,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert split.status_code == 200, split.text

    annotation = next(
        ann for ann in ctx.client.get(_get_annotations_url(pid, vid), headers=headers).json()
        if ann["id"] == ann_resp.json()["id"]
    )
    assert annotation["mouse_id_status"] == "valid"
    assert annotation["detection_import_revision"] == 1
    assert annotation["identity_revision"] == 1
    submitted = ctx.client.post(
        f"/api/projects/{pid}/videos/{vid}/submit", headers=headers
    )
    assert submitted.status_code == 200, submitted.text


# ---------------------------------------------------------------------------
# Merge tests
# ---------------------------------------------------------------------------

def test_merge_check(ctx, login_headers):
    headers, pid, vid = _setup_mergeable_import(ctx, login_headers)

    resp = ctx.client.post(
        f"{_get_identity_url(pid, vid)}/check",
        json={
            "operation": "merge",
            "track_ids": [1, 2],
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation"] == "merge"
    assert body["retained_display_track_id"] in (1, 2)
    assert body["conflict_frames"] == []

    resp2 = ctx.client.get(
        f"/api/projects/{pid}/videos/{vid}/corrected-tracks",
        headers=headers,
    )
    assert resp2.json()["total"] == 3


def test_merge_check_rejects_single_track(ctx, login_headers):
    headers, pid, vid = _setup_mergeable_import(ctx, login_headers)

    resp = ctx.client.post(
        f"{_get_identity_url(pid, vid)}/check",
        json={
            "operation": "merge",
            "track_ids": [1],
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 400


def test_merge_rejects_duplicate_track_ids(ctx, login_headers):
    headers, pid, vid = _setup_mergeable_import(ctx, login_headers)
    payload = {
        "operation": "merge",
        "track_ids": [1, 1],
        "base_identity_revision": 0,
        "base_detection_import_revision": 1,
    }
    checked = ctx.client.post(
        f"{_get_identity_url(pid, vid)}/check", json=payload, headers=headers
    )
    assert checked.status_code == 400
    assert "distinct" in checked.json()["detail"].lower()
    committed = ctx.client.post(
        _get_identity_url(pid, vid), json=payload, headers=headers
    )
    assert committed.status_code == 400

    with ctx.session_factory() as db:
        assert db.get(models.Video, vid).identity_revision == 0


def test_merge_commit(ctx, login_headers):
    headers, pid, vid = _setup_mergeable_import(ctx, login_headers)

    resp = ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "merge",
            "track_ids": [1, 2],
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["identity_revision"] == 1
    assert body["retained_display_track_id"] in (1, 2)
    assert len(body["merged_display_track_ids"]) == 1

    with ctx.session_factory() as db:
        imp = db.query(models.DetectionImport).filter(
            models.DetectionImport.video_id == vid, models.DetectionImport.active == True
        ).first()
        active_tracks = db.query(models.CorrectedTrack).filter(
            models.CorrectedTrack.detection_import_id == imp.id,
            models.CorrectedTrack.active == True,
        ).all()
        display_ids = {t.display_track_id for t in active_tracks}
        assert display_ids == {body["retained_display_track_id"], 3}

        video = db.get(models.Video, vid)
        assert video.identity_revision == 1


def test_merge_updates_annotation_mouse_ids(ctx, login_headers):
    headers, pid, vid = _setup_mergeable_import(ctx, login_headers)
    social_cat = _get_social_category(ctx, pid)

    ann_resp = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": social_cat["id"],
            "start_time": 0.0,
            "end_time": 0.16,
            "start_frame": 0,
            "end_frame": 4,
            "mouse_ids": [1, 2],
        },
        headers=headers,
    )
    assert ann_resp.status_code == 201, ann_resp.text
    ann_id = ann_resp.json()["id"]

    resp = ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "merge",
            "track_ids": [1, 2],
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    ann = ctx.client.get(_get_annotations_url(pid, vid), headers=headers).json()
    found = next(a for a in ann if a["id"] == ann_id)
    assert resp.json()["retained_display_track_id"] in found["mouse_ids"]
    assert found["mouse_id_status"] == "needs_mouse_ids"


def test_revert_merge(ctx, login_headers):
    headers, pid, vid = _setup_mergeable_import(ctx, login_headers)

    merge_resp = ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "merge",
            "track_ids": [1, 2],
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert merge_resp.status_code == 200, merge_resp.text
    new_rev = merge_resp.json()["identity_revision"]

    with ctx.session_factory() as db:
        edit = db.query(models.IdentityEdit).filter(
            models.IdentityEdit.video_id == vid,
            models.IdentityEdit.operation == "merge",
        ).first()
        edit_id = edit.id

    revert_resp = ctx.client.post(
        f"{_get_identity_url(pid, vid)}/{edit_id}/revert",
        json={
            "base_identity_revision": new_rev,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert revert_resp.status_code == 200, revert_resp.text

    with ctx.session_factory() as db:
        imp = db.query(models.DetectionImport).filter(
            models.DetectionImport.video_id == vid, models.DetectionImport.active == True
        ).first()
        active_tracks = db.query(models.CorrectedTrack).filter(
            models.CorrectedTrack.detection_import_id == imp.id,
            models.CorrectedTrack.active == True,
        ).all()
        display_ids = {t.display_track_id for t in active_tracks}
        assert display_ids == {1, 2, 3}


def test_revert_merge_restores_annotation_snapshots(ctx, login_headers):
    headers, pid, vid = _setup_mergeable_import(ctx, login_headers)
    social_cat = _get_social_category(ctx, pid)

    ann_resp = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": social_cat["id"],
            "start_time": 0.0,
            "end_time": 0.16,
            "start_frame": 0,
            "end_frame": 4,
            "mouse_ids": [1, 2],
        },
        headers=headers,
    )
    assert ann_resp.status_code == 201, ann_resp.text
    ann_id = ann_resp.json()["id"]

    merge_resp = ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "merge",
            "track_ids": [1, 2],
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert merge_resp.status_code == 200
    new_rev = merge_resp.json()["identity_revision"]

    with ctx.session_factory() as db:
        edit = db.query(models.IdentityEdit).filter(
            models.IdentityEdit.video_id == vid,
            models.IdentityEdit.operation == "merge",
        ).first()
        edit_id = edit.id

    revert_resp = ctx.client.post(
        f"{_get_identity_url(pid, vid)}/{edit_id}/revert",
        json={
            "base_identity_revision": new_rev,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert revert_resp.status_code == 200

    anns2 = ctx.client.get(_get_annotations_url(pid, vid), headers=headers).json()
    found2 = next(a for a in anns2 if a["id"] == ann_id)
    assert found2["mouse_ids"] == [1, 2]
    assert found2["mouse_id_status"] == "valid"


# ---------------------------------------------------------------------------
# 409 stale base revision
# ---------------------------------------------------------------------------

def test_stale_revision_409(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    # First split
    resp1 = ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "split",
            "track_ids": [1],
            "frame": 2,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp1.status_code == 200, resp1.text

    # Second split with stale revision (0 instead of 1)
    resp2 = ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "split",
            "track_ids": [2],
            "frame": 2,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp2.status_code == 409


# ---------------------------------------------------------------------------
# Annotation create/update with mouse_ids
# ---------------------------------------------------------------------------

def test_create_annotation_with_valid_mouse_ids(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    cat = _get_category(ctx, pid, "静止")  # individual: min=1, max=1

    resp = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": cat["id"],
            "start_time": 0.0,
            "end_time": 4.0,
            "start_frame": 0,
            "end_frame": 4,
            "mouse_ids": [1],
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    ann = resp.json()
    assert ann["mouse_ids"] == [1]
    assert ann["mouse_id_status"] == "valid"
    assert ann["detection_import_revision"] == 1
    assert ann["identity_revision"] == 0


def test_create_annotation_mouse_ids_wrong_count(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    cat = _get_category(ctx, pid, "静止")  # individual: min=1, max=1

    # 2 IDs for individual behavior
    resp = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": cat["id"],
            "start_time": 0.0,
            "end_time": 4.0,
            "start_frame": 0,
            "end_frame": 4,
            "mouse_ids": [1, 2],
        },
        headers=headers,
    )
    assert resp.status_code == 400
    assert "at most 1" in resp.json()["detail"].lower()


def test_create_annotation_nonexistent_track_id(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    cat = _get_category(ctx, pid, "静止")

    resp = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": cat["id"],
            "start_time": 0.0,
            "end_time": 4.0,
            "start_frame": 0,
            "end_frame": 4,
            "mouse_ids": [99],
        },
        headers=headers,
    )
    assert resp.status_code == 400
    assert "not an active corrected track" in resp.json()["detail"].lower()


def test_create_annotation_no_coverage(ctx, login_headers):
    """Track ID exists but has no detection in the specified frame range."""
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    cat = _get_category(ctx, pid, "静止")

    # Track 3 appears at frames 1,2,3,4. Frame range 0-1 includes frame 0 (no detection for track 3)
    # But track 3 appears at frame 1 which is in range, so it has coverage.
    # Instead, test with a frame range beyond the video: frames 5-6
    # Actually the data has 5 frames (0-4), so frame 3 is where track 1 has no detection.
    # Use frame 3-4 for track 1: track 1 is NOT at frame 3, but IS at frame 4.
    # Need a range where the track has truly no detections.
    # Let's suppress all of track 3 and then try to use it.

    # First suppress track 3
    ctx.client.post(
        _get_suppression_url(pid, vid),
        json={
            "scope": "corrected_track",
            "track_id": 3,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )

    # Now track 3 has no unsuppressed detections
    resp = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": cat["id"],
            "start_time": 0.0,
            "end_time": 0.16,
            "start_frame": 1,
            "end_frame": 4,
            "mouse_ids": [3],
        },
        headers=headers,
    )
    assert resp.status_code == 400
    assert "no unsuppressed detections" in resp.json()["detail"].lower()


def test_create_annotation_without_mouse_ids_gets_needs_mouse_ids(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    cat = _get_category(ctx, pid, "静止")

    resp = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": cat["id"],
            "start_time": 0.0,
            "end_time": 4.0,
            "start_frame": 0,
            "end_frame": 4,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    ann = resp.json()
    assert ann["mouse_ids"] == []
    assert ann["mouse_id_status"] == "needs_mouse_ids"


def test_create_annotation_social_requires_two(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    social_cat = _get_social_category(ctx, pid)

    # Only 1 mouse_id for social behavior
    resp = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": social_cat["id"],
            "start_time": 0.0,
            "end_time": 4.0,
            "start_frame": 0,
            "end_frame": 4,
            "mouse_ids": [1],
        },
        headers=headers,
    )
    assert resp.status_code == 400
    assert "requires at least 2" in resp.json()["detail"].lower()


def test_create_annotation_social_two_valid(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    social_cat = _get_social_category(ctx, pid)

    resp = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": social_cat["id"],
            "start_time": 0.0,
            "end_time": 4.0,
            "start_frame": 0,
            "end_frame": 4,
            "mouse_ids": [1, 2],
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["mouse_ids"] == [1, 2]
    assert resp.json()["mouse_id_status"] == "valid"


def test_category_zhadui_allows_many(ctx, login_headers):
    """扎堆行为: min>=2, max=None."""
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    cats = ctx.client.get(f"/api/projects/{pid}/categories", headers=headers).json()
    zhadui = next(c for c in cats if c["name"] == "扎堆行为")

    resp = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": zhadui["id"],
            "start_time": 0.0,
            "end_time": 4.0,
            "start_frame": 0,
            "end_frame": 4,
            "mouse_ids": [1, 2, 3],
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["mouse_id_status"] == "valid"

    # 扎堆也需要至少2只
    resp2 = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": zhadui["id"],
            "start_time": 1.0,
            "end_time": 3.0,
            "start_frame": 1,
            "end_frame": 3,
            "mouse_ids": [1],
        },
        headers=headers,
    )
    assert resp2.status_code == 400


def test_update_annotation_adds_mouse_ids(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    cat = _get_category(ctx, pid, "静止")

    # Create without mouse_ids
    create_resp = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": cat["id"],
            "start_time": 0.0,
            "end_time": 4.0,
            "start_frame": 0,
            "end_frame": 4,
        },
        headers=headers,
    )
    assert create_resp.status_code == 201
    ann_id = create_resp.json()["id"]
    assert create_resp.json()["mouse_id_status"] == "needs_mouse_ids"

    # Update with valid mouse_ids
    update_resp = ctx.client.patch(
        f"{_get_annotations_url(pid, vid)}/{ann_id}",
        json={"mouse_ids": [1]},
        headers=headers,
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["mouse_ids"] == [1]
    assert update_resp.json()["mouse_id_status"] == "valid"


# ---------------------------------------------------------------------------
# submit_video: mouse_ids requirement
# ---------------------------------------------------------------------------

def test_submit_requires_all_mouse_ids_valid(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    cat = _get_category(ctx, pid, "静止")

    # Create annotation without mouse_ids
    ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": cat["id"],
            "start_time": 0.0,
            "end_time": 4.0,
            "start_frame": 0,
            "end_frame": 4,
        },
        headers=headers,
    )

    # Submit should fail
    submit_resp = ctx.client.post(
        f"/api/projects/{pid}/videos/{vid}/submit",
        headers=headers,
    )
    assert submit_resp.status_code == 400
    assert "needs_mouse_ids" in submit_resp.json()["detail"].lower() or "mouse" in submit_resp.json()["detail"].lower()


def test_submit_succeeds_with_all_valid(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    cat = _get_category(ctx, pid, "静止")

    # Create annotation with valid mouse_ids
    ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": cat["id"],
            "start_time": 0.0,
            "end_time": 4.0,
            "start_frame": 0,
            "end_frame": 4,
            "mouse_ids": [1],
        },
        headers=headers,
    )

    submit_resp = ctx.client.post(
        f"/api/projects/{pid}/videos/{vid}/submit",
        headers=headers,
    )
    assert submit_resp.status_code == 200, submit_resp.text


# ---------------------------------------------------------------------------
# Detection Suppression tests
# ---------------------------------------------------------------------------

def test_suppress_single_detection_request_rejected_without_changes(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    # Get a detection ID
    dets = ctx.client.get(
        f"/api/projects/{pid}/videos/{vid}/detections?start_frame=0&end_frame=1",
        headers=headers,
    ).json()
    detection_id = dets["detections"][0]["detection_id"]

    resp = ctx.client.post(
        _get_suppression_url(pid, vid),
        json={
            "scope": "detection",
            "detection_id": detection_id,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text

    with ctx.session_factory() as db:
        supp_count = db.query(models.DetectionSuppression).filter(
            models.DetectionSuppression.video_id == vid
        ).count()
        assert supp_count == 0
        sd_count = db.query(models.SuppressionDetection).filter(
            models.SuppressionDetection.raw_detection_id == detection_id
        ).count()
        assert sd_count == 0
        assert db.get(models.Video, vid).identity_revision == 0


def test_suppress_corrected_track(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    resp = ctx.client.post(
        _get_suppression_url(pid, vid),
        json={
            "scope": "corrected_track",
            "track_id": 1,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["identity_revision"] == 1
    # Track 1 has 4 detections (frames 0,1,2,4)
    assert body["frozen_detection_count"] == 4

    dets = ctx.client.get(
        f"/api/projects/{pid}/videos/{vid}/detections?start_frame=0&end_frame=4",
        headers=headers,
    ).json()
    assert all(det["display_track_id"] != 1 for det in dets["detections"])
    assert dets["total"] == 8

    tracks = ctx.client.get(
        f"/api/projects/{pid}/videos/{vid}/corrected-tracks",
        headers=headers,
    ).json()
    assert {track["display_track_id"] for track in tracks["items"]} == {2, 3}


def test_partial_suppression_updates_corrected_track_summary(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    dets = ctx.client.get(
        f"/api/projects/{pid}/videos/{vid}/detections?start_frame=0&end_frame=4",
        headers=headers,
    ).json()["detections"]
    frame_zero = next(
        det for det in dets if det["display_track_id"] == 1 and det["frame_index"] == 0
    )

    _seed_legacy_detection_suppression(ctx, vid, frame_zero["detection_id"])

    tracks = ctx.client.get(
        f"/api/projects/{pid}/videos/{vid}/corrected-tracks?current_frame=0",
        headers=headers,
    ).json()
    track = next(item for item in tracks["items"] if item["display_track_id"] == 1)
    assert track["detection_count"] == 3
    assert track["first_frame"] == 1
    assert track["last_frame"] == 4
    assert track["visible_in_current_frame"] is False


def test_repeated_track_suppression_rejected_without_revision(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    payload = {
        "scope": "corrected_track",
        "track_id": 1,
        "base_identity_revision": 0,
        "base_detection_import_revision": 1,
    }
    first = ctx.client.post(_get_suppression_url(pid, vid), json=payload, headers=headers)
    assert first.status_code == 200, first.text

    payload["base_identity_revision"] = 1
    repeated = ctx.client.post(_get_suppression_url(pid, vid), json=payload, headers=headers)
    assert repeated.status_code == 409
    assert repeated.json()["detail"] == "Track is already fully suppressed"

    with ctx.session_factory() as db:
        assert db.get(models.Video, vid).identity_revision == 1
        assert db.query(models.DetectionSuppression).filter(
            models.DetectionSuppression.video_id == vid,
            models.DetectionSuppression.reverted_suppression_id == None,
        ).count() == 1


def test_historical_single_detection_suppression_can_be_queried_and_reverted(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    detection_id = ctx.client.get(
        f"/api/projects/{pid}/videos/{vid}/detections?start_frame=0&end_frame=0",
        headers=headers,
    ).json()["detections"][0]["detection_id"]
    suppression_id = _seed_legacy_detection_suppression(ctx, vid, detection_id)
    active = ctx.client.get(_get_suppression_url(pid, vid), headers=headers)
    assert active.status_code == 200, active.text
    listed = next(item for item in active.json() if item["id"] == suppression_id)
    assert listed["scope"] == "detection"
    assert listed["result_identity_revision"] == 1
    assert listed["frozen_detection_count"] == 1
    assert listed["created_at"] is not None
    filtered = ctx.client.get(
        f"/api/projects/{pid}/videos/{vid}/detections?start_frame=0&end_frame=0",
        headers=headers,
    ).json()["detections"]
    assert detection_id not in {det["detection_id"] for det in filtered}

    reverted = ctx.client.post(
        f"{_get_suppression_url(pid, vid)}/{suppression_id}/revert",
        json={
            "base_identity_revision": 1,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert reverted.status_code == 200, reverted.text
    assert reverted.json()["freed_detection_count"] == 1
    assert suppression_id not in {
        item["id"]
        for item in ctx.client.get(_get_suppression_url(pid, vid), headers=headers).json()
    }
    with ctx.session_factory() as db:
        assert db.get(models.Video, vid).identity_revision == 2
        revert_record = db.query(models.DetectionSuppression).filter(
            models.DetectionSuppression.reverted_suppression_id == suppression_id
        ).one()
        assert revert_record.scope == "detection"


def test_suppress_revert(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    # Suppress
    resp = ctx.client.post(
        _get_suppression_url(pid, vid),
        json={
            "scope": "corrected_track",
            "track_id": 1,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    supp_id = resp.json()["suppression_id"]
    new_rev = resp.json()["identity_revision"]

    # Revert
    revert_resp = ctx.client.post(
        f"{_get_suppression_url(pid, vid)}/{supp_id}/revert",
        json={
            "base_identity_revision": new_rev,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert revert_resp.status_code == 200, revert_resp.text
    assert revert_resp.json()["identity_revision"] == new_rev + 1
    assert revert_resp.json()["freed_detection_count"] == 4

    # Suppression detection rows should be gone
    with ctx.session_factory() as db:
        sd_count = db.query(models.SuppressionDetection).filter(
            models.SuppressionDetection.suppression_id == supp_id
        ).count()
        assert sd_count == 0


def test_suppression_and_revert_revalidate_annotation_revisions(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    cat = _get_category(ctx, pid, "静止")
    created = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": cat["id"],
            "start_time": 0.0,
            "end_time": 0.16,
            "start_frame": 0,
            "end_frame": 4,
            "mouse_ids": [1],
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    ann_id = created.json()["id"]

    suppressed = ctx.client.post(
        _get_suppression_url(pid, vid),
        json={
            "scope": "corrected_track",
            "track_id": 1,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert suppressed.status_code == 200, suppressed.text
    after_suppress = next(
        ann for ann in ctx.client.get(_get_annotations_url(pid, vid), headers=headers).json()
        if ann["id"] == ann_id
    )
    assert after_suppress["mouse_id_status"] == "needs_mouse_ids"
    assert after_suppress["identity_revision"] == 1
    assert after_suppress["detection_import_revision"] == 1

    reverted = ctx.client.post(
        f"{_get_suppression_url(pid, vid)}/{suppressed.json()['suppression_id']}/revert",
        json={
            "base_identity_revision": 1,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert reverted.status_code == 200, reverted.text
    after_revert = next(
        ann for ann in ctx.client.get(_get_annotations_url(pid, vid), headers=headers).json()
        if ann["id"] == ann_id
    )
    assert after_revert["mouse_id_status"] == "valid"
    assert after_revert["identity_revision"] == 2
    assert after_revert["detection_import_revision"] == 1


def test_active_suppression_list_removes_reverted_track_suppression(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    created = ctx.client.post(
        _get_suppression_url(pid, vid),
        json={
            "scope": "corrected_track",
            "track_id": 2,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    suppression_id = created.json()["suppression_id"]
    listed = ctx.client.get(_get_suppression_url(pid, vid), headers=headers)
    assert listed.status_code == 200, listed.text
    item = next(item for item in listed.json() if item["id"] == suppression_id)
    assert item["scope"] == "corrected_track"
    assert item["result_identity_revision"] == 1
    assert item["created_at"] is not None
    assert item["frozen_detection_count"] == 4

    reverted = ctx.client.post(
        f"{_get_suppression_url(pid, vid)}/{suppression_id}/revert",
        json={
            "base_identity_revision": 1,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert reverted.status_code == 200, reverted.text
    assert suppression_id not in {
        item["id"]
        for item in ctx.client.get(_get_suppression_url(pid, vid), headers=headers).json()
    }


def test_old_import_suppression_is_not_listed_or_revertible(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    suppressed = ctx.client.post(
        _get_suppression_url(pid, vid),
        json={
            "scope": "corrected_track",
            "track_id": 1,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert suppressed.status_code == 200, suppressed.text
    suppression_id = suppressed.json()["suppression_id"]

    replacement = ctx.client.post(
        f"/api/projects/{pid}/videos/{vid}/detection-imports",
        files={
            "tracks_file": ("tracks_v2.jsonl", make_tracks_jsonl().encode()),
            "metadata_file": ("metadata.json", make_metadata_json().encode()),
        },
        params={"confirm": True},
        headers=headers,
    )
    assert replacement.status_code == 200, replacement.text
    assert replacement.json()["revision"] == 2

    active = ctx.client.get(_get_suppression_url(pid, vid), headers=headers)
    assert active.status_code == 200, active.text
    assert active.json() == []

    reverted = ctx.client.post(
        f"{_get_suppression_url(pid, vid)}/{suppression_id}/revert",
        json={
            "base_identity_revision": 0,
            "base_detection_import_revision": 2,
        },
        headers=headers,
    )
    assert reverted.status_code == 409, reverted.text
    assert reverted.json()["detail"] == (
        "Suppression does not belong to the active detection import"
    )
    with ctx.session_factory() as db:
        assert db.get(models.Video, vid).identity_revision == 0


def test_suppress_affects_annotation_coverage(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    cat = _get_category(ctx, pid, "静止")

    # Create annotation that uses track 1
    ann_resp = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": cat["id"],
            "start_time": 0.0,
            "end_time": 0.08,
            "start_frame": 0,
            "end_frame": 2,
            "mouse_ids": [1],
        },
        headers=headers,
    )
    assert ann_resp.status_code == 201, ann_resp.text
    ann_id = ann_resp.json()["id"]
    assert ann_resp.json()["mouse_id_status"] == "valid"

    # Suppress track 1 (all its 4 detections across all frames)
    resp = ctx.client.post(
        _get_suppression_url(pid, vid),
        json={
            "scope": "corrected_track",
            "track_id": 1,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    # Annotation should now be needs_mouse_ids (track 1 has no unsuppressed detections)
    ann = ctx.client.get(_get_annotations_url(pid, vid), headers=headers).json()
    found = next(a for a in ann if a["id"] == ann_id)
    assert found["mouse_id_status"] == "needs_mouse_ids"


def test_suppression_stale_revision_409(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    # First suppression
    resp1 = ctx.client.post(
        _get_suppression_url(pid, vid),
        json={
            "scope": "corrected_track",
            "track_id": 1,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp1.status_code == 200

    # Second with stale revision
    resp2 = ctx.client.post(
        _get_suppression_url(pid, vid),
        json={
            "scope": "corrected_track",
            "track_id": 2,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp2.status_code == 409


# ---------------------------------------------------------------------------
# 检测端点反映 identity_revision 变化
# ---------------------------------------------------------------------------

def test_detections_query_respects_identity_revision(ctx, login_headers):
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    # Before split: all 12 detections, 3 tracks
    dets_before = ctx.client.get(
        f"/api/projects/{pid}/videos/{vid}/detections?start_frame=0&end_frame=4",
        headers=headers,
    ).json()
    assert dets_before["total"] == 12

    # Split track 1 at frame 2
    ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "split",
            "track_ids": [1],
            "frame": 2,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )

    # After split: still 12 detections, now 4 tracks
    dets_after = ctx.client.get(
        f"/api/projects/{pid}/videos/{vid}/detections?start_frame=0&end_frame=4",
        headers=headers,
    ).json()
    assert dets_after["total"] == 12
    assert dets_after["detections"][0]["identity_revision"] == 1

    tracks = ctx.client.get(
        f"/api/projects/{pid}/videos/{vid}/corrected-tracks",
        headers=headers,
    ).json()
    assert tracks["total"] == 4


# ---------------------------------------------------------------------------
# New tests for Oracle fixes
# ---------------------------------------------------------------------------


def test_merge_commit_rejects_on_conflict_frames(ctx, login_headers):
    """Fix 1: Merge commit with conflict frames should be rejected with 400."""
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    # Both track 1 and track 2 appear at frame 0, 1 → conflict
    resp = ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "merge",
            "track_ids": [1, 2],
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert "conflict_frames" in body["detail"]

    # Verify nothing persisted
    with ctx.session_factory() as db:
        video = db.get(models.Video, vid)
        assert video.identity_revision == 0


def test_double_revert_rejected_409(ctx, login_headers):
    """Fix 4: Double revert of same identity edit should return 409."""
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    split_resp = ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "split",
            "track_ids": [1],
            "frame": 2,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert split_resp.status_code == 200

    with ctx.session_factory() as db:
        edit = db.query(models.IdentityEdit).filter(
            models.IdentityEdit.video_id == vid,
            models.IdentityEdit.operation == "split",
        ).first()
        edit_id = edit.id

    new_rev = split_resp.json()["identity_revision"]
    r1 = ctx.client.post(
        f"{_get_identity_url(pid, vid)}/{edit_id}/revert",
        json={
            "base_identity_revision": new_rev,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert r1.status_code == 200, r1.text

    new_rev2 = r1.json()["identity_revision"]
    r2 = ctx.client.post(
        f"{_get_identity_url(pid, vid)}/{edit_id}/revert",
        json={
            "base_identity_revision": new_rev2,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert r2.status_code == 409, r2.text
    assert "already been reverted" in r2.json()["detail"].lower()


def test_double_suppression_revert_rejected_409(ctx, login_headers):
    """Fix 4: Double revert of same suppression should return 409."""
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    resp = ctx.client.post(
        _get_suppression_url(pid, vid),
        json={
            "scope": "corrected_track",
            "track_id": 1,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 200
    supp_id = resp.json()["suppression_id"]
    new_rev = resp.json()["identity_revision"]

    r1 = ctx.client.post(
        f"{_get_suppression_url(pid, vid)}/{supp_id}/revert",
        json={
            "base_identity_revision": new_rev,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert r1.status_code == 200

    new_rev2 = r1.json()["identity_revision"]
    r2 = ctx.client.post(
        f"{_get_suppression_url(pid, vid)}/{supp_id}/revert",
        json={
            "base_identity_revision": new_rev2,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert r2.status_code == 409
    assert "already been reverted" in r2.json()["detail"].lower()


def test_split_uses_active_tracks_max_id(ctx, login_headers):
    """Fix 10: New split track uses max display_track_id from active tracks only."""
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    # Merge track 2 into 3 first (track 2 becomes inactive)
    ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "merge",
            "track_ids": [3, 2],
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )

    # Now split track 1 → new_track should get ID 4 (not reusing inactive 2)
    resp = ctx.client.post(
        f"{_get_identity_url(pid, vid)}/check",
        json={
            "operation": "split",
            "track_ids": [1],
            "frame": 2,
            "base_identity_revision": 1,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    # After merge: active tracks are 1, 3 → max is 3, new is 4
    assert resp.json()["new_display_track_id"] == 4


def test_split_excludes_suppressed_detections(ctx, login_headers):
    """Fix 11: Split before/after counts exclude suppressed detections."""
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    # First suppress a detection on track 1 at frame 0
    dets = ctx.client.get(
        f"/api/projects/{pid}/videos/{vid}/detections?start_frame=0&end_frame=0",
        headers=headers,
    ).json()
    det_id = dets["detections"][0]["detection_id"]

    _seed_legacy_detection_suppression(ctx, vid, det_id)

    # Now check split - before_count should exclude suppressed det
    check_resp = ctx.client.post(
        f"{_get_identity_url(pid, vid)}/check",
        json={
            "operation": "split",
            "track_ids": [1],
            "frame": 2,
            "base_identity_revision": 1,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert check_resp.status_code == 200, check_resp.text
    body = check_resp.json()
    # Track 1 frames: 0,1,2,4. Frame 0 suppressed → before should be 1 (frame 1 only)
    assert body["detections_before"] == 1


def test_split_annotation_boundary_end_frame(ctx, login_headers):
    """Fix 6: Annotations with end_frame >= split frame (even entirely after) get needs_mouse_ids."""
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    cat = _get_category(ctx, pid, "静止")

    # Annotation entirely after frame 2, using track 1
    ann1 = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": cat["id"],
            "start_time": 0.12,
            "end_time": 0.20,
            "start_frame": 3,
            "end_frame": 4,
            "mouse_ids": [1],
        },
        headers=headers,
    )
    assert ann1.status_code == 201, ann1.text

    # Annotation entirely before frame 2, using track 1
    ann2 = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": cat["id"],
            "start_time": 0.0,
            "end_time": 0.04,
            "start_frame": 0,
            "end_frame": 1,
            "mouse_ids": [1],
        },
        headers=headers,
    )
    assert ann2.status_code == 201, ann2.text

    # Split track 1 at frame 2
    split_resp = ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "split",
            "track_ids": [1],
            "frame": 2,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert split_resp.status_code == 200, split_resp.text
    needs_ids = split_resp.json()["needs_mouse_ids_annotation_ids"]

    # Annotation entirely after F (end_frame >= F) should be needs_mouse_ids
    assert ann1.json()["id"] in needs_ids

    # Annotation entirely before F (end_frame < F) should stay valid
    assert ann2.json()["id"] not in needs_ids

    # Verify in DB
    anns = ctx.client.get(_get_annotations_url(pid, vid), headers=headers).json()
    a1 = next(a for a in anns if a["id"] == ann1.json()["id"])
    assert a1["mouse_id_status"] == "needs_mouse_ids"
    a2 = next(a for a in anns if a["id"] == ann2.json()["id"])
    assert a2["mouse_id_status"] == "valid"


def test_revert_split_restores_annotation_snapshots(ctx, login_headers):
    """Fix 3: Split revert restores original annotation mouse_ids and mouse_id_status."""
    headers, pid, vid = _setup_complete_import(ctx, login_headers)
    cat = _get_category(ctx, pid, "静止")

    ann_resp = ctx.client.post(
        _get_annotations_url(pid, vid),
        json={
            "category_id": cat["id"],
            "start_time": 0.0,
            "end_time": 4.0,
            "start_frame": 0,
            "end_frame": 4,
            "mouse_ids": [1],
        },
        headers=headers,
    )
    assert ann_resp.status_code == 201, ann_resp.text
    ann_id = ann_resp.json()["id"]
    assert ann_resp.json()["mouse_id_status"] == "valid"

    split_resp = ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "split",
            "track_ids": [1],
            "frame": 2,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert split_resp.status_code == 200
    new_rev = split_resp.json()["identity_revision"]
    assert ann_id in split_resp.json()["needs_mouse_ids_annotation_ids"]

    with ctx.session_factory() as db:
        edit = db.query(models.IdentityEdit).filter(
            models.IdentityEdit.video_id == vid,
            models.IdentityEdit.operation == "split",
        ).first()
        edit_id = edit.id

    revert_resp = ctx.client.post(
        f"{_get_identity_url(pid, vid)}/{edit_id}/revert",
        json={
            "base_identity_revision": new_rev,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert revert_resp.status_code == 200

    anns = ctx.client.get(_get_annotations_url(pid, vid), headers=headers).json()
    found = next(a for a in anns if a["id"] == ann_id)
    assert found["mouse_ids"] == [1]
    assert found["mouse_id_status"] == "valid"


def test_consecutive_revert_after_new_edit(ctx, login_headers):
    """Revert old edit after a new edit on top: should correctly restore state."""
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    # Split track 1 at frame 2
    r1 = ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "split",
            "track_ids": [1],
            "frame": 2,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert r1.status_code == 200
    rev1 = r1.json()["identity_revision"]

    with ctx.session_factory() as db:
        edit1 = db.query(models.IdentityEdit).filter(
            models.IdentityEdit.video_id == vid,
            models.IdentityEdit.operation == "split",
            models.IdentityEdit.result_identity_revision == 1,
        ).first()
        edit1_id = edit1.id

    # Split track 2 at frame 2 (second edit, on top of first)
    r2 = ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "split",
            "track_ids": [2],
            "frame": 2,
            "base_identity_revision": rev1,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert r2.status_code == 200
    rev2 = r2.json()["identity_revision"]

    # Revert the FIRST edit (edit1) - should work with current base revision
    revert_resp = ctx.client.post(
        f"{_get_identity_url(pid, vid)}/{edit1_id}/revert",
        json={
            "base_identity_revision": rev2,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    # Revert should succeed - it materializes from current revision
    assert revert_resp.status_code == 200

    # Track 1 should be whole again (re-merged from track 4)
    with ctx.session_factory() as db:
        imp = db.query(models.DetectionImport).filter(
            models.DetectionImport.video_id == vid, models.DetectionImport.active == True
        ).first()
        active_tracks = db.query(models.CorrectedTrack).filter(
            models.CorrectedTrack.detection_import_id == imp.id,
            models.CorrectedTrack.active == True,
        ).all()
        display_ids = {t.display_track_id for t in active_tracks}
        assert 1 in display_ids


def test_commit_split_validates_track_ids(ctx, login_headers):
    """Fix 7: Commit path validates split input (exactly one track_id)."""
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    resp = ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "split",
            "track_ids": [1, 2],  # too many
            "frame": 2,
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 400


def test_commit_merge_validates_track_count(ctx, login_headers):
    """Fix 7: Commit path validates merge input (at least two track_ids)."""
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    resp = ctx.client.post(
        _get_identity_url(pid, vid),
        json={
            "operation": "merge",
            "track_ids": [1],  # only one
            "base_identity_revision": 0,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 400


def test_split_check_excludes_suppressed_detections(ctx, login_headers):
    """Fix 11: Check endpoint also excludes suppressed from before/after counts."""
    headers, pid, vid = _setup_complete_import(ctx, login_headers)

    dets = ctx.client.get(
        f"/api/projects/{pid}/videos/{vid}/detections?start_frame=0&end_frame=0",
        headers=headers,
    ).json()
    det_id = dets["detections"][0]["detection_id"]

    _seed_legacy_detection_suppression(ctx, vid, det_id)

    check_resp = ctx.client.post(
        f"{_get_identity_url(pid, vid)}/check",
        json={
            "operation": "split",
            "track_ids": [1],
            "frame": 2,
            "base_identity_revision": 1,
            "base_detection_import_revision": 1,
        },
        headers=headers,
    )
    assert check_resp.status_code == 200, check_resp.text
    assert check_resp.json()["detections_before"] == 1
