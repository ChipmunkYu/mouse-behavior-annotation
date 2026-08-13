"""Phase 2 sparse draft identity/suppression cutover acceptance tests."""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from sqlalchemy import event

from app import draft_detection_edits, models
from app.track_ids import TRACK_ID_UPPER_BOUND

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
    "keypoint_names": ["nose"],
    "skeleton_edges": [],
}


def _line(frame: int, detections: list[dict]) -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "video_id": "test-mouse-video",
            "frame_index": frame,
            "timestamp_sec": frame / 25.0,
            "detection_count": len(detections),
            "detections": detections,
        }
    )


def _det(track_id: int) -> dict:
    return {
        "track_id": track_id,
        "box_xyxy_px": [10.0, 20.0, 30.0, 40.0],
        "detection_confidence": 0.9,
        "class_id": 0,
        "keypoints": [{"x_px": 20.0, "y_px": 30.0, "confidence": 0.9}],
    }


def make_tracks_jsonl() -> str:
    return "\n".join(
        [
            _line(0, [_det(1), _det(2)]),
            _line(1, [_det(1), _det(2), _det(3)]),
            _line(2, [_det(1), _det(3)]),
            _line(3, [_det(2), _det(3)]),
            _line(4, [_det(1), _det(2), _det(3)]),
        ]
    ) + "\n"


def make_mergeable_jsonl() -> str:
    return "\n".join(
        [
            _line(0, [_det(1)]),
            _line(1, [_det(1)]),
            _line(2, [_det(3)]),
            _line(3, [_det(2)]),
            _line(4, [_det(2)]),
        ]
    ) + "\n"


def _setup(ctx, login_headers, *, mergeable: bool = False) -> tuple[dict, int, int]:
    headers = login_headers()
    project = ctx.client.post(
        "/api/projects", json={"name": "Phase2 sparse", "description": "test"}, headers=headers
    ).json()
    project_id = project["id"]
    batch = ctx.client.post(
        f"/api/projects/{project_id}/video-import-batches", headers=headers
    ).json()
    batch_id = batch["id"]
    tracks = make_mergeable_jsonl() if mergeable else make_tracks_jsonl()
    for role, filename, content in (
        ("video", "clip.mp4", b"FAKE"),
        ("tracks", "tracks.jsonl", tracks.encode()),
        ("metadata", "metadata.json", json.dumps(SAMPLE_METADATA).encode()),
    ):
        response = ctx.client.put(
            f"/api/projects/{project_id}/video-import-batches/{batch_id}/files/{role}",
            files={"file": (filename, content)},
            headers=headers,
        )
        assert response.status_code == 200, response.text
    completed = ctx.client.post(
        f"/api/projects/{project_id}/video-import-batches/{batch_id}/complete", headers=headers
    )
    assert completed.status_code == 200, completed.text
    return headers, project_id, completed.json()["video_id"]


def _identity_url(project_id: int, video_id: int) -> str:
    return f"/api/projects/{project_id}/videos/{video_id}/identity-edits"


def _suppression_url(project_id: int, video_id: int) -> str:
    return f"/api/projects/{project_id}/videos/{video_id}/detection-suppressions"


def _active_import(db, video_id: int):
    return db.query(models.DetectionImport).filter_by(video_id=video_id, active=True).one()


def _split_payload(version: int = 0) -> dict:
    return {
        "operation": "split",
        "track_ids": [1],
        "frame": 2,
        "base_identity_revision": version,
        "base_detection_import_revision": 1,
    }


def _suppress_payload(track_id: int = 1, version: int = 0) -> dict:
    return {
        "scope": "corrected_track",
        "track_id": track_id,
        "base_identity_revision": version,
        "base_detection_import_revision": 1,
    }


def test_raw_baseline_and_effective_read_have_no_legacy_rows(ctx, login_headers):
    headers, project_id, video_id = _setup(ctx, login_headers)
    detections = ctx.client.get(
        f"/api/projects/{project_id}/videos/{video_id}/detections?start_frame=0&end_frame=4",
        headers=headers,
    ).json()
    assert detections["total"] == 12
    assert all(row["raw_track_id"] == row["display_track_id"] for row in detections["detections"])
    tracks = ctx.client.get(
        f"/api/projects/{project_id}/videos/{video_id}/corrected-tracks", headers=headers
    ).json()
    assert {item["display_track_id"] for item in tracks["items"]} == {1, 2, 3}
    with ctx.session_factory() as db:
        assert db.query(models.CorrectedTrack).count() == 0
        assert db.query(models.CorrectedDetectionAssignment).count() == 0
        assert db.query(models.IdentityEdit).count() == 0
        assert db.query(models.DetectionSuppression).count() == 0
        assert db.query(models.SuppressionDetection).count() == 0


@pytest.mark.parametrize(("role", "expected"), [("admin", 200), ("annotator", 200), ("reviewer", 403), ("viewer", 403)])
def test_identity_edit_permissions(ctx, login_headers, role, expected):
    _headers, project_id, video_id = _setup(ctx, login_headers)
    username = f"phase2_{role}"
    user_id = ctx.create_user(username)
    ctx.add_member(project_id, user_id, role=role)
    role_headers = login_headers(username=username, password="pw123")
    response = ctx.client.post(
        f"{_identity_url(project_id, video_id)}/check",
        json=_split_payload(), headers=role_headers,
    )
    assert response.status_code == expected


@pytest.mark.parametrize("role", ["reviewer", "viewer"])
def test_all_identity_and_suppression_mutations_reject_readonly_roles(
    ctx, login_headers, role
):
    headers, project_id, video_id = _setup(ctx, login_headers)
    split = ctx.client.post(
        _identity_url(project_id, video_id), json=_split_payload(), headers=headers
    ).json()
    username = f"readonly_mutations_{role}"
    user_id = ctx.create_user(username)
    ctx.add_member(project_id, user_id, role=role)
    readonly = login_headers(username=username, password="pw123")
    calls = [
        ctx.client.post(_identity_url(project_id, video_id),
                        json=_split_payload(version=1), headers=readonly),
        ctx.client.post(
            f"{_identity_url(project_id, video_id)}/{split['edit_id']}/revert",
            json={"base_identity_revision": 1, "base_detection_import_revision": 1},
            headers=readonly,
        ),
        ctx.client.post(_suppression_url(project_id, video_id),
                        json=_suppress_payload(track_id=2, version=1), headers=readonly),
        ctx.client.post(
            f"{_suppression_url(project_id, video_id)}/{split['edit_id']}/revert",
            json={"base_identity_revision": 1, "base_detection_import_revision": 1},
            headers=readonly,
        ),
    ]
    assert [response.status_code for response in calls] == [403, 403, 403, 403]


def test_split_writes_only_sparse_affected_rows_and_history(ctx, login_headers):
    headers, project_id, video_id = _setup(ctx, login_headers)
    preview = ctx.client.post(
        f"{_identity_url(project_id, video_id)}/check", json=_split_payload(), headers=headers
    )
    assert preview.status_code == 200
    assert preview.json()["detections_before"] == 2
    assert preview.json()["detections_after"] == 2
    response = ctx.client.post(
        _identity_url(project_id, video_id), json=_split_payload(), headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["new_display_track_id"] == 4
    with ctx.session_factory() as db:
        detection_import = _active_import(db, video_id)
        assert (detection_import.edit_version, detection_import.next_display_track_id) == (1, 5)
        assert db.query(models.DetectionStateOverride).count() == 2
        edit = db.query(models.DraftIdentityEdit).one()
        assert edit.operation == "split"
        assert len(edit.changes) == 2
        assert db.query(models.CorrectedDetectionAssignment).count() == 0
        assert db.query(models.IdentityEdit).count() == 0
    history = ctx.client.get(
        f"{_identity_url(project_id, video_id)}/history?limit=10", headers=headers
    ).json()
    assert [item["operation"] for item in history] == ["split"]


def test_split_check_and_commit_report_the_same_affected_set(ctx, login_headers):
    headers, project_id, video_id = _setup(ctx, login_headers)
    checked = ctx.client.post(
        f"{_identity_url(project_id, video_id)}/check", json=_split_payload(), headers=headers
    )
    committed = ctx.client.post(
        _identity_url(project_id, video_id), json=_split_payload(), headers=headers
    )
    assert checked.status_code == committed.status_code == 200
    assert checked.json()["detections_after"] == committed.json()["affected_detection_count"]


@pytest.mark.parametrize(
    "payload",
    [
        {"operation": "split", "track_ids": [1], "frame": 99,
         "base_identity_revision": 0, "base_detection_import_revision": 1},
        {"operation": "merge", "track_ids": [1],
         "base_identity_revision": 0, "base_detection_import_revision": 1},
        {"operation": "merge", "track_ids": [1, 1],
         "base_identity_revision": 0, "base_detection_import_revision": 1},
    ],
)
def test_identity_input_boundaries_leave_no_partial_state(ctx, login_headers, payload):
    headers, project_id, video_id = _setup(ctx, login_headers)
    response = ctx.client.post(_identity_url(project_id, video_id), json=payload, headers=headers)
    assert response.status_code in (400, 422)
    with ctx.session_factory() as db:
        assert _active_import(db, video_id).edit_version == 0
        assert db.query(models.DraftIdentityEdit).count() == 0
        assert db.query(models.DetectionStateOverride).count() == 0


def test_stale_identity_revision_is_rejected_without_second_edit(ctx, login_headers):
    headers, project_id, video_id = _setup(ctx, login_headers)
    assert ctx.client.post(
        _identity_url(project_id, video_id), json=_split_payload(), headers=headers
    ).status_code == 200
    stale = ctx.client.post(
        _suppression_url(project_id, video_id), json=_suppress_payload(track_id=2), headers=headers
    )
    assert stale.status_code == 409
    with ctx.session_factory() as db:
        assert _active_import(db, video_id).edit_version == 1
        assert db.query(models.DraftIdentityEdit).count() == 1


def test_merge_writes_only_merged_track_rows(ctx, login_headers):
    headers, project_id, video_id = _setup(ctx, login_headers, mergeable=True)
    payload = {
        "operation": "merge",
        "track_ids": [1, 2],
        "base_identity_revision": 0,
        "base_detection_import_revision": 1,
    }
    response = ctx.client.post(_identity_url(project_id, video_id), json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["retained_display_track_id"] == 1
    with ctx.session_factory() as db:
        edit = db.query(models.DraftIdentityEdit).one()
        assert edit.operation == "merge"
        assert len(edit.changes) == 2
        assert {row.display_track_id for row in db.query(models.DetectionStateOverride)} == {1}
    tracks = ctx.client.get(
        f"/api/projects/{project_id}/videos/{video_id}/corrected-tracks", headers=headers
    ).json()
    assert {item["display_track_id"] for item in tracks["items"]} == {1, 3}


def test_merge_conflict_rejected_without_state(ctx, login_headers):
    headers, project_id, video_id = _setup(ctx, login_headers)
    payload = {
        "operation": "merge",
        "track_ids": [1, 2],
        "base_identity_revision": 0,
        "base_detection_import_revision": 1,
    }
    response = ctx.client.post(_identity_url(project_id, video_id), json=payload, headers=headers)
    assert response.status_code == 400
    with ctx.session_factory() as db:
        assert _active_import(db, video_id).edit_version == 0
        assert db.query(models.DraftIdentityEdit).count() == 0


def test_suppress_and_lifo_undo_delete_baseline_overrides(ctx, login_headers):
    headers, project_id, video_id = _setup(ctx, login_headers)
    suppressed = ctx.client.post(
        _suppression_url(project_id, video_id), json=_suppress_payload(), headers=headers
    )
    assert suppressed.status_code == 200, suppressed.text
    suppression_id = suppressed.json()["suppression_id"]
    assert suppressed.json()["frozen_detection_count"] == 4
    assert ctx.client.get(
        f"/api/projects/{project_id}/videos/{video_id}/detections?start_frame=0&end_frame=4",
        headers=headers,
    ).json()["total"] == 8
    reverted = ctx.client.post(
        f"{_suppression_url(project_id, video_id)}/{suppression_id}/revert",
        json={"base_identity_revision": 1, "base_detection_import_revision": 1},
        headers=headers,
    )
    assert reverted.status_code == 200, reverted.text
    with ctx.session_factory() as db:
        detection_import = _active_import(db, video_id)
        assert (detection_import.edit_version, detection_import.next_display_track_id) == (2, 4)
        assert db.query(models.DetectionStateOverride).count() == 0
        assert db.query(models.DraftIdentityEdit).count() == 0
        assert db.query(models.DetectionSuppression).count() == 0


def test_merge_lifo_round_trip_restores_annotation_and_effective_tracks(ctx, login_headers):
    headers, project_id, video_id = _setup(ctx, login_headers, mergeable=True)
    category = next(item for item in ctx.client.get(
        f"/api/projects/{project_id}/categories", headers=headers
    ).json() if item["mouse_count_min"] == 1)
    annotation = ctx.client.post(
        f"/api/projects/{project_id}/videos/{video_id}/annotations",
        json={"category_id": category["id"], "start_time": 0, "end_time": 0.16,
              "start_frame": 0, "end_frame": 4, "mouse_ids": [2]}, headers=headers,
    )
    assert annotation.status_code == 201, annotation.text
    merged = ctx.client.post(
        _identity_url(project_id, video_id),
        json={"operation": "merge", "track_ids": [1, 2],
              "base_identity_revision": 0, "base_detection_import_revision": 1},
        headers=headers,
    )
    assert merged.status_code == 200, merged.text
    assert ctx.client.get(
        f"/api/projects/{project_id}/videos/{video_id}/annotations", headers=headers
    ).json()[0]["mouse_ids"] == [1]
    undone = ctx.client.post(
        f"{_identity_url(project_id, video_id)}/{merged.json()['edit_id']}/revert",
        json={"base_identity_revision": 1, "base_detection_import_revision": 1},
        headers=headers,
    )
    assert undone.status_code == 200, undone.text
    assert ctx.client.get(
        f"/api/projects/{project_id}/videos/{video_id}/annotations", headers=headers
    ).json()[0]["mouse_ids"] == [2]
    tracks = ctx.client.get(
        f"/api/projects/{project_id}/videos/{video_id}/corrected-tracks", headers=headers
    ).json()
    assert {item["display_track_id"] for item in tracks["items"]} == {1, 2, 3}


def test_lifo_rejects_non_top_and_type_mismatch(ctx, login_headers):
    headers, project_id, video_id = _setup(ctx, login_headers)
    split = ctx.client.post(
        _identity_url(project_id, video_id), json=_split_payload(), headers=headers
    ).json()
    suppression = ctx.client.post(
        _suppression_url(project_id, video_id),
        json=_suppress_payload(track_id=2, version=1),
        headers=headers,
    ).json()
    non_top = ctx.client.post(
        f"{_identity_url(project_id, video_id)}/{split['edit_id']}/revert",
        json={"base_identity_revision": 2, "base_detection_import_revision": 1},
        headers=headers,
    )
    assert non_top.status_code == 409
    wrong_type = ctx.client.post(
        f"{_identity_url(project_id, video_id)}/{suppression['suppression_id']}/revert",
        json={"base_identity_revision": 2, "base_detection_import_revision": 1},
        headers=headers,
    )
    assert wrong_type.status_code == 409


def test_cursor_exhausted_has_no_side_effect(ctx, login_headers):
    headers, project_id, video_id = _setup(ctx, login_headers)
    with ctx.session_factory() as db:
        detection_import = _active_import(db, video_id)
        detection_import.next_display_track_id = TRACK_ID_UPPER_BOUND
        db.commit()
    response = ctx.client.post(
        _identity_url(project_id, video_id), json=_split_payload(), headers=headers
    )
    assert response.status_code == 409
    with ctx.session_factory() as db:
        detection_import = _active_import(db, video_id)
        assert detection_import.edit_version == 0
        assert detection_import.next_display_track_id == TRACK_ID_UPPER_BOUND
        assert db.query(models.DraftIdentityEdit).count() == 0
        assert db.query(models.DetectionStateOverride).count() == 0


def test_split_undo_does_not_reuse_allocated_track_id(ctx, login_headers):
    headers, project_id, video_id = _setup(ctx, login_headers)
    first = ctx.client.post(
        _identity_url(project_id, video_id), json=_split_payload(), headers=headers
    ).json()
    undone = ctx.client.post(
        f"{_identity_url(project_id, video_id)}/{first['edit_id']}/revert",
        json={"base_identity_revision": 1, "base_detection_import_revision": 1},
        headers=headers,
    )
    assert undone.status_code == 200, undone.text
    second = ctx.client.post(
        _identity_url(project_id, video_id), json=_split_payload(version=2), headers=headers
    )
    assert second.status_code == 200, second.text
    assert first["new_display_track_id"] == 4
    assert second.json()["new_display_track_id"] == 5
    with ctx.session_factory() as db:
        assert _active_import(db, video_id).next_display_track_id == 6


def test_two_sessions_same_base_only_first_cas_succeeds(ctx, login_headers):
    _headers, _project_id, video_id = _setup(ctx, login_headers)
    first = ctx.session_factory()
    second = ctx.session_factory()
    try:
        imp1, imp2 = _active_import(first, video_id), _active_import(second, video_id)
        video1, video2 = first.get(models.Video, video_id), second.get(models.Video, video_id)
        draft_detection_edits.commit_suppress(
            first, imp1, video1, track_id=1, expected_version=0, operator_id=1
        )
        with pytest.raises(HTTPException) as exc_info:
            draft_detection_edits.commit_suppress(
                second, imp2, video2, track_id=2, expected_version=0, operator_id=1
            )
        assert exc_info.value.status_code == 409
        second.rollback()
    finally:
        first.close()
        second.close()
    with ctx.session_factory() as db:
        assert _active_import(db, video_id).edit_version == 1
        assert db.query(models.DraftIdentityEdit).count() == 1
        assert db.query(models.DetectionStateOverride).count() == 4


@pytest.mark.parametrize("operation", ["split", "merge", "suppress"])
def test_mid_operation_exception_rolls_back_all_sparse_state(
    ctx, login_headers, monkeypatch, operation
):
    headers, project_id, video_id = _setup(
        ctx, login_headers, mergeable=operation == "merge"
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("injected sparse write failure")

    monkeypatch.setattr(draft_detection_edits, "_after_set_based_delta", fail)
    if operation == "split":
        call = lambda: ctx.client.post(
            _identity_url(project_id, video_id), json=_split_payload(), headers=headers
        )
    elif operation == "merge":
        call = lambda: ctx.client.post(
            _identity_url(project_id, video_id),
            json={
                "operation": "merge",
                "track_ids": [1, 2],
                "base_identity_revision": 0,
                "base_detection_import_revision": 1,
            },
            headers=headers,
        )
    else:
        call = lambda: ctx.client.post(
            _suppression_url(project_id, video_id), json=_suppress_payload(), headers=headers
        )
    with pytest.raises(RuntimeError, match="injected"):
        call()
    with ctx.session_factory() as db:
        detection_import = _active_import(db, video_id)
        assert detection_import.edit_version == 0
        assert detection_import.next_display_track_id == 4
        assert db.query(models.DraftIdentityEdit).count() == 0
        assert db.query(models.DraftDetectionChange).count() == 0
        assert db.query(models.DetectionStateOverride).count() == 0


def test_direct_service_exception_rolls_back_and_session_remains_usable(
    ctx, login_headers, monkeypatch
):
    _headers, _project_id, video_id = _setup(ctx, login_headers)
    monkeypatch.setattr(
        draft_detection_edits,
        "_after_set_based_delta",
        lambda: (_ for _ in ()).throw(HTTPException(status_code=400, detail="injected")),
    )
    with ctx.session_factory() as db:
        detection_import = _active_import(db, video_id)
        video = db.get(models.Video, video_id)
        with pytest.raises(HTTPException):
            draft_detection_edits.commit_suppress(
                db, detection_import, video, track_id=1, expected_version=0, operator_id=1
            )
        assert _active_import(db, video_id).edit_version == 0
        assert db.query(models.DraftIdentityEdit).count() == 0
        assert db.query(models.DetectionStateOverride).count() == 0


def test_submitted_lock_blocks_all_edits(ctx, login_headers):
    headers, project_id, video_id = _setup(ctx, login_headers)
    with ctx.session_factory() as db:
        video = db.get(models.Video, video_id)
        video.workflow_status = "submitted"
        db.commit()
    response = ctx.client.post(
        _identity_url(project_id, video_id), json=_split_payload(), headers=headers
    )
    assert response.status_code == 409
    assert "withdraw" in response.json()["detail"].lower()


def test_future_submitted_submission_also_locks_draft(ctx, login_headers):
    headers, project_id, video_id = _setup(ctx, login_headers)
    with ctx.session_factory() as db:
        detection_import = _active_import(db, video_id)
        snapshot = models.DetectionSnapshot(
            detection_import_id=detection_import.id,
            source_edit_version=0,
            raw_detection_count=12,
            override_count=0,
            schema_version=1,
            fps=25.0,
            width=1280,
            height=720,
            frame_count=5,
            keypoint_names=["nose"],
            skeleton_edges=[],
        )
        db.add(snapshot)
        db.flush()
        db.add(
            models.Submission(
                video_id=video_id,
                detection_snapshot_id=snapshot.id,
                attempt_no=1,
                source_annotation_version=1,
                source_media_revision=1,
                source_video_filename="clip.mp4",
                source_storage_key="videos/clip.mp4",
                source_video_sha256="a" * 64,
                status="submitted",
                submitted_at=models.utcnow(),
            )
        )
        db.commit()
    response = ctx.client.post(
        _suppression_url(project_id, video_id), json=_suppress_payload(), headers=headers
    )
    assert response.status_code == 409
    assert "withdraw" in response.json()["detail"].lower()


@pytest.mark.parametrize("workflow", ["approved", "rejected"])
def test_approved_rejected_projection_returns_to_draft(ctx, login_headers, workflow):
    headers, project_id, video_id = _setup(ctx, login_headers)
    with ctx.session_factory() as db:
        video = db.get(models.Video, video_id)
        video.workflow_status = workflow
        db.commit()
    response = ctx.client.post(
        _suppression_url(project_id, video_id), json=_suppress_payload(), headers=headers
    )
    assert response.status_code == 200, response.text
    with ctx.session_factory() as db:
        assert db.get(models.Video, video_id).workflow_status == "draft"


def test_annotation_revalidation_and_revision_projection(ctx, login_headers):
    headers, project_id, video_id = _setup(ctx, login_headers)
    categories = ctx.client.get(
        f"/api/projects/{project_id}/categories", headers=headers
    ).json()
    category = next(item for item in categories if item["mouse_count_min"] == 1)
    created = ctx.client.post(
        f"/api/projects/{project_id}/videos/{video_id}/annotations",
        json={
            "category_id": category["id"],
            "start_time": 0.0,
            "end_time": 0.16,
            "start_frame": 0,
            "end_frame": 4,
            "mouse_ids": [1],
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    suppressed = ctx.client.post(
        _suppression_url(project_id, video_id), json=_suppress_payload(), headers=headers
    )
    assert suppressed.status_code == 200
    with ctx.session_factory() as db:
        annotation = db.get(models.Annotation, created.json()["id"])
        assert annotation.mouse_id_status == "needs_mouse_ids"
        assert annotation.identity_revision == 1
        assert db.get(models.Video, video_id).identity_revision == 1


def test_replace_clears_old_draft_and_initializes_new_import(ctx, login_headers):
    headers, project_id, video_id = _setup(ctx, login_headers)
    ctx.client.post(
        _identity_url(project_id, video_id), json=_split_payload(), headers=headers
    )
    with ctx.session_factory() as db:
        old_id = _active_import(db, video_id).id
    replacement = ctx.client.post(
        f"/api/projects/{project_id}/videos/{video_id}/detection-imports",
        files={
            "tracks_file": ("tracks.jsonl", make_tracks_jsonl().encode()),
            "metadata_file": ("metadata.json", json.dumps(SAMPLE_METADATA).encode()),
        },
        params={"confirm": True},
        headers=headers,
    )
    assert replacement.status_code == 200, replacement.text
    with ctx.session_factory() as db:
        new_import = _active_import(db, video_id)
        assert (new_import.revision, new_import.edit_version, new_import.next_display_track_id) == (
            2,
            0,
            4,
        )
        assert db.query(models.DetectionStateOverride).filter_by(
            detection_import_id=old_id
        ).count() == 0
        assert db.query(models.DraftIdentityEdit).filter_by(
            detection_import_id=old_id
        ).count() == 0


def test_replacement_rejects_track_id_that_exists_only_in_old_import(ctx, login_headers):
    headers, project_id, video_id = _setup(ctx, login_headers)
    old_only_track = ctx.client.post(
        _identity_url(project_id, video_id), json=_split_payload(), headers=headers
    ).json()["new_display_track_id"]
    replacement_tracks = "\n".join(_line(frame, [_det(7)]) for frame in range(5)) + "\n"
    replacement = ctx.client.post(
        f"/api/projects/{project_id}/videos/{video_id}/detection-imports",
        files={
            "tracks_file": ("tracks.jsonl", replacement_tracks.encode()),
            "metadata_file": ("metadata.json", json.dumps(SAMPLE_METADATA).encode()),
        },
        params={"confirm": True}, headers=headers,
    )
    assert replacement.status_code == 200, replacement.text
    cross_import = ctx.client.post(
        _identity_url(project_id, video_id),
        json={"operation": "split", "track_ids": [old_only_track], "frame": 2,
              "base_identity_revision": 0, "base_detection_import_revision": 2},
        headers=headers,
    )
    assert cross_import.status_code == 400
    with ctx.session_factory() as db:
        current = _active_import(db, video_id)
        assert (current.revision, current.edit_version) == (2, 0)
        assert db.query(models.DraftIdentityEdit).count() == 0
        assert db.query(models.DetectionStateOverride).count() == 0


def test_long_track_set_based_suppression_has_constant_statement_count(ctx, login_headers):
    headers, project_id, video_id = _setup(ctx, login_headers)
    with ctx.session_factory() as db:
        detection_import = _active_import(db, video_id)
        db.bulk_save_objects([
            models.RawDetection(
                detection_import_id=detection_import.id,
                frame_index=index,
                frame_detection_index=99,
                raw_track_id=9,
            )
            for index in range(1200)
        ])
        db.commit()
    statements = 0
    def count_statement(*_args):
        nonlocal statements
        statements += 1
    event.listen(ctx.session_factory.kw["bind"], "before_cursor_execute", count_statement)
    try:
        response = ctx.client.post(
            _suppression_url(project_id, video_id),
            json=_suppress_payload(track_id=9),
            headers=headers,
        )
    finally:
        event.remove(ctx.session_factory.kw["bind"], "before_cursor_execute", count_statement)
    assert response.status_code == 200, response.text
    assert response.json()["frozen_detection_count"] == 1200
    assert statements < 30
    with ctx.session_factory() as db:
        assert db.query(models.DraftDetectionChange).count() == 1200
        assert db.query(models.DetectionStateOverride).filter_by(suppressed=True).count() == 1200
