import json

import pytest

from tests.conftest import auth_headers
from tests.test_detection_imports import SAMPLE_METADATA, make_tracks_jsonl


def _role_project(ctx):
    headers = auth_headers(ctx.client)
    project = ctx.client.post("/api/projects", json={"name": "roles"}, headers=headers).json()
    payload = {"expected_version": 0, "categories": [{
        "name": "追逐", "group": "社交", "sort_order": 0,
        "participant_mode": "role_based", "role_definitions": [
            {"name": "追逐者", "min_count": 1, "max_count": 1, "role_sort_order": 0},
            {"name": "被追逐者", "min_count": 1, "max_count": 2, "role_sort_order": 1},
        ],
    }, {
        "name": "行走", "group": "个体", "sort_order": 1,
        "participant_mode": "unordered", "role_definitions": [],
        "mouse_count_min": 1, "mouse_count_max": 2,
    }]}
    scheme = ctx.client.put(
        f"/api/projects/{project['id']}/category-scheme", json=payload, headers=headers
    ).json()
    ctx.client.post(f"/api/projects/{project['id']}/category-scheme/lock",
                    json={"expected_version": 1}, headers=headers)
    video = ctx.client.post(f"/api/projects/{project['id']}/videos",
        json={"filename": "roles.mp4", "duration": 10, "fps": 25}, headers=headers).json()
    return headers, project, scheme["categories"], video


def _body(category_id, roles=None):
    result = {"category_id": category_id, "start_time": 0, "end_time": 2,
              "start_frame": 0, "end_frame": 49}
    if roles is not None:
        result["participant_roles"] = roles
    return result


def _install_detection_import(ctx, headers, project_id, video_id):
    response = ctx.client.post(
        f"/api/projects/{project_id}/videos/{video_id}/detection-imports",
        files={
            "tracks_file": ("tracks.jsonl", make_tracks_jsonl().encode()),
            "metadata_file": ("metadata.json", json.dumps(SAMPLE_METADATA).encode()),
        },
        params={"confirm": True},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_role_annotation_complete_incomplete_projection_and_unordered_regression(ctx):
    headers, project, categories, video = _role_project(ctx)
    role_category, unordered = categories
    first, second = [item["key"] for item in role_category["role_definitions"]]
    url = f"/api/projects/{project['id']}/videos/{video['id']}/annotations"
    draft = ctx.client.post(url, json=_body(role_category["id"], {first: [3], second: []}),
                            headers=headers)
    assert draft.status_code == 201, draft.text
    assert draft.json()["participant_status"] == "needs_participants"
    assert draft.json()["participant_roles"] == {first: [3], second: []}
    assert draft.json()["mouse_ids"] == [3]
    completed = ctx.client.patch(f"{url}/{draft.json()['id']}",
        json={"participant_roles": {first: [3], second: [8]}}, headers=headers)
    assert completed.status_code == 200
    assert completed.json()["participant_status"] == "valid"
    assert completed.json()["mouse_ids"] == [3, 8]
    plain = ctx.client.post(url, json=_body(unordered["id"]), headers=headers)
    assert plain.status_code == 201
    assert plain.json()["participant_roles"] == {} and plain.json()["participant_status"] == "valid"


def test_role_annotation_rejects_mouse_ids_and_structural_json_errors(ctx):
    headers, project, categories, video = _role_project(ctx)
    category = categories[0]
    first, second = [item["key"] for item in category["role_definitions"]]
    url = f"/api/projects/{project['id']}/videos/{video['id']}/annotations"
    both = _body(category["id"], {first: [1], second: [2]}); both["mouse_ids"] = [1, 2]
    assert ctx.client.post(url, json=both, headers=headers).status_code == 422
    invalid = [
        {first: [1]},
        {first: [1], second: [1]},
        {first: [1, 1], second: [2]},
        {first: [1, 2], second: [3]},
        {first: [True], second: [2]},
        {first: [1], second: [2], "unknown": []},
    ]
    for roles in invalid:
        assert ctx.client.post(url, json=_body(category["id"], roles), headers=headers).status_code == 422


@pytest.mark.parametrize(
    ("roles", "expected_track_status"),
    [([1, 2], "valid"), ([1, 99], "needs_mouse_ids")],
    ids=["valid-tracks", "invalid-track"],
)
def test_category_only_role_to_unordered_canonicalizes_and_revalidates_tracks(
    ctx, roles, expected_track_status
):
    headers, project, categories, video = _role_project(ctx)
    replacement = _install_detection_import(ctx, headers, project["id"], video["id"])
    role_category, unordered = categories
    first, second = [item["key"] for item in role_category["role_definitions"]]
    url = f"/api/projects/{project['id']}/videos/{video['id']}/annotations"
    created = ctx.client.post(
        url,
        json=_body(role_category["id"], {first: [roles[0]], second: [roles[1]]}),
        headers=headers,
    )
    assert created.status_code == 201, created.text

    updated = ctx.client.patch(
        f"{url}/{created.json()['id']}",
        json={"category_id": unordered["id"]},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["mouse_ids"] == roles
    assert body["participant_roles"] == {}
    assert body["participant_status"] == "valid"
    assert body["mouse_id_status"] == expected_track_status
    assert body["detection_import_revision"] == replacement["revision"]
    assert body["identity_revision"] == 0


def test_detection_replacement_preview_separates_unordered_and_role_based_actions(ctx):
    headers, project, categories, video = _role_project(ctx)
    _install_detection_import(ctx, headers, project["id"], video["id"])
    role_category, unordered = categories
    first, second = [item["key"] for item in role_category["role_definitions"]]
    url = f"/api/projects/{project['id']}/videos/{video['id']}/annotations"
    assert ctx.client.post(
        url, json=_body(role_category["id"], {first: [1], second: [2]}), headers=headers
    ).status_code == 201
    unordered_body = _body(unordered["id"])
    unordered_body["mouse_ids"] = [3]
    assert ctx.client.post(url, json=unordered_body, headers=headers).status_code == 201

    preview = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/detection-imports",
        files={
            "tracks_file": ("tracks.jsonl", make_tracks_jsonl().encode()),
            "metadata_file": ("metadata.json", json.dumps(SAMPLE_METADATA).encode()),
        },
        params={"confirm": False},
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["affected_annotations_count"] == 2
    assert body["unordered_force_reselection_count"] == 1
    assert body["role_based_revalidation_count"] == 1
    assert "annotation_statuses" not in body


def test_legacy_export_projects_role_participants_and_unordered_empty(ctx):
    headers, project, categories, video = _role_project(ctx)
    role_category, unordered = categories
    first, second = [item["key"] for item in role_category["role_definitions"]]
    url = f"/api/projects/{project['id']}/videos/{video['id']}/annotations"
    assert ctx.client.post(url, json=_body(
        role_category["id"], {first: [8], second: [3]}), headers=headers).status_code == 201
    plain = _body(unordered["id"])
    assert ctx.client.post(url, json=plain, headers=headers).status_code == 201
    events = ctx.client.get(f"{url}/export", headers=headers).json()
    assert events[0]["mouse_ids"] == [3, 8]
    assert events[0]["participants"] == [
        {"role_key": first, "role_name": "追逐者", "track_ids": [8]},
        {"role_key": second, "role_name": "被追逐者", "track_ids": [3]},
    ]
    assert events[1]["mouse_ids"] == []
    assert events[1]["participants"] == []
