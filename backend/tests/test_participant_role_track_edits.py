"""Real SQLite/API coverage for role-authoritative draft track edits."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app import models
from tests.conftest import auth_headers
from tests.test_identity_edits import (SAMPLE_METADATA, _det, _line, make_mergeable_jsonl,
                                       make_tracks_jsonl)


def _setup(ctx, *, mergeable=False):
    headers = auth_headers(ctx.client)
    project = ctx.client.post("/api/projects", json={"name": "role track edits"}, headers=headers).json()
    scheme = ctx.client.put(
        f"/api/projects/{project['id']}/category-scheme",
        json={"expected_version": 0, "categories": [
            {"name": "追逐", "group": "社交", "sort_order": 0,
             "participant_mode": "role_based", "role_definitions": [
                 {"name": "追逐者", "min_count": 0, "max_count": 2, "role_sort_order": 0},
                 {"name": "被追逐者", "min_count": 1, "max_count": 2, "role_sort_order": 1},
             ]},
            {"name": "跟随", "group": "社交", "sort_order": 1,
             "participant_mode": "role_based", "role_definitions": [
                 {"name": "前方", "min_count": 1, "max_count": 2, "role_sort_order": 0},
                 {"name": "后方", "min_count": 0, "max_count": 2, "role_sort_order": 1},
             ]},
        ]}, headers=headers,
    ).json()
    ctx.client.post(f"/api/projects/{project['id']}/category-scheme/lock",
                    json={"expected_version": 1}, headers=headers)
    batch = ctx.client.post(f"/api/projects/{project['id']}/video-import-batches", headers=headers).json()
    tracks = make_mergeable_jsonl() if mergeable else make_tracks_jsonl()
    for role, filename, content in (
        ("video", "clip.mp4", b"FAKE"),
        ("tracks", "tracks.jsonl", tracks.encode()),
        ("metadata", "metadata.json", json.dumps(SAMPLE_METADATA).encode()),
    ):
        response = ctx.client.put(
            f"/api/projects/{project['id']}/video-import-batches/{batch['id']}/files/{role}",
            files={"file": (filename, content)}, headers=headers,
        )
        assert response.status_code == 200, response.text
    completed = ctx.client.post(
        f"/api/projects/{project['id']}/video-import-batches/{batch['id']}/complete", headers=headers
    )
    assert completed.status_code == 200, completed.text
    return headers, project["id"], completed.json()["video_id"], scheme["categories"]


def _annotation(ctx, setup, roles, *, category_index=0, start_frame=0, end_frame=4):
    headers, project_id, video_id, categories = setup
    response = ctx.client.post(
        f"/api/projects/{project_id}/videos/{video_id}/annotations",
        json={"category_id": categories[category_index]["id"],
              "start_time": start_frame / 25, "end_time": (end_frame + 1) / 25,
              "start_frame": start_frame, "end_frame": end_frame,
              "participant_roles": roles}, headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _keys(setup, category_index=0):
    return [item["key"] for item in setup[3][category_index]["role_definitions"]]


def _identity(setup):
    return f"/api/projects/{setup[1]}/videos/{setup[2]}/identity-edits"


def _assert_no_edit_state(ctx, video_id):
    with ctx.session_factory() as db:
        imp = db.query(models.DetectionImport).filter_by(video_id=video_id, active=True).one()
        assert imp.edit_version == 0
        assert db.query(models.DraftIdentityEdit).count() == 0
        assert db.query(models.DraftDetectionChange).count() == 0
        assert db.query(models.DetectionStateOverride).count() == 0


def test_detection_replacement_requires_annotation_refetch(ctx):
    setup = _setup(ctx)
    headers, project_id, video_id, _categories = setup
    first, second = _keys(setup)
    annotation = _annotation(ctx, setup, {first: [], second: [1]})

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
    assert replacement.json()["annotations_must_be_refetched"] is True
    assert "Refetch annotations" in replacement.json()["message"]

    annotations = ctx.client.get(
        f"/api/projects/{project_id}/videos/{video_id}/annotations", headers=headers
    )
    assert annotations.status_code == 200, annotations.text
    current = next(item for item in annotations.json() if item["id"] == annotation["id"])
    assert current["mouse_id_status"] == "valid"
    assert current["participant_status"] == "valid"
    assert current["detection_import_revision"] == replacement.json()["revision"]


def test_split_preview_commit_conflict_safe_interval_and_commit_recheck(ctx):
    setup = _setup(ctx)
    headers, _project, video_id, _categories = setup
    first, second = _keys(setup)
    ann = _annotation(ctx, setup, {first: [], second: [1]}, start_frame=2)
    payload = {"operation": "split", "track_ids": [1], "frame": 2,
               "base_identity_revision": 0, "base_detection_import_revision": 1}
    preview = ctx.client.post(f"{_identity(setup)}/check", json=payload, headers=headers)
    assert preview.status_code == 200
    assert preview.json()["message"] == "Split conflicts with participant role assignments"
    expected = {"annotation_id": ann["id"], "start_time": 0.08, "end_time": 0.2,
                "start_frame": 2, "end_frame": 4, "role_key": second,
                "role_name": "被追逐者", "track_id": 1}
    assert preview.json()["conflicts"] == [expected]
    commit = ctx.client.post(_identity(setup), json=payload, headers=headers)
    assert commit.status_code == 409 and commit.json()["detail"] == {
        "message": "Split conflicts with participant role assignments", "conflicts": [expected]}
    _assert_no_edit_state(ctx, video_id)

    # An interval ending before [split_frame,+infinity) is safe.
    ctx.client.delete(
        f"/api/projects/{setup[1]}/videos/{video_id}/annotations/{ann['id']}", headers=headers)
    _annotation(ctx, setup, {first: [], second: [1]}, start_frame=0, end_frame=1)
    assert ctx.client.post(f"{_identity(setup)}/check", json=payload, headers=headers).json().get("conflicts") is None
    # Preview is advisory: a newly-created overlapping role reference must be seen at commit.
    _annotation(ctx, setup, {first: [], second: [1]}, start_frame=2)
    assert ctx.client.post(_identity(setup), json=payload, headers=headers).status_code == 409
    _assert_no_edit_state(ctx, video_id)


def test_suppress_any_role_reference_has_complete_conflict_and_no_state(ctx):
    setup = _setup(ctx)
    headers, project_id, video_id, _ = setup
    first, second = _keys(setup)
    ann = _annotation(ctx, setup, {first: [3], second: [1]})
    response = ctx.client.post(
        f"/api/projects/{project_id}/videos/{video_id}/detection-suppressions",
        json={"scope": "corrected_track", "track_id": 3,
              "base_identity_revision": 0, "base_detection_import_revision": 1}, headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"]["conflicts"] == [{
        "annotation_id": ann["id"], "start_time": 0.0, "end_time": 0.2,
        "start_frame": 0, "end_frame": 4, "role_key": first,
        "role_name": "追逐者", "track_id": 3}]
    _assert_no_edit_state(ctx, video_id)


def test_malformed_persisted_roles_do_not_hide_known_track_references(ctx):
    setup = _setup(ctx)
    headers, project_id, video_id, _ = setup
    first, second = _keys(setup)
    ann = _annotation(ctx, setup, {first: [], second: [1]})
    with ctx.session_factory() as db:
        db.execute(text("UPDATE annotations SET participant_roles=:roles WHERE id=:id"), {
            "roles": json.dumps({first: [], second: [1, {"bad": True}]}), "id": ann["id"]})
        db.commit()
    response = ctx.client.post(
        f"/api/projects/{project_id}/videos/{video_id}/detection-suppressions",
        json={"scope": "corrected_track", "track_id": 1,
              "base_identity_revision": 0, "base_detection_import_revision": 1}, headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"]["conflicts"][0]["track_id"] == 1
    assert response.json()["detail"]["conflicts"][0]["role_key"] == second
    _assert_no_edit_state(ctx, video_id)


def test_merge_unique_role_hit_updates_and_undo_round_trip(ctx):
    setup = _setup(ctx, mergeable=True)
    headers, project_id, video_id, _ = setup
    first, second = _keys(setup)
    ann = _annotation(ctx, setup, {first: [], second: [2]})
    payload = {"operation": "merge", "track_ids": [1, 2],
               "base_identity_revision": 0, "base_detection_import_revision": 1}
    merged = ctx.client.post(_identity(setup), json=payload, headers=headers)
    assert merged.status_code == 200, merged.text
    current = ctx.client.get(
        f"/api/projects/{project_id}/videos/{video_id}/annotations", headers=headers).json()[0]
    assert current["participant_roles"] == {first: [], second: [1]}
    assert current["mouse_ids"] == [1]
    assert current["participant_status"] == current["mouse_id_status"] == "valid"
    with ctx.session_factory() as db:
        edit = db.get(models.DraftIdentityEdit, merged.json()["edit_id"])
        image = edit.params["annotation_images"][str(ann["id"])]
        for side in ("before", "after"):
            assert set(image[side]) == {"category_id", "start_time", "end_time", "start_frame",
                                        "end_frame", "participant_roles", "mouse_ids"}
    undone = ctx.client.post(
        f"{_identity(setup)}/{merged.json()['edit_id']}/revert",
        json={"base_identity_revision": 1, "base_detection_import_revision": 1}, headers=headers)
    assert undone.status_code == 200, undone.text
    restored = ctx.client.get(
        f"/api/projects/{project_id}/videos/{video_id}/annotations", headers=headers).json()[0]
    assert restored["participant_roles"] == {first: [], second: [2]}
    assert restored["mouse_ids"] == [2]


@pytest.mark.parametrize("roles", ["same", "cross"])
def test_merge_multiple_role_hits_rejected_with_complete_details(ctx, roles):
    setup = _setup(ctx, mergeable=True)
    headers, _project, video_id, _ = setup
    first, second = _keys(setup)
    assignments = ({first: [], second: [1, 2]} if roles == "same"
                   else {first: [1], second: [2]})
    ann = _annotation(ctx, setup, assignments)
    payload = {"operation": "merge", "track_ids": [1, 2],
               "base_identity_revision": 0, "base_detection_import_revision": 1}
    preview = ctx.client.post(f"{_identity(setup)}/check", json=payload, headers=headers)
    commit = ctx.client.post(_identity(setup), json=payload, headers=headers)
    assert preview.status_code == 200 and commit.status_code == 409
    conflicts = commit.json()["detail"]["conflicts"]
    assert conflicts == preview.json()["conflicts"]
    assert {(item["annotation_id"], item["role_key"], item["track_id"])
            for item in conflicts} == {(ann["id"], first if roles == "cross" else second, 1),
                                       (ann["id"], second, 2)}
    assert all(set(item) == {"annotation_id", "start_time", "end_time", "start_frame",
                             "end_frame", "role_key", "role_name", "track_id"}
               for item in conflicts)
    _assert_no_edit_state(ctx, video_id)


@pytest.mark.parametrize("change", ["interval", "roles", "category"])
def test_merge_undo_rejects_changed_after_image_without_advancing_version(ctx, change):
    setup = _setup(ctx, mergeable=True)
    headers, project_id, video_id, categories = setup
    first, second = _keys(setup)
    ann = _annotation(ctx, setup, {first: [], second: [2]})
    merged = ctx.client.post(_identity(setup), json={
        "operation": "merge", "track_ids": [1, 2], "base_identity_revision": 0,
        "base_detection_import_revision": 1}, headers=headers).json()
    patch = ({"start_time": 0.04, "start_frame": 1} if change == "interval" else
             {"participant_roles": {first: [3], second: [1]}} if change == "roles" else
             {"category_id": categories[1]["id"],
              "participant_roles": {_keys(setup, 1)[0]: [1], _keys(setup, 1)[1]: []}})
    url = f"/api/projects/{project_id}/videos/{video_id}/annotations/{ann['id']}"
    changed = ctx.client.patch(url, json=patch, headers=headers)
    assert changed.status_code == 200, changed.text
    before = changed.json()
    rejected = ctx.client.post(f"{_identity(setup)}/{merged['edit_id']}/revert",
        json={"base_identity_revision": 1, "base_detection_import_revision": 1}, headers=headers)
    assert rejected.status_code == 409
    after = next(item for item in ctx.client.get(
        f"/api/projects/{project_id}/videos/{video_id}/annotations", headers=headers
    ).json() if item["id"] == ann["id"])
    for field in ("category_id", "start_time", "start_frame", "participant_roles", "mouse_ids"):
        assert after[field] == before[field]
    with ctx.session_factory() as db:
        imp = db.query(models.DetectionImport).filter_by(video_id=video_id, active=True).one()
        assert imp.edit_version == 1
        assert db.get(models.Video, video_id).identity_revision == 1
        assert db.get(models.DraftIdentityEdit, merged["edit_id"]) is not None


def test_detection_replacement_preserves_role_authority_and_recomputes_status(ctx):
    setup = _setup(ctx)
    headers, project_id, video_id, _ = setup
    first, second = _keys(setup)
    ann = _annotation(ctx, setup, {first: [], second: [1]})
    endpoint = f"/api/projects/{project_id}/videos/{video_id}/detection-imports"
    same = ctx.client.post(endpoint, files={
        "tracks_file": ("tracks.jsonl", make_tracks_jsonl().encode()),
        "metadata_file": ("metadata.json", json.dumps(SAMPLE_METADATA).encode()),
    }, params={"confirm": True}, headers=headers)
    assert same.status_code == 200, same.text
    current = ctx.client.get(
        f"/api/projects/{project_id}/videos/{video_id}/annotations", headers=headers).json()[0]
    assert current["participant_roles"] == {first: [], second: [1]}
    assert current["mouse_ids"] == [1]
    assert current["participant_status"] == current["mouse_id_status"] == "valid"

    only_three = "\n".join(_line(frame, [_det(3)]) for frame in range(5)) + "\n"
    gone = ctx.client.post(endpoint, files={
        "tracks_file": ("tracks.jsonl", only_three.encode()),
        "metadata_file": ("metadata.json", json.dumps(SAMPLE_METADATA).encode()),
    }, params={"confirm": True}, headers=headers)
    assert gone.status_code == 200, gone.text
    current = ctx.client.get(
        f"/api/projects/{project_id}/videos/{video_id}/annotations", headers=headers).json()[0]
    assert current["id"] == ann["id"]
    assert current["participant_roles"] == {first: [], second: [1]}
    assert current["mouse_ids"] == [1]
    assert current["participant_status"] == "valid"
    assert current["mouse_id_status"] == "needs_mouse_ids"


def test_role_submission_snapshots_are_complete_immutable_and_review_authoritative(ctx):
    setup = _setup(ctx)
    headers, project_id, video_id, categories = setup
    first, second = _keys(setup)
    ann = _annotation(ctx, setup, {first: [1], second: []})
    submit_url = f"/api/projects/{project_id}/videos/{video_id}/submit"
    incomplete = ctx.client.post(submit_url, headers=headers)
    assert incomplete.status_code == 400
    completed = ctx.client.patch(
        f"/api/projects/{project_id}/videos/{video_id}/annotations/{ann['id']}",
        json={"participant_roles": {first: [1], second: [2]}}, headers=headers)
    assert completed.status_code == 200, completed.text
    assert ctx.client.post(submit_url, headers=headers).status_code == 200

    with ctx.session_factory() as db:
        copy = db.query(models.SubmissionAnnotation).one()
        assert copy.category_group == "社交"
        assert copy.category_participant_mode == "role_based"
        assert copy.role_definitions_snapshot == categories[0]["role_definitions"]
        assert copy.participant_roles_snapshot == {first: [1], second: [2]}
        assert copy.mouse_ids == [1, 2]
        copy_id = copy.id
        for statement in (
            "UPDATE submission_annotations SET category_group='tampered' WHERE id=:id",
            "DELETE FROM submission_annotations WHERE id=:id",
        ):
            with pytest.raises(DBAPIError):
                db.execute(text(statement), {"id": copy_id})
                db.commit()
            db.rollback()
        # A live-draft projection mutation cannot alter the frozen review authority.
        db.execute(text(
            "UPDATE annotations SET participant_roles=:roles,mouse_ids=:ids WHERE id=:id"
        ), {"roles": json.dumps({first: [], second: [3]}), "ids": "[3]", "id": ann["id"]})
        db.commit()

    queue = ctx.client.get(f"/api/projects/{project_id}/reviews/queue", headers=headers)
    assert queue.status_code == 200
    frozen = queue.json()[0]["submission_annotations"][0]
    assert frozen["category_group"] == "社交"
    assert frozen["category_participant_mode"] == "role_based"
    assert frozen["role_definitions"] == categories[0]["role_definitions"]
    assert frozen["participant_roles"] == {first: [1], second: [2]}
    assert frozen["mouse_ids"] == [1, 2]
    decision = ctx.client.post(
        f"/api/projects/{project_id}/videos/{video_id}/review",
        json={"result": "rejected", "comment": "snapshot"}, headers=headers)
    assert decision.status_code == 200, decision.text
    assert decision.json()["submission_annotations"][0] == frozen
    history = ctx.client.get(
        f"/api/projects/{project_id}/videos/{video_id}/reviews", headers=headers).json()
    assert history[0]["submission_annotations"][0] == frozen


def test_clips_library_reads_role_summary_from_submission_snapshot(ctx):
    setup = _setup(ctx)
    headers, project_id, video_id, categories = setup
    first, second = _keys(setup)
    ann = _annotation(ctx, setup, {first: [3], second: [1]})
    assert ctx.client.post(
        f"/api/projects/{project_id}/videos/{video_id}/submit", headers=headers).status_code == 200
    approved = ctx.client.post(
        f"/api/projects/{project_id}/videos/{video_id}/review",
        json={"result": "approved", "comment": "snapshot"}, headers=headers)
    assert approved.status_code == 200, approved.text
    with ctx.session_factory() as db:
        # Change the live projection after freezing; clips must not follow it.
        db.execute(text(
            "UPDATE annotations SET participant_roles=:roles,mouse_ids=:ids WHERE id=:id"
        ), {"roles": json.dumps({first: [], second: [2]}), "ids": "[2]", "id": ann["id"]})
        db.commit()
    response = ctx.client.get(f"/api/projects/{project_id}/clips", headers=headers)
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["category_group"] == "社交"
    assert item["category_participant_mode"] == "role_based"
    assert item["role_definitions"] == categories[0]["role_definitions"]
    assert item["participant_roles"] == {first: [3], second: [1]}
    assert item["mouse_ids"] == [1, 3]
