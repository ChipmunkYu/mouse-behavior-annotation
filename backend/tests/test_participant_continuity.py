from types import SimpleNamespace
import json

import pytest

from app.participant_continuity import participant_continuity_error
from app.participant_roles import canonicalize_participant_roles
from .test_participant_roles_phase2 import _body, _role_project


NAMES = ["nose", "base_of_neck", "left_hip", "right_hip"]


class Query:
    def __init__(self, rows): self.rows = rows
    def order_by(self, *args):
        self.rows.sort(key=lambda row: (row.RawDetection.frame_index,
                                        row.RawDetection.frame_detection_index,
                                        row.RawDetection.id))
        return self
    def __iter__(self): return iter(self.rows)


def raw(frame, track=1, *, center=True, box=(0, 0, 10, 10), detection_index=0, row_id=None):
    point = {"x_px": 5, "y_px": 5, "confidence": .9} if center else {
        "x_px": float("nan"), "y_px": 5, "confidence": .9}
    detection = SimpleNamespace(frame_index=frame, frame_detection_index=detection_index,
        id=frame if row_id is None else row_id, box=dict(zip(("x1", "y1", "x2", "y2"), box)),
        keypoints=[point, point, point, point], detection_confidence=.9, class_id=0)
    return SimpleNamespace(RawDetection=detection, display_track_id=track)


def check(monkeypatch, frames, *, start=0, end=9, group="社交行为", crop=None):
    monkeypatch.setattr("app.participant_continuity.effective_detection_query",
                        lambda *args, **kwargs: Query(frames))
    imp = SimpleNamespace(id=1, fps=25, frame_count=100, width=100, height=100)
    category = SimpleNamespace(group=group)
    return participant_continuity_error(None, imp, category, [1], start, end, crop,
                                        keypoint_names=NAMES)


def test_configuration_driven_attack_roles_and_unordered_together():
    definitions = [
        {"key": "attacker", "name": "任意甲", "min_count": 1, "max_count": 1},
        {"key": "target", "name": "任意乙", "min_count": 1, "max_count": 1},
    ]
    roles, ids, status = canonicalize_participant_roles(definitions, {"attacker": [8], "target": [3]})
    assert (roles, ids, status) == ({"attacker": [8], "target": [3]}, [3, 8], "valid")
    # Together uses the same unordered category count path; continuity itself only reads group/configured IDs.


@pytest.mark.parametrize("missing", [range(2, 7), range(0, 5)])
def test_internal_and_boundary_five_missing_allowed(monkeypatch, missing):
    rows = [raw(frame) for frame in range(10) if frame not in missing]
    assert check(monkeypatch, rows) is None


def test_six_missing_rejected_with_track_range_and_count(monkeypatch):
    reason = check(monkeypatch, [raw(frame) for frame in (0, 7, 8, 9)])
    assert "轨迹 1" in reason and "[1, 6]" in reason and "6 帧" in reason


def test_non_social_long_gap_is_not_checked(monkeypatch):
    assert check(monkeypatch, [], group="个体行为") is None


def test_no_finite_center_counts_missing(monkeypatch):
    assert "没有可用于下游的帧" in check(monkeypatch, [raw(frame, center=False) for frame in range(10)])


def test_crop_omitted_detection_counts_missing(monkeypatch):
    rows = [raw(frame, box=(50, 50, 60, 60)) for frame in range(10)]
    assert "没有可用于下游的帧" in check(
        monkeypatch, rows, crop={"x": 0, "y": 0, "w": 20, "h": 20})


def test_later_duplicate_detection_determines_frame_usability(monkeypatch):
    # Deliberately reverse query iteration input: export order is detection_index, then row id.
    later_unusable = raw(0, center=False, detection_index=2, row_id=10)
    earlier_usable = raw(0, center=True, detection_index=1, row_id=20)
    reason = check(monkeypatch, [later_unusable, earlier_usable], start=0, end=5)
    assert "没有可用于下游的帧" in reason

    later_usable = raw(0, center=True, detection_index=2, row_id=10)
    earlier_unusable = raw(0, center=False, detection_index=1, row_id=20)
    assert check(monkeypatch, [later_usable, earlier_unusable], start=0, end=5) is None


def test_later_crop_excluded_duplicate_preserves_exported_frame(monkeypatch):
    rows = [raw(0, detection_index=1),
            raw(0, detection_index=2, box=(50, 50, 60, 60))]
    assert check(monkeypatch, rows, start=0, end=5,
                 crop={"x": 0, "y": 0, "w": 20, "h": 20}) is None


def test_unordered_ids_strict_at_post_and_patch_without_import(ctx):
    headers, project, categories, video = _role_project(ctx)
    url = f"/api/projects/{project['id']}/videos/{video['id']}/annotations"
    body = {**_body(categories[1]["id"]), "mouse_ids": [0, 99]}
    created = ctx.client.post(url, json=body, headers=headers)
    assert created.status_code == 201, created.text
    item_url = f"{url}/{created.json()['id']}"
    for ids in ([-1], [True], ["1"], [1.0]):
        assert ctx.client.post(url, json={**body, "mouse_ids": ids}, headers=headers).status_code == 422
        assert ctx.client.patch(item_url, json={"mouse_ids": ids}, headers=headers).status_code == 422
    for ids in ([], [1, 2, 3]):
        assert ctx.client.post(url, json={**body, "mouse_ids": ids}, headers=headers).status_code == 400
        assert ctx.client.patch(item_url, json={"mouse_ids": ids}, headers=headers).status_code == 400
    patched = ctx.client.patch(item_url, json={"mouse_ids": [7]}, headers=headers)
    assert patched.status_code == 200, patched.text
    assert patched.json()["mouse_ids"] == [7]


def test_patch_validates_merged_candidate_before_mutation(ctx):
    headers, project, categories, video = _role_project(ctx)
    category = categories[0]
    first, second = [item["key"] for item in category["role_definitions"]]
    url = f"/api/projects/{project['id']}/videos/{video['id']}/annotations"
    created = ctx.client.post(url, json=_body(
        category["id"], {first: [1], second: [2]}), headers=headers)
    assert created.status_code == 201, created.text
    rejected = ctx.client.patch(f"{url}/{created.json()['id']}", json={
        "participant_roles": {first: [1], second: []}, "end_frame": 8,
    }, headers=headers)
    assert rejected.status_code == 400
    current = ctx.client.get(url, headers=headers).json()[0]
    assert current["participant_roles"] == {first: [1], second: [2]}
    assert current["end_frame"] == 4


def test_http_social_gap_gate_and_submit_defense(ctx):
    from app.models import Annotation, Submission, Video
    from .test_detection_imports import SAMPLE_METADATA, _det, _make_frame_line

    headers, project, categories, video = _role_project(ctx, unordered_group="社交行为")
    category = categories[1]
    base = f"/api/projects/{project['id']}/videos/{video['id']}"
    metadata = {**SAMPLE_METADATA, "frame_count": 10,
                "keypoint_names": ["nose", "base_of_neck", "left_hip"]}
    tracks = "\n".join(_make_frame_line(frame, [_det(1)] if frame == 0 else [])
                       for frame in range(10)) + "\n"
    imported = ctx.client.post(base + "/detection-imports", params={"confirm": True},
        files={"tracks_file": ("tracks.jsonl", tracks.encode()),
               "metadata_file": ("metadata.json", json.dumps(metadata).encode())}, headers=headers)
    assert imported.status_code == 200, imported.text
    body = {"category_id": category["id"], "start_frame": 0, "end_frame": 5,
            "mouse_ids": [1], "detection_import_revision": imported.json()["revision"],
            "identity_revision": 0}
    created = ctx.client.post(base + "/annotations", json=body, headers=headers)
    assert created.status_code == 201, created.text
    aid = created.json()["id"]

    def state():
        with ctx.session_factory() as db:
            ann, vid = db.get(Annotation, aid), db.get(Video, video["id"])
            return (ann.end_frame, ann.material_revision, ann.review_status,
                    vid.annotation_revision, vid.media_revision, vid.workflow_status,
                    db.query(Submission).count())

    before = state()
    for response in (
        ctx.client.post(base + "/annotations", json={**body, "end_frame": 6}, headers=headers),
        ctx.client.patch(base + f"/annotations/{aid}", json={"end_frame": 6}, headers=headers),
    ):
        assert response.status_code == 400, response.text
        assert "6 帧" in response.text
    assert state() == before
    assert ctx.client.get(base + "/annotations", headers=headers).json() == [created.json()]
    # Simulate a historical invalid draft; submit must not manufacture a submission.
    with ctx.session_factory() as db:
        ann = db.get(Annotation, aid)
        ann.end_frame, ann.end_time = 6, 7 / 25
        source = ctx.raw_client.app.state.settings.videos_dir / "continuity.mp4"
        source.write_bytes(b"continuity-test-source")
        db.get(Video, video["id"]).storage_path = source.name
        db.commit()
    before = state()
    submitted = ctx.client.post(base + "/submit", headers=headers)
    assert submitted.status_code == 400, submitted.text
    assert "6 帧" in submitted.text
    assert state() == before
