"""验收（批次 3）：提交 / 审核队列 / 审核历史 / 审核裁决。

覆盖契约：
- submit：owner/admin/annotator 可提交；reviewer 403；至少 1 条标注；draft/rejected 可提交，
  submitted/approved 拒绝；提交后 annotations 全部 pending、reviewer null。
- queue：owner/admin/reviewer 可见；只返回 submitted。
- reviews（历史）：项目成员可读；跨项目 404；非成员 403。
- review：owner/admin/reviewer 可裁决；仅 submitted；追加当前修订 Review；
  approved/rejected 同步 Video 与 annotations 审核字段。
"""
from __future__ import annotations

import json

from app.models import Annotation, DetectionImport, Review, Video


_SAMPLE_METADATA = {
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


def _make_frame_line(frame_index: int, detections: list[dict]) -> str:
    return json.dumps({
        "schema_version": "1.0",
        "video_id": "test-mouse-video",
        "frame_index": frame_index,
        "timestamp_sec": frame_index / 25.0,
        "detection_count": len(detections),
        "detections": detections,
    })


def _make_tracks_jsonl() -> str:
    lines = [
        _make_frame_line(0, [_det(1), _det(2)]),
        _make_frame_line(1, [_det(1), _det(2), _det(3)]),
        _make_frame_line(2, [_det(1), _det(3)]),
        _make_frame_line(3, [_det(2), _det(3)]),
        _make_frame_line(4, [_det(1), _det(2), _det(3)]),
    ]
    return "\n".join(lines) + "\n"


def _make_metadata_json() -> str:
    return json.dumps(_SAMPLE_METADATA)


def _setup_video_with_import(ctx, login_headers):
    """Helper: creates project + detection import video. Returns (headers, project, categories, video)."""
    headers = login_headers()
    project = ctx.client.post(
        "/api/projects", json={"name": "Review测试项目", "description": "test"}, headers=headers
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
        files={"file": ("tracks.jsonl", _make_tracks_jsonl().encode())},
        headers=headers,
    )
    ctx.client.put(
        f"/api/projects/{pid}/video-import-batches/{bid}/files/metadata",
        files={"file": ("metadata.json", _make_metadata_json().encode())},
        headers=headers,
    )
    resp = ctx.client.post(
        f"/api/projects/{pid}/video-import-batches/{bid}/complete",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    vid = resp.json()["video_id"]

    cats = ctx.client.get(f"/api/projects/{pid}/categories", headers=headers).json()
    return headers, project, cats, {"id": vid}


def _annotate(ctx, headers, project, video, category_id, start_time=0.0):
    resp = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations",
        json={
            "category_id": category_id,
            "start_time": start_time,
            "end_time": start_time + 0.04,
            "start_frame": int(start_time * 25),
            "end_frame": int((start_time + 0.04) * 25),
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _annotate_with_mouse(ctx, headers, project, video, category_id, start_time=0.0, mouse_ids=None):
    """Create annotation with optional mouse_ids for review tests that need valid mouse_ids."""
    payload = {
        "category_id": category_id,
        "start_time": start_time,
        "end_time": start_time + 0.04,
        "start_frame": int(start_time * 25),
        "end_frame": int((start_time + 0.04) * 25),
    }
    if mouse_ids is not None:
        payload["mouse_ids"] = mouse_ids
    resp = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _submit(ctx, headers, project, video):
    return ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/submit", headers=headers
    )


def _review(ctx, headers, project, video, result, comment="ok"):
    return ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/review",
        json={"result": result, "comment": comment},
        headers=headers,
    )


def _queue(ctx, headers, project):
    return ctx.client.get(f"/api/projects/{project['id']}/reviews/queue", headers=headers)


def _history(ctx, headers, project, video):
    return ctx.client.get(
        f"/api/projects/{project['id']}/videos/{video['id']}/reviews", headers=headers
    )


def _add_reviewer(ctx, project_id, username="reviewer1"):
    user_id = ctx.create_user(username)
    ctx.add_member(project_id, user_id, role="reviewer")
    return user_id


def _video_state(ctx, video_id) -> dict:
    with ctx.session_factory() as db:
        v = db.get(Video, video_id)
        return {
            "workflow_status": v.workflow_status,
            "annotation_revision": v.annotation_revision,
            "submitted_at": v.submitted_at,
            "approved_at": v.approved_at,
            "approved_by": v.approved_by,
        }


def _annotation_review_fields(ctx, video_id) -> list[tuple[str, int | None]]:
    with ctx.session_factory() as db:
        rows = (
            db.query(Annotation)
            .filter(Annotation.video_id == video_id)
            .order_by(Annotation.id)
            .all()
        )
        return [(a.review_status, a.reviewer_id) for a in rows]


# ---------- submit ----------


def test_submit_requires_at_least_one_annotation(ctx, login_headers):
    headers, project, _categories, video = _setup_video_with_import(ctx, login_headers)
    resp = _submit(ctx, headers, project, video)
    assert resp.status_code == 400
    assert "at least one annotation" in resp.json()["detail"].lower()


def test_submit_requires_detection_import(ctx):
    """提交需要检测导入：无检测导入的视频应返回 400。"""
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"], setup["project"], setup["categories"], setup["video"]
    )
    _annotate(ctx, headers, project, video, categories[0]["id"])
    resp = _submit(ctx, headers, project, video)
    assert resp.status_code == 400
    assert "no detection import" in resp.json()["detail"].lower()


def test_submit_success_sets_submitted_and_resets_annotations(ctx, login_headers):
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)
    cat = next((c for c in categories if c["mouse_count_min"] == 1 and c["mouse_count_max"] == 1), categories[0])
    _annotate_with_mouse(ctx, headers, project, video, cat["id"], mouse_ids=[1])

    resp = _submit(ctx, headers, project, video)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workflow_status"] == "submitted"
    assert body["submitted_at"] is not None
    assert body["approved_at"] is None
    assert body["approved_by"] is None

    state = _video_state(ctx, video["id"])
    assert state["workflow_status"] == "submitted"
    assert state["annotation_revision"] == 2
    assert state["submitted_at"] is not None
    assert _annotation_review_fields(ctx, video["id"]) == [("pending", None)]


def test_submit_revalidates_valid_stale_annotation_and_advances_revisions(ctx, login_headers):
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)
    cat = next(c for c in categories if c["mouse_count_min"] == 1 and c["mouse_count_max"] == 1)
    ann = _annotate_with_mouse(ctx, headers, project, video, cat["id"], mouse_ids=[1])

    with ctx.session_factory() as db:
        stored_video = db.get(Video, video["id"])
        stored_video.identity_revision = 3
        db.query(DetectionImport).filter_by(video_id=video["id"], active=True).one().edit_version = 3
        stored_ann = db.get(Annotation, ann["id"])
        stored_ann.mouse_id_status = "valid"
        stored_ann.detection_import_revision = 1
        stored_ann.identity_revision = 0
        db.commit()

    resp = _submit(ctx, headers, project, video)
    assert resp.status_code == 200, resp.text
    assert resp.json()["workflow_status"] == "submitted"

    with ctx.session_factory() as db:
        stored_video = db.get(Video, video["id"])
        stored_ann = db.get(Annotation, ann["id"])
        assert stored_video.workflow_status == "submitted"
        assert stored_ann.mouse_id_status == "valid"
        assert stored_ann.detection_import_revision == stored_video.detection_import_revision == 1
        assert stored_ann.identity_revision == stored_video.identity_revision == 3


def test_submit_does_not_change_invalid_stale_annotation(ctx, login_headers):
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)
    cat = next(c for c in categories if c["mouse_count_min"] == 1 and c["mouse_count_max"] == 1)
    ann = _annotate_with_mouse(ctx, headers, project, video, cat["id"], mouse_ids=[1])

    with ctx.session_factory() as db:
        stored_video = db.get(Video, video["id"])
        stored_video.identity_revision = 3
        stored_ann = db.get(Annotation, ann["id"])
        stored_ann.mouse_id_status = "valid"
        stored_ann.detection_import_revision = 1
        stored_ann.identity_revision = 0
        db.commit()

    resp = _submit(ctx, headers, project, video)
    assert resp.status_code == 400
    assert "projection is stale" in resp.json()["detail"]

    with ctx.session_factory() as db:
        stored_video = db.get(Video, video["id"])
        stored_ann = db.get(Annotation, ann["id"])
        assert stored_video.workflow_status == "draft"
        assert stored_ann.mouse_id_status == "valid"
        assert stored_ann.detection_import_revision == 1
        assert stored_ann.identity_revision == 0


def test_submit_invalid_mixed_annotations_changes_neither(ctx, login_headers):
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)
    cat = next(c for c in categories if c["mouse_count_min"] == 1 and c["mouse_count_max"] == 1)
    valid_ann = _annotate_with_mouse(
        ctx, headers, project, video, cat["id"], mouse_ids=[1]
    )
    invalid_ann = _annotate_with_mouse(
        ctx, headers, project, video, cat["id"], start_time=0.04, mouse_ids=[2]
    )

    with ctx.session_factory() as db:
        stored_video = db.get(Video, video["id"])
        stored_video.identity_revision = 3
        db.query(DetectionImport).filter_by(video_id=video["id"], active=True).one().edit_version = 3
        first = db.get(Annotation, valid_ann["id"])
        first.mouse_id_status = "valid"
        first.detection_import_revision = 0
        first.identity_revision = 1
        second = db.get(Annotation, invalid_ann["id"])
        second.mouse_ids = [99]
        second.mouse_id_status = "needs_mouse_ids"
        second.detection_import_revision = 1
        second.identity_revision = 2
        db.commit()

    resp = _submit(ctx, headers, project, video)
    assert resp.status_code == 400
    assert resp.json()["detail"]["invalid_annotations"] == [
        {
            "annotation_id": invalid_ann["id"],
            "reason": "Track ID 99 is not an active corrected track",
        }
    ]

    with ctx.session_factory() as db:
        first = db.get(Annotation, valid_ann["id"])
        second = db.get(Annotation, invalid_ann["id"])
        assert (
            first.mouse_id_status,
            first.detection_import_revision,
            first.identity_revision,
        ) == ("valid", 0, 1)
        assert (
            second.mouse_id_status,
            second.detection_import_revision,
            second.identity_revision,
        ) == ("needs_mouse_ids", 1, 2)


def test_submit_rejects_needs_mouse_ids(ctx, login_headers):
    """有检测导入但标注缺少 mouse_ids 时提交应被拒绝。"""
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)
    _annotate(ctx, headers, project, video, categories[0]["id"])
    resp = _submit(ctx, headers, project, video)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "1 annotation(s) still need valid mouse_ids before submission"


def test_submit_roles(ctx, login_headers):
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)
    cat = next((c for c in categories if c["mouse_count_min"] == 1 and c["mouse_count_max"] == 1), categories[0])
    _annotate_with_mouse(ctx, headers, project, video, cat["id"], mouse_ids=[1])

    reviewer_id = _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")
    assert _submit(ctx, reviewer_headers, project, video).status_code == 403

    outsider_id = ctx.create_user("outsider")
    outsider_headers = login_headers(username="outsider", password="pw123")
    assert _submit(ctx, outsider_headers, project, video).status_code == 403

    annotator_id = ctx.create_user("annot1")
    ctx.add_member(project["id"], annotator_id, role="annotator")
    annotator_headers = login_headers(username="annot1", password="pw123")
    assert _submit(ctx, annotator_headers, project, video).status_code == 200

    _review(ctx, reviewer_headers, project, video, "rejected")
    admin_id = ctx.create_user("admin1")
    ctx.add_member(project["id"], admin_id, role="admin")
    admin_headers = login_headers(username="admin1", password="pw123")
    assert _submit(ctx, admin_headers, project, video).status_code == 200


def test_submit_state_gate_draft_rejected_ok_submitted_approved_rejected(ctx, login_headers):
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)
    cat = next((c for c in categories if c["mouse_count_min"] == 1 and c["mouse_count_max"] == 1), categories[0])
    _annotate_with_mouse(ctx, headers, project, video, cat["id"], mouse_ids=[1])
    _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")

    assert _submit(ctx, headers, project, video).status_code == 200
    assert _submit(ctx, headers, project, video).status_code == 409

    assert _review(ctx, reviewer_headers, project, video, "rejected").status_code == 200
    assert _video_state(ctx, video["id"])["workflow_status"] == "rejected"
    assert _submit(ctx, headers, project, video).status_code == 200
    assert _submit(ctx, headers, project, video).status_code == 409

    assert _review(ctx, reviewer_headers, project, video, "approved").status_code == 200
    assert _video_state(ctx, video["id"])["workflow_status"] == "approved"
    assert _submit(ctx, headers, project, video).status_code == 400


def test_submit_resubmit_resets_annotation_review_fields(ctx, login_headers):
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)
    cat = next((c for c in categories if c["mouse_count_min"] == 1 and c["mouse_count_max"] == 1), categories[0])
    ann = _annotate_with_mouse(ctx, headers, project, video, cat["id"], mouse_ids=[1])
    _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")

    assert _submit(ctx, headers, project, video).status_code == 200
    assert _review(ctx, reviewer_headers, project, video, "rejected").status_code == 200
    with ctx.session_factory() as db:
        a = db.get(Annotation, ann["id"])
        assert a.review_status == "rejected"
        assert a.reviewer_id is not None

    assert _submit(ctx, headers, project, video).status_code == 200
    assert _annotation_review_fields(ctx, video["id"]) == [("pending", None)]


def test_submit_cross_project_video_404(ctx, login_headers):
    headers, project, _categories, video = _setup_video_with_import(ctx, login_headers)
    other = ctx.client.post("/api/projects", json={"name": "别的项目"}, headers=headers).json()
    resp = _submit(ctx, headers, other, video)
    assert resp.status_code == 404


# ---------- queue ----------


def test_queue_roles_and_submitted_only(ctx, login_headers):
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)
    cat = next((c for c in categories if c["mouse_count_min"] == 1 and c["mouse_count_max"] == 1), categories[0])
    _annotate_with_mouse(ctx, headers, project, video, cat["id"], mouse_ids=[1])

    # Create a second video in the same project, without detection import
    video2 = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "s2.mp4"},
        headers=headers,
    ).json()

    assert _submit(ctx, headers, project, video).status_code == 200

    ctx.create_user("outsider")
    outsider_headers = login_headers(username="outsider", password="pw123")
    assert _queue(ctx, outsider_headers, project).status_code == 403
    annot_id = ctx.create_user("annot2")
    ctx.add_member(project["id"], annot_id, role="annotator")
    annot_headers = login_headers(username="annot2", password="pw123")
    assert _queue(ctx, annot_headers, project).status_code == 403

    _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")
    queue = _queue(ctx, headers, project)
    assert queue.status_code == 200
    ids = [v["id"] for v in queue.json()]
    assert ids == [video["id"]]

    assert [v["id"] for v in _queue(ctx, reviewer_headers, project).json()] == ids

    admin_id = ctx.create_user("admin3")
    ctx.add_member(project["id"], admin_id, role="admin")
    admin_headers = login_headers(username="admin3", password="pw123")
    assert [v["id"] for v in _queue(ctx, admin_headers, project).json()] == ids


def test_queue_excludes_rejected_and_approved(ctx, login_headers):
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)
    cat = next((c for c in categories if c["mouse_count_min"] == 1 and c["mouse_count_max"] == 1), categories[0])
    _annotate_with_mouse(ctx, headers, project, video, cat["id"], mouse_ids=[1])
    _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")

    assert _submit(ctx, headers, project, video).status_code == 200
    assert _queue(ctx, headers, project).json()

    assert _review(ctx, reviewer_headers, project, video, "rejected").status_code == 200
    assert _queue(ctx, headers, project).json() == []

    assert _submit(ctx, headers, project, video).status_code == 200
    assert _review(ctx, reviewer_headers, project, video, "approved").status_code == 200
    assert _queue(ctx, headers, project).json() == []


def test_queue_cross_project_isolation(ctx, login_headers):
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)

    # Create another project with detection import and submit a video there
    other_headers = login_headers()
    other_project = ctx.client.post(
        "/api/projects", json={"name": "隔离项目"}, headers=other_headers
    ).json()
    other_pid = other_project["id"]

    obatch = ctx.client.post(f"/api/projects/{other_pid}/video-import-batches", headers=other_headers)
    assert obatch.status_code == 201
    obid = obatch.json()["id"]

    ctx.client.put(
        f"/api/projects/{other_pid}/video-import-batches/{obid}/files/video",
        files={"file": ("o.mp4", b"FAKE-MP4")}, headers=other_headers,
    )
    ctx.client.put(
        f"/api/projects/{other_pid}/video-import-batches/{obid}/files/tracks",
        files={"file": ("tracks.jsonl", _make_tracks_jsonl().encode())}, headers=other_headers,
    )
    ctx.client.put(
        f"/api/projects/{other_pid}/video-import-batches/{obid}/files/metadata",
        files={"file": ("metadata.json", _make_metadata_json().encode())}, headers=other_headers,
    )
    oresp = ctx.client.post(f"/api/projects/{other_pid}/video-import-batches/{obid}/complete", headers=other_headers)
    assert oresp.status_code == 200
    other_vid = oresp.json()["video_id"]

    other_cats = ctx.client.get(f"/api/projects/{other_pid}/categories", headers=other_headers).json()
    ocat = next((c for c in other_cats if c["mouse_count_min"] == 1 and c["mouse_count_max"] == 1), other_cats[0])
    other_video = {"id": other_vid}
    _annotate_with_mouse(ctx, other_headers, other_project, other_video, ocat["id"], mouse_ids=[1])
    assert _submit(ctx, other_headers, other_project, other_video).status_code == 200

    # Project A's queue should not see project B's submitted video
    assert _queue(ctx, headers, project).json() == []


# ---------- history ----------


def test_review_history_member_readable_and_order(ctx, login_headers):
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)
    cat = next((c for c in categories if c["mouse_count_min"] == 1 and c["mouse_count_max"] == 1), categories[0])
    _annotate_with_mouse(ctx, headers, project, video, cat["id"], mouse_ids=[1])
    _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")

    assert _history(ctx, headers, project, video).json() == []

    assert _submit(ctx, headers, project, video).status_code == 200
    assert _review(ctx, reviewer_headers, project, video, "rejected", comment="改一下").status_code == 200

    history = _history(ctx, headers, project, video)
    assert history.status_code == 200
    rows = history.json()
    assert len(rows) == 1
    assert rows[0]["result"] == "rejected"
    assert rows[0]["comment"] == "改一下"
    assert rows[0]["annotation_revision"] == 2
    assert rows[0]["reviewer"] == "reviewer1"

    annot_id = ctx.create_user("annot4")
    ctx.add_member(project["id"], annot_id, role="annotator")
    annot_headers = login_headers(username="annot4", password="pw123")
    assert _history(ctx, annot_headers, project, video).status_code == 200

    ctx.create_user("outsider")
    outsider_headers = login_headers(username="outsider", password="pw123")
    assert _history(ctx, outsider_headers, project, video).status_code == 403
    other = ctx.client.post("/api/projects", json={"name": "另一个"}, headers=headers).json()
    assert _history(ctx, headers, other, video).status_code == 404


def test_review_history_accumulates_across_revisions(ctx, login_headers):
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)
    cat = next((c for c in categories if c["mouse_count_min"] == 1 and c["mouse_count_max"] == 1), categories[0])
    ann = _annotate_with_mouse(ctx, headers, project, video, cat["id"], mouse_ids=[1])
    _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")

    assert _submit(ctx, headers, project, video).status_code == 200
    assert _review(ctx, reviewer_headers, project, video, "rejected").status_code == 200

    patch = ctx.client.patch(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations/{ann['id']}",
        json={"end_time": 0.12, "end_frame": 3},
        headers=headers,
    )
    assert patch.status_code == 200
    assert _video_state(ctx, video["id"])["annotation_revision"] == 3

    assert _submit(ctx, headers, project, video).status_code == 200
    assert _review(ctx, reviewer_headers, project, video, "approved").status_code == 200

    rows = _history(ctx, headers, project, video).json()
    assert len(rows) == 2
    assert [r["annotation_revision"] for r in rows] == [2, 3]
    assert [r["result"] for r in rows] == ["rejected", "approved"]


# ---------- review ----------


def test_review_approve_syncs_video_and_annotations(ctx, login_headers):
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)
    cat = next((c for c in categories if c["mouse_count_min"] == 1 and c["mouse_count_max"] == 1), categories[0])
    ann = _annotate_with_mouse(ctx, headers, project, video, cat["id"], mouse_ids=[1])
    reviewer_id = _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")

    assert _submit(ctx, headers, project, video).status_code == 200
    resp = _review(ctx, reviewer_headers, project, video, "approved", comment="通过")
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "approved"
    assert body["comment"] == "通过"
    assert body["annotation_revision"] == 2
    assert body["reviewer"] == "reviewer1"

    state = _video_state(ctx, video["id"])
    assert state["workflow_status"] == "approved"
    assert state["approved_at"] is not None
    assert state["approved_by"] == reviewer_id
    assert state["submitted_at"] is not None

    with ctx.session_factory() as db:
        a = db.get(Annotation, ann["id"])
        assert a.review_status == "approved"
        assert a.reviewer_id == reviewer_id


def test_review_reject_syncs_video_and_annotations(ctx, login_headers):
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)
    cat = next((c for c in categories if c["mouse_count_min"] == 1 and c["mouse_count_max"] == 1), categories[0])
    ann = _annotate_with_mouse(ctx, headers, project, video, cat["id"], mouse_ids=[1])
    reviewer_id = _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")

    assert _submit(ctx, headers, project, video).status_code == 200
    assert _review(ctx, reviewer_headers, project, video, "rejected", comment="要改").status_code == 200

    state = _video_state(ctx, video["id"])
    assert state["workflow_status"] == "rejected"
    assert state["approved_at"] is None
    assert state["approved_by"] is None
    assert state["submitted_at"] is not None

    with ctx.session_factory() as db:
        a = db.get(Annotation, ann["id"])
        assert a.review_status == "rejected"
        assert a.reviewer_id == reviewer_id


def test_review_roles_and_state_gate(ctx, login_headers):
    headers, project, categories, video = _setup_video_with_import(ctx, login_headers)
    cat = next((c for c in categories if c["mouse_count_min"] == 1 and c["mouse_count_max"] == 1), categories[0])
    _annotate_with_mouse(ctx, headers, project, video, cat["id"], mouse_ids=[1])
    _add_reviewer(ctx, project["id"])
    reviewer_headers = login_headers(username="reviewer1", password="pw123")

    ctx.create_user("outsider")
    outsider_headers = login_headers(username="outsider", password="pw123")
    assert _review(ctx, outsider_headers, project, video, "approved").status_code == 403

    annot_id = ctx.create_user("annot5")
    ctx.add_member(project["id"], annot_id, role="annotator")
    annot_headers = login_headers(username="annot5", password="pw123")
    assert _review(ctx, annot_headers, project, video, "approved").status_code == 403

    assert _review(ctx, reviewer_headers, project, video, "approved").status_code == 400

    assert _submit(ctx, headers, project, video).status_code == 200
    assert _review(ctx, reviewer_headers, project, video, "approved").status_code == 200
    assert _review(ctx, reviewer_headers, project, video, "rejected").status_code == 400

    other = ctx.client.post("/api/projects", json={"name": "别的"}, headers=headers).json()
    assert _review(ctx, headers, other, video, "approved").status_code == 404
