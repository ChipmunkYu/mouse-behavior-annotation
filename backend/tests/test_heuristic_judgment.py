"""Runnable API loop, rollback attacks and immutable four-file export evidence."""
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import zipfile

import pytest
from sqlalchemy import text

from app.models import Annotation, Clip, Review, Submission, Video, VideoImportBatch
from tests.conftest import auth_headers


def setup_project(ctx):
    headers = auth_headers(ctx.client)
    categories = [dict(name="one", group="test", color="#808080", sort_order=0,
                       participant_mode="unordered", role_definitions=[], mouse_count_min=1),
                  dict(name="roles", group="test", color="#909090", sort_order=1,
                       participant_mode="role_based", role_definitions=[
                           dict(name="actor", min_count=1, max_count=1, role_sort_order=0),
                           dict(name="target", min_count=1, max_count=1, role_sort_order=1)])]
    r = ctx.client.post("/api/projects", json=dict(name="heuristic", categories=categories), headers=headers)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = ctx.client.post(f"/api/projects/{pid}/category-scheme/lock", json={"expected_version": 0}, headers=headers)
    assert r.status_code == 200, r.text
    return headers, pid, r.json()["categories"]


def files():
    ann = dict(behavior="unknown label", mouse_ids=[1, 2], participants=[], actor_id=-1,
               frame_range=dict(start=2, end=7), confidence="heuristic_candidate")
    meta = dict(schema_version="2.0", clip=dict(filename="clip.mp4", fps=10, width=64, height=48, frame_count=10),
                tracks=dict(frame_origin=0, time_origin=0, coordinate_system="clip_pixels"),
                pose=dict(keypoint_names=["nose"], skeleton_edges=[]),
                source=dict(video_filename="original.mp4", start_frame=100, end_frame=109, start_time=10, end_time=11))
    tracks = [dict(frame=i, time=i / 10, detections=[dict(track_id=t, box=[1, 2, 10, 12],
                  keypoints=[[3, 4, .9]], detection_confidence=.8, class_id=0) for t in (1, 2)]) for i in range(10)]
    return {"video": b"INPUT-MP4-BYTES", "annotation": ann, "metadata": meta, "tracks": tracks}


def upload(ctx, headers, pid, content=None, *, complete=True, uppercase_names=False):
    content = content or files()
    base = f"/api/projects/{pid}/video-import-batches"
    r = ctx.client.post(base, json={"source": "heuristic"}, headers=headers)
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    for role in ("tracks", "metadata", "annotation", "video"):
        filename = "clip.mp4" if role == "video" else role + ".json"
        if uppercase_names:
            filename = filename.upper()
        data = content[role] if role == "video" else json.dumps(content[role]).encode()
        r = ctx.client.put(f"{base}/{bid}/files/{role}", files={"file": (filename, data)}, headers=headers)
        assert r.status_code == 200, r.text
    if not complete:
        return bid
    r = ctx.client.post(f"{base}/{bid}/complete", headers=headers)
    assert r.status_code == 200, r.text
    return bid, r.json()


def context(state):
    return dict(expected_annotation_revision=state["annotation_revision"],
                base_detection_import_revision=state["detection_import_revision"], base_identity_revision=state["identity_revision"],
                expected_submission_id=state["submission_id"],
                expected_decision_revision=state["decision_revision"] if state["submission_id"] else None)


def get(ctx, base, headers):
    r = ctx.client.get(base + "/judgment", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def save(ctx, base, headers, category, *, state=None, ids=None, roles=None):
    payload = dict(judgment_status="usable", category_id=category["id"], **context(state or get(ctx, base, headers)))
    if roles is not None:
        payload["participant_roles"] = roles
    else:
        payload.update(mouse_ids=ids or [1], participant_roles={})
    r = ctx.client.put(base + "/judgment", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def submit(ctx, base, headers):
    r = ctx.client.post(base + "/submit", json=context(get(ctx, base, headers)), headers=headers)
    assert r.status_code == 200, r.text
    return get(ctx, base, headers)


def approve(ctx, base, headers):
    state = ctx.client.get(base + "/review-state", headers=headers).json()
    sid, aid = state["submission_id"], state["annotations"][0]["id"]
    r = ctx.client.put(f"{base}/submissions/{sid}/annotations/{aid}/decision",
                       json=dict(status="approved", expected_decision_revision=state["decision_revision"]), headers=headers)
    assert r.status_code == 200, r.text
    r = ctx.client.post(base + "/review", json=dict(result="approved", expected_submission_id=sid,
                        expected_decision_revision=r.json()["decision_revision"]), headers=headers)
    assert r.status_code == 200, r.text
    return sid, aid


def test_api_loop_atomic_snapshot_export_and_final_locks(media_ctx, monkeypatch):
    ctx = media_ctx
    headers, pid, cats = setup_project(ctx)
    bid, completion = upload(ctx, headers, pid)
    assert completion["status"] == "ready", completion
    vid = completion["video_id"]
    base = f"/api/projects/{pid}/videos/{vid}"
    state = get(ctx, base, headers)
    assert state["selection"] is None and state["input"]["behavior"] == "unknown label"
    invalid = dict(judgment_status="usable", category_id=cats[0]["id"], mouse_ids=[999], **context(state))
    assert ctx.client.put(base + "/judgment", json=invalid, headers=headers).status_code == 400
    assert get(ctx, base, headers) == state
    state = save(ctx, base, headers, cats[0])
    assert save(ctx, base, headers, cats[0]) == state  # no-op keeps revision
    state = submit(ctx, base, headers)
    old_sid = state["submission_id"]
    review = ctx.client.get(base + "/review-state", headers=headers).json()
    r = ctx.client.put(f"{base}/submissions/{old_sid}/annotations/{review['annotations'][0]['id']}/decision",
        json=dict(status="approved", expected_decision_revision=review["decision_revision"]), headers=headers)
    assert r.status_code == 200, r.text
    keys = [r["key"] for r in cats[1]["role_definitions"]]
    state = save(ctx, base, headers, cats[1], roles={keys[0]: [1], keys[1]: [2]})
    assert state["submission_id"] != old_sid
    assert ctx.client.get(base + "/review-state", headers=headers).json()["annotations"][0]["decision"]["status"] == "pending"
    with ctx.session_factory() as db:
        old = db.get(Submission, old_sid)
        assert old.status == "withdrawn" and old.annotations[0].category_id == cats[0]["id"]
        assert old.annotations[0].mouse_ids == [1]
    sid, aid = approve(ctx, base, headers)
    assert not ctx.processor.clip_calls
    with ctx.session_factory() as db:
        clip = db.query(Clip).filter_by(submission_annotation_id=aid).one()
        assert clip.status == "ready", clip.error
        path = ctx.app.state.settings.clips_dir / clip.clip_path
        assert path.read_bytes() == files()["video"]
    # default is manual, even though heuristic assets are available
    assert ctx.client.post(f"/api/projects/{pid}/export", json={}, headers=headers).status_code == 400
    assert ctx.client.get(f"/api/projects/{pid}/export/status", headers=headers).json()["exportable_count"] == 0
    r = ctx.client.post(f"/api/projects/{pid}/export", json={"source": "heuristic"}, headers=headers)
    assert r.status_code == 201, r.text
    status = ctx.client.get(f"/api/projects/{pid}/export/status?source=heuristic", headers=headers).json()
    assert status["latest_job"]["status"] == "succeeded", status
    result = ctx.client.get(f"/api/projects/{pid}/export/download?source=heuristic", headers=headers)
    assert result.status_code == 200, result.text
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        assert archive.testzip() is None
        by_name = {name.rsplit("/", 1)[-1]: archive.read(name) for name in archive.namelist()}
        assert set(by_name) == {"clip.mp4", "annotation.json", "metadata.json", "tracks.json"}
        assert by_name["clip.mp4"] == files()["video"]
        annotation = json.loads(by_name["annotation.json"])
        assert annotation["frame_range"] == {"start": 2, "end": 7}
        assert annotation["behavior"] == "roles" and annotation["mouse_ids"] == [1, 2]
        assert len(json.loads(by_name["tracks.json"])) == 10
        assert json.loads(by_name["metadata.json"])["source"]["annotation"]["start_frame"] == 102
    assert ctx.client.get(f"/api/projects/{pid}/export/download", headers=headers).status_code == 404
    assert not ctx.processor.clip_calls
    # Missing asset repair must still copy.
    path.unlink()
    r = ctx.client.post(base + "/media/generate", headers=headers)
    assert r.status_code == 200, r.text
    assert path.read_bytes() == files()["video"] and not ctx.processor.clip_calls
    assert not ctx.client.get(base + "/review-state", headers=headers).json()["can_reopen"]
    terminal_attacks(ctx, headers, pid, vid, sid, aid)
    repeated = ctx.client.post(f"/api/projects/{pid}/video-import-batches/{bid}/complete", headers=headers)
    assert repeated.json()["video_id"] == vid
    assert ctx.client.delete(f"/api/projects/{pid}/video-import-batches/{bid}", headers=headers).status_code == 409


def test_submitted_heuristic_review_finalizes_unusable_with_audit_and_locks(media_ctx):
    ctx = media_ctx
    headers, pid, cats = setup_project(ctx)
    _, result = upload(ctx, headers, pid)
    vid = result["video_id"]
    base = f"/api/projects/{pid}/videos/{vid}"
    save(ctx, base, headers, cats[0])
    submitted = submit(ctx, base, headers)
    review_state = ctx.client.get(base + "/review-state", headers=headers).json()
    sid = review_state["submission_id"]
    snapshot_id = review_state["annotations"][0]["id"]
    payload = dict(result="unusable", comment="input cannot be reviewed",
                   expected_submission_id=sid,
                   expected_decision_revision=review_state["decision_revision"])

    member_id = ctx.create_user("no-review")
    ctx.add_member(pid, member_id, role="member")
    denied_headers = auth_headers(ctx.client, "no-review", "pw123")
    assert ctx.client.post(base + "/review", json=payload, headers=denied_headers).status_code == 403
    assert get(ctx, base, headers) == submitted

    for stale in ({**payload, "expected_submission_id": sid + 1},
                  {**payload, "expected_decision_revision": review_state["decision_revision"] + 1}):
        response = ctx.client.post(base + "/review", json=stale, headers=headers)
        assert response.status_code == 409, response.text
        assert get(ctx, base, headers) == submitted

    legacy = ctx.client.put(base + "/judgment",
        json=dict(judgment_status="unusable", **context(submitted)), headers=headers)
    assert legacy.status_code == 409
    assert legacy.json()["detail"]["code"] == "review_required_for_unusable"

    response = ctx.client.post(base + "/review", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    audit = response.json()
    assert audit["result"] == "unusable" and audit["reviewer"] == "demo"
    assert audit["submission_id"] == sid
    assert [row["id"] for row in audit["submission_annotations"]] == [snapshot_id]
    with ctx.session_factory() as db:
        submission = db.get(Submission, sid)
        review = db.query(Review).filter_by(submission_id=sid).one()
        assert submission.status == "unusable" and submission.decided_at is not None
        assert review.result == "unusable" and review.reviewer_id == submission.submitted_by
        assert len(submission.annotations) == 1
        assert db.query(Clip).count() == 0

    final = get(ctx, base, headers)
    assert final["workflow_status"] == final["judgment_status"] == "unusable"
    assert ctx.client.get(f"/api/projects/{pid}/clips?source=heuristic", headers=headers).json()["total"] == 0
    assert ctx.client.get(f"/api/projects/{pid}/export/status?source=heuristic", headers=headers).json()["exportable_count"] == 0
    assert ctx.client.post(f"/api/projects/{pid}/export", json={"source": "heuristic"}, headers=headers).status_code == 400
    assert not ctx.client.get(base + "/review-state", headers=headers).json()["can_reopen"]
    terminal_attacks(ctx, headers, pid, vid, sid, snapshot_id)


def test_manual_review_rejects_unusable(ctx):
    setup = ctx.make_project_with_video()
    response = ctx.client.post(
        f"/api/projects/{setup['project']['id']}/videos/{setup['video']['id']}/review",
        json={"result": "unusable", "expected_submission_id": 1,
              "expected_decision_revision": 0},
        headers=setup["headers"],
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unusable_requires_heuristic_video"


def terminal_attacks(ctx, headers, pid, vid, sid=1, aid=1):
    base = f"/api/projects/{pid}/videos/{vid}"
    state = get(ctx, base, headers)
    identity = dict(operation="split", track_ids=[1], frame=5, **context(state))
    attacks = [
        ("put", "/judgment", dict(judgment_status="unusable", **context(state))),
        ("post", "/annotations", dict(category_id=1, start_frame=2, end_frame=7, mouse_ids=[1])),
        ("patch", "/annotations/1", dict(mouse_ids=[1])),
        ("delete", "/annotations/1", None), ("post", "/identity-edits", identity),
        ("post", "/identity-edits/1/revert", context(state)),
        ("post", "/withdraw", dict(expected_submission_id=sid, expected_decision_revision=0)),
        ("post", "/submit", context(state)),
        ("post", f"/submissions/{sid}/reopen", {}),
        ("put", f"/submissions/{sid}/annotations/{aid}/decision", dict(status="pending", expected_decision_revision=0)),
        ("put", f"/submissions/{sid}/annotations/{aid}/feedback-mark", None),
        ("post", "/review", dict(result="rejected", expected_submission_id=sid, expected_decision_revision=0)),
        ("post", "/claim", None), ("post", "/release", None), ("delete", "", None),
        ("delete", "/distance-calibration", None),
        ("put", "/distance-calibration", dict(point_a=dict(x=0, y=0), point_b=dict(x=10, y=0), distance_cm=1)),
        ("post", "/detection-suppressions", dict(scope="corrected_track", track_id=1, base_identity_revision=state["identity_revision"], base_detection_import_revision=1)),
        ("post", "/detection-suppressions/1/revert", dict(base_identity_revision=state["identity_revision"], base_detection_import_revision=1)),
    ]
    for method, path, body in attacks:
        r = getattr(ctx.client, method)(base + path, headers=headers, **({"json": body} if body is not None else {}))
        assert r.status_code == 409, (method, path, r.status_code, r.text)
    assert get(ctx, base, headers) == state
    assert ctx.client.get(base + "/annotations/export", headers=headers).status_code == 409
    assert ctx.client.get(base + "/detections/export", headers=headers).status_code == 409
    assert ctx.client.post(f"/api/projects/{pid}/videos/claims", json={"video_ids": [vid]}, headers=headers).status_code == 409
    assert ctx.client.post(f"/api/projects/{pid}/videos/assignments", json={"video_ids": [vid]}, headers=headers).status_code == 409
    r = ctx.client.post(base + "/detection-imports", headers=headers, files={
        "tracks_file": ("tracks.jsonl", b"{}"), "metadata_file": ("metadata.json", b"{}")})
    assert r.status_code == 409, r.text


@pytest.mark.parametrize("damage", ["single", "events", "container_label", "schema", "box", "point", "frames", "source", "fps", "null", "array"])
def test_bad_input_publishes_nothing_and_cleanup_is_owned(media_ctx, damage):
    ctx = media_ctx
    headers, pid, _cats = setup_project(ctx)
    content = files()
    if damage == "single": content["annotation"]["frame_range"]["end"] = 2
    if damage == "events": content["annotation"]["events"] = []
    if damage == "container_label": content["annotation"]["behavior"] = "Multiple_behavior_candidates"
    if damage == "schema": content["metadata"]["schema_version"] = {"version": "2.0"}
    if damage == "box": content["tracks"][0]["detections"][0]["box"][0] = -1
    if damage == "point": content["tracks"][0]["detections"][0]["keypoints"][0][0] = 100
    if damage == "frames": content["tracks"].pop()
    if damage == "source": del content["metadata"]["source"]
    if damage == "fps": content["metadata"]["clip"]["fps"] = 0
    if damage == "null": content["tracks"][0]["detections"][0]["box"] = None
    if damage == "array": content["annotation"] = [content["annotation"]]
    bid, result = upload(ctx, headers, pid, content)
    assert result["status"] == "failed" and result["video_id"] is None, result
    assert ctx.client.get(f"/api/projects/{pid}/videos", headers=headers).json() == []
    with ctx.session_factory() as db:
        batch = db.get(VideoImportBatch, bid)
        names = [batch.annotation_path, batch.metadata_path, batch.tracks_path]
    r = ctx.client.delete(f"/api/projects/{pid}/video-import-batches/{bid}", headers=headers)
    assert r.status_code == 204, r.text
    assert not any((ctx.app.state.settings.detection_imports_dir / n).exists() for n in names)
    _new, good = upload(ctx, headers, pid)
    assert good["status"] == "ready", good


def test_unusable_and_generic_write_bypasses(media_ctx):
    ctx = media_ctx
    headers, pid, _cats = setup_project(ctx)
    _, result = upload(ctx, headers, pid)
    assert result["status"] == "ready", result
    vid = result["video_id"]
    base = f"/api/projects/{pid}/videos/{vid}"
    r = ctx.client.put(base + "/judgment", json=dict(judgment_status="unusable", **context(get(ctx, base, headers))), headers=headers)
    assert r.status_code == 200, r.text
    terminal_attacks(ctx, headers, pid, vid)
    assert ctx.client.post(base + "/media/generate", headers=headers).status_code == 409
    assert ctx.client.get(f"/api/projects/{pid}/clips?source=heuristic", headers=headers).json()["total"] == 0


def test_split_confirmation_undo_cas_and_after_image(media_ctx):
    ctx = media_ctx
    headers, pid, cats = setup_project(ctx)
    _, result = upload(ctx, headers, pid)
    assert result["status"] == "ready", result
    base = f"/api/projects/{pid}/videos/{result['video_id']}"
    state = save(ctx, base, headers, cats[0])
    payload = dict(operation="split", track_ids=[1], frame=5, **context(state))
    r = ctx.client.post(base + "/identity-edits/check", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["participant_confirmation_required"]
    assert ctx.client.post(base + "/identity-edits", json=payload, headers=headers).status_code == 409
    assert get(ctx, base, headers) == state
    payload["confirmed_selection"] = dict(category_id=cats[0]["id"], mouse_ids=[1, 3], participant_roles={})
    r = ctx.client.post(base + "/identity-edits", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    edit = r.json()
    assert edit["selection"]["mouse_ids"] == [1, 3] and edit["identity_revision"] == 1
    current = get(ctx, base, headers)
    r = ctx.client.post(f"{base}/identity-edits/{edit['edit_id']}/revert", json=context(current), headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["selection"]["mouse_ids"] == [1] and r.json()["identity_revision"] == 2
    assert ctx.client.post(base + "/identity-edits", json=payload, headers=headers).status_code == 409
    # Reviewer identity edit must replace the attempt in the same transaction.
    state = submit(ctx, base, headers)
    payload.update(context(state))
    payload["confirmed_selection"]["mouse_ids"] = [1, 4]
    r = ctx.client.post(base + "/identity-edits", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["submission_id"] != state["submission_id"]
    state = save(ctx, base, headers, cats[0], ids=[2])
    r = ctx.client.post(f"{base}/identity-edits/{r.json()['edit_id']}/revert", json=context(state), headers=headers)
    assert r.status_code == 409 and "after_image" in r.text


def test_reviewer_change_failure_keeps_old_attempt(media_ctx, monkeypatch):
    ctx = media_ctx
    headers, pid, cats = setup_project(ctx)
    _, result = upload(ctx, headers, pid)
    base = f"/api/projects/{pid}/videos/{result['video_id']}"
    save(ctx, base, headers, cats[0])
    state = submit(ctx, base, headers)
    from app import submission_service
    def fail(stage):
        if stage == "submission_annotations":
            raise RuntimeError("injected")
    monkeypatch.setattr(submission_service, "_fault", fail)
    with pytest.raises(RuntimeError, match="injected"):
        save(ctx, base, headers, cats[0], ids=[2])
    assert get(ctx, base, headers) == state
    with ctx.session_factory() as db:
        assert db.get(Submission, state["submission_id"]).status == "submitted"
        assert db.query(Submission).count() == 1


def test_real_mp4_copy_twice(media_ctx, tmp_path):
    from app.media import FfmpegMediaProcessor
    ffmpeg = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")
    ffprobe = os.environ.get("FFPROBE_PATH") or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("Existing ffmpeg/ffprobe required for real MP4 evidence")
    ctx = media_ctx
    processor = FfmpegMediaProcessor(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)
    original = tmp_path / "real.mp4"
    subprocess.run([processor.ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:s=64x48:r=10",
                    "-frames:v", "10", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(original)],
                   check=True, capture_output=True)
    # Real probe and thumbnail; rendering a clip in either phase is forbidden.
    def forbidden(**kwargs):
        pytest.fail("heuristic called render_clip")
    processor.render_clip = forbidden
    ctx.app.state.media_worker.processor = ctx.app.state.export_worker.processor = processor
    headers, pid, cats = setup_project(ctx)
    content = files()
    content["video"] = original.read_bytes()
    _, result = upload(ctx, headers, pid, content)
    assert result["status"] == "ready", result
    base = f"/api/projects/{pid}/videos/{result['video_id']}"
    save(ctx, base, headers, cats[0])
    submit(ctx, base, headers)
    approve(ctx, base, headers)
    r = ctx.client.post(f"/api/projects/{pid}/export", json={"source": "heuristic"}, headers=headers)
    assert r.status_code == 201, r.text
    r = ctx.client.get(f"/api/projects/{pid}/export/download?source=heuristic", headers=headers)
    assert r.status_code == 200, r.text
    with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
        video = next(name for name in archive.namelist() if name.endswith("clip.mp4"))
        assert hashlib.sha256(archive.read(video)).hexdigest() == hashlib.sha256(content["video"]).hexdigest()


def test_judgment_roles_permissions_and_cas_are_atomic(media_ctx):
    ctx = media_ctx
    headers, pid, cats = setup_project(ctx)
    _, result = upload(ctx, headers, pid)
    base = f"/api/projects/{pid}/videos/{result['video_id']}"
    state = save(ctx, base, headers, cats[0])
    keys = [r["key"] for r in cats[1]["role_definitions"]]
    for changes, code in [
        (dict(expected_annotation_revision=state["annotation_revision"] - 1), 409),
        (dict(base_identity_revision=1), 409),
        (dict(start_frame=0), 422),
        (dict(mouse_ids=[True]), 422),
        (dict(category_id=cats[1]["id"], participant_roles={"actor": [1]}), 422),
        (dict(category_id=cats[1]["id"], participant_roles={keys[0]: [1], keys[1]: [1]}), 422),
        (dict(category_id=cats[1]["id"], participant_roles={keys[0]: [1], keys[1]: []}), 400),
    ]:
        body = dict(judgment_status="usable", category_id=cats[0]["id"], **context(state))
        body.update(changes)
        r = ctx.client.put(base + "/judgment", json=body, headers=headers)
        assert r.status_code == code, r.text
        assert get(ctx, base, headers) == state
    uid = ctx.create_user("other")
    ctx.add_member(pid, uid)
    other = auth_headers(ctx.client, "other", "pw123")
    body = dict(judgment_status="usable", category_id=cats[0]["id"], mouse_ids=[2], **context(state))
    assert ctx.client.put(base + "/judgment", json=body, headers=other).status_code == 403
    state = submit(ctx, base, headers)
    body.update(context(state))
    assert ctx.client.put(base + "/judgment", json=body, headers=other).status_code == 403
    assert ctx.client.put(base + "/judgment", json={**body, "expected_submission_id": None}, headers=headers).status_code == 409
    assert get(ctx, base, headers) == state


def test_merge_roles_confirmation_undo_and_frozen_export(media_ctx):
    ctx = media_ctx
    headers, pid, cats = setup_project(ctx)
    content = files()
    content["metadata"]["distance_scale"] = dict(cm_per_pixel=0.1, unit="cm")
    for frame in content["tracks"]:
        first, second = frame["detections"]
        third = copy.deepcopy(first)
        third["track_id"] = 3
        frame["detections"] = [first if frame["frame"] < 5 else second, third]
    _, result = upload(ctx, headers, pid, content)
    base = f"/api/projects/{pid}/videos/{result['video_id']}"
    keys = [r["key"] for r in cats[1]["role_definitions"]]
    state = save(ctx, base, headers, cats[1], roles={keys[0]: [1], keys[1]: [2]})
    payload = dict(operation="merge", track_ids=[1, 2], **context(state))
    preview = ctx.client.post(base + "/identity-edits/check", json=payload, headers=headers)
    assert preview.status_code == 200, preview.text
    assert preview.json()["participant_confirmation_required"]
    assert ctx.client.post(base + "/identity-edits", json=payload, headers=headers).status_code == 409
    assert get(ctx, base, headers) == state
    retained = preview.json()["retained_display_track_id"]
    payload["confirmed_selection"] = dict(category_id=cats[1]["id"], participant_roles={keys[0]: [retained], keys[1]: [3]})
    r = ctx.client.post(base + "/identity-edits", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    edit = r.json()
    assert edit["selection"]["mouse_ids"] == [retained, 3]
    state = get(ctx, base, headers)
    r = ctx.client.post(f"{base}/identity-edits/{edit['edit_id']}/revert", json=context(state), headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["selection"]["participant_roles"] == {keys[0]: [1], keys[1]: [2]}
    payload.update(context(get(ctx, base, headers)))
    r = ctx.client.post(base + "/identity-edits", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    submit(ctx, base, headers)
    approve(ctx, base, headers)
    r = ctx.client.post(f"/api/projects/{pid}/export", json={"source": "heuristic"}, headers=headers)
    assert r.status_code == 201, r.text
    r = ctx.client.get(f"/api/projects/{pid}/export/download?source=heuristic", headers=headers)
    assert r.status_code == 200, r.text
    with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
        docs = {n.rsplit("/", 1)[-1]: archive.read(n) for n in archive.namelist()}
        ann = json.loads(docs["annotation.json"])
        assert ann["mouse_ids"] == [retained, 3]
        assert [p["track_ids"] for p in ann["participants"]] == [[retained], [3]]
        exported = json.loads(docs["tracks.json"])
        for source, target in zip(content["tracks"], exported):
            assert [d["track_id"] for d in target["detections"]] == [retained, 3]
            assert [d["box"] for d in target["detections"]] == [d["box"] for d in source["detections"]]
            assert [d["keypoints"] for d in target["detections"]] == [d["keypoints"] for d in source["detections"]]
        assert docs["clip.mp4"] == content["video"] and not ctx.processor.clip_calls
        assert json.loads(docs["metadata.json"])["distance_scale"] == content["metadata"]["distance_scale"]


def test_source_filters_and_export_scopes_do_not_leak(media_ctx):
    ctx = media_ctx
    headers, pid, cats = setup_project(ctx)
    _, result = upload(ctx, headers, pid)
    vid = result["video_id"]
    base = f"/api/projects/{pid}/videos/{vid}"
    save(ctx, base, headers, cats[0])
    submit(ctx, base, headers)
    # A rejected attempt never publishes; retry starts with a fresh decision.
    review = ctx.client.get(base + "/review-state", headers=headers).json()
    r = ctx.client.put(f"{base}/submissions/{review['submission_id']}/annotations/{review['annotations'][0]['id']}/decision",
        json=dict(status="rejected", feedback="retry", expected_decision_revision=review["decision_revision"]), headers=headers)
    r = ctx.client.post(base + "/review", json=dict(result="rejected", comment="retry",
        expected_submission_id=review["submission_id"], expected_decision_revision=r.json()["decision_revision"]), headers=headers)
    assert r.status_code == 200, r.text
    assert ctx.client.get(f"/api/projects/{pid}/clips?source=all", headers=headers).json()["total"] == 0
    save(ctx, base, headers, cats[0], ids=[2])
    submit(ctx, base, headers)
    review = ctx.client.get(base + "/review-state", headers=headers).json()
    assert review["annotations"][0]["decision"]["status"] == "pending"
    approve(ctx, base, headers)
    r = ctx.client.post(f"/api/projects/{pid}/videos", json=dict(filename="manual.mp4", fps=10, duration=1, width=64, height=48), headers=headers)
    assert r.status_code == 201, r.text
    manual = r.json()["id"]
    for source, expected in [("all", {vid, manual}), ("manual", {manual}), ("heuristic", {vid})]:
        assert {v["id"] for v in ctx.client.get(f"/api/projects/{pid}/videos?source={source}", headers=headers).json()} == expected
        filtered = ctx.client.get(f"/api/projects/{pid}/videos?source={source}&workflow_status=draft", headers=headers).json()
        assert {v["id"] for v in filtered} == expected.intersection({manual})
        count = 0 if source == "manual" else 1
        assert ctx.client.get(f"/api/projects/{pid}/clips?source={source}", headers=headers).json()["total"] == count
        assert sum(c["count"] for c in ctx.client.get(f"/api/projects/{pid}/clips/categories?source={source}", headers=headers).json()) == count
        assert ctx.client.get(f"/api/projects/{pid}/clips?source={source}&video_id={manual}", headers=headers).json()["total"] == 0
        assert ctx.client.get(f"/api/projects/{pid}/clips?source={source}&search=no-match", headers=headers).json()["total"] == 0
    for source in ("heuristic", "all"):
        r = ctx.client.post(f"/api/projects/{pid}/export", json={"source": source, "category_ids": [cats[0]["id"]]}, headers=headers)
        assert r.status_code == 201, r.text
        status = ctx.client.get(f"/api/projects/{pid}/export/status?source={source}", headers=headers).json()
        assert status["source"] == source and status["category_ids"] == [cats[0]["id"]]
        with ctx.session_factory() as db:
            from app.models import BackgroundJob
            assert db.get(BackgroundJob, status["latest_job"]["id"]).payload["source"] == source
    assert ctx.client.get(f"/api/projects/{pid}/export/status", headers=headers).json()["latest_job"] is None
    assert ctx.client.get(f"/api/projects/{pid}/export/download", headers=headers).status_code == 404


def test_fourth_slot_processing_cleanup_retry_and_history_protection(media_ctx, monkeypatch):
    from datetime import datetime, timedelta
    from app import import_batch_cleanup
    from app.cleanup import run_retention_cleanup
    ctx = media_ctx
    headers, pid, _cats = setup_project(ctx)
    good_bid, good = upload(ctx, headers, pid)
    bid = upload(ctx, headers, pid, complete=False)
    root = ctx.app.state.settings.detection_imports_dir
    with ctx.session_factory() as db:
        batch = db.get(VideoImportBatch, bid)
        owned = [root / batch.annotation_path, root / (batch.tracks_path + ".normalized.jsonl.part")]
        owned[-1].write_text("partial", encoding="utf-8")
        batch.annotation_upload_state = "uploading"
        db.commit()
    url = f"/api/projects/{pid}/video-import-batches/{bid}"
    assert ctx.client.post(url + "/complete", headers=headers).status_code == 409
    with ctx.session_factory() as db:
        with pytest.raises(import_batch_cleanup.BatchCleanupConflict, match="upload in progress"):
            import_batch_cleanup.cleanup_import_batch(db, bid, pid, ctx.app.state.settings)
    with ctx.session_factory() as db:
        batch = db.get(VideoImportBatch, bid)
        batch.annotation_upload_state = "uploaded"
        db.commit()
    original = import_batch_cleanup.remove_checked
    def fail_annotation(path, **kwargs):
        return (False, "injected unlink failure") if path == owned[0] else original(path, **kwargs)
    monkeypatch.setattr(import_batch_cleanup, "remove_checked", fail_annotation)
    assert ctx.client.delete(url, headers=headers).status_code == 409
    assert ctx.client.get(url, headers=headers).status_code == 200
    monkeypatch.setattr(import_batch_cleanup, "remove_checked", original)
    with ctx.session_factory() as db:
        batch = db.get(VideoImportBatch, bid)
        batch.status = "processing"
        batch.updated_at = datetime.utcnow() - timedelta(days=2)
        db.commit()
        report = run_retention_cleanup(db, ctx.app.state.settings)
        assert report["import_batches_deleted"] == 1, report
        assert db.get(VideoImportBatch, good_bid).status == "ready"
        assert db.get(Video, good["video_id"]) is not None
    assert not any(p.exists() for p in owned)


def test_import_transaction_failure_leaves_no_video(media_ctx, monkeypatch):
    from app.routers import detection_imports
    ctx = media_ctx
    headers, pid, _cats = setup_project(ctx)
    def fail(**kwargs):
        raise RuntimeError("injected insert failure")
    monkeypatch.setattr(detection_imports, "_insert_detection_import_data", fail)
    bid, result = upload(ctx, headers, pid)
    assert result["status"] == "failed" and result["video_id"] is None
    with ctx.session_factory() as db:
        assert db.query(Video).count() == 0
        assert db.get(VideoImportBatch, bid).created_video_id is None


def test_withdraw_decision_rollback_and_fresh_resubmission(media_ctx):
    ctx = media_ctx
    headers, pid, cats = setup_project(ctx)
    _, result = upload(ctx, headers, pid)
    base = f"/api/projects/{pid}/videos/{result['video_id']}"
    state = save(ctx, base, headers, cats[0])
    for changes in (dict(track_ids=[True]), dict(base_identity_revision=False), dict(frame=True)):
        body = dict(operation="split", track_ids=[1], frame=5, **context(state))
        body.update(changes)
        assert ctx.client.post(base + "/identity-edits", json=body, headers=headers).status_code == 422
    state = submit(ctx, base, headers)
    assert ctx.client.post(base + "/withdraw", headers=headers).status_code == 422
    r = ctx.client.post(base + "/withdraw", json=dict(expected_submission_id=state["submission_id"], expected_decision_revision=1), headers=headers)
    assert r.status_code == 409, r.text
    r = ctx.client.put(base + "/judgment", json=dict(judgment_status="unusable", **context(state)), headers=headers)
    assert r.status_code == 409, r.text
    review = ctx.client.get(base + "/review-state", headers=headers).json()
    endpoint = f"{base}/submissions/{state['submission_id']}/annotations/{review['annotations'][0]['id']}/decision"
    for status in ("approved", "pending"):
        r = ctx.client.put(endpoint, json=dict(status=status, expected_decision_revision=review["decision_revision"]), headers=headers)
        assert r.status_code == 200, r.text
        review = r.json()
        assert review["annotations"][0]["decision"]["status"] == status
    state = get(ctx, base, headers)
    r = ctx.client.post(base + "/withdraw", json=dict(expected_submission_id=state["submission_id"], expected_decision_revision=state["decision_revision"]), headers=headers)
    assert r.status_code == 200, r.text
    # Withdrawal does not change content revisions: the attempt CAS must still
    # reject a pre-submit tab which sends null submission context.
    withdrawn = get(ctx, base, headers)
    stale = {**context(withdrawn), "expected_submission_id": None, "expected_decision_revision": None}
    r = ctx.client.put(base + "/judgment", json=dict(judgment_status="usable", category_id=cats[0]["id"], mouse_ids=[2], **stale), headers=headers)
    assert r.status_code == 409, r.text
    assert get(ctx, base, headers) == withdrawn
    new = submit(ctx, base, headers)
    assert new["submission_id"] != state["submission_id"]
    assert ctx.client.get(base + "/review-state", headers=headers).json()["annotations"][0]["decision"]["status"] == "pending"


def test_heuristic_database_authority_guards(media_ctx):
    from sqlalchemy.exc import IntegrityError
    ctx = media_ctx
    headers, pid, cats = setup_project(ctx)
    _, result = upload(ctx, headers, pid)
    vid = result["video_id"]
    base = f"/api/projects/{pid}/videos/{vid}"
    state = save(ctx, base, headers, cats[0])
    statements = ["UPDATE videos SET source='manual' WHERE id=:v",
                  "UPDATE videos SET heuristic_input='{}' WHERE id=:v",
                  "UPDATE annotations SET start_frame=0 WHERE video_id=:v",
                  "DELETE FROM annotations WHERE video_id=:v",
                  "DELETE FROM videos WHERE id=:v"]
    for sql in statements:
        with ctx.session_factory() as db:
            with pytest.raises(IntegrityError):
                db.execute(text(sql), {"v": vid})
            db.rollback()
    assert get(ctx, base, headers) == state


    submit(ctx, base, headers)
    approve(ctx, base, headers)
    state = get(ctx, base, headers)
    for sql in ("UPDATE videos SET workflow_status='draft' WHERE id=:v",
                "UPDATE videos SET identity_revision=identity_revision+1 WHERE id=:v",
                "UPDATE annotations SET mouse_ids='[2]' WHERE video_id=:v",
                "UPDATE submissions SET status='superseded' WHERE video_id=:v"):
        with ctx.session_factory() as db:
            with pytest.raises(IntegrityError):
                db.execute(text(sql), {"v": vid})
            db.rollback()
    assert get(ctx, base, headers) == state


def test_identity_is_local_and_suggestions_do_not_expose_open_extension_fields(media_ctx):
    ctx = media_ctx
    headers, pid, cats = setup_project(ctx)
    content = files()
    content["annotation"]["participants"] = [dict(role_key="external", role_name="external",
        track_ids=[1], server_path="not-public", evidence={"private": True})]
    _, first = upload(ctx, headers, pid, content)
    _, second = upload(ctx, headers, pid, content)
    base = f"/api/projects/{pid}/videos/{first['video_id']}"
    other = f"/api/projects/{pid}/videos/{second['video_id']}"
    assert get(ctx, base, headers)["input"]["participants"] == [
        dict(role_key="external", role_name="external", track_ids=[1])]
    original = get(ctx, other, headers)
    original_detections = ctx.client.get(other + "/detections?start_frame=0&end_frame=9", headers=headers).json()
    state = save(ctx, base, headers, cats[0])
    r = ctx.client.post(base + "/identity-edits", json=dict(operation="split", track_ids=[1], frame=5,
        confirmed_selection=dict(category_id=cats[0]["id"], mouse_ids=[1, 3], participant_roles={}),
        **context(state)), headers=headers)
    assert r.status_code == 200, r.text
    assert get(ctx, other, headers) == original
    assert ctx.client.get(other + "/detections?start_frame=0&end_frame=9", headers=headers).json() == original_detections


def test_upload_matches_frontend_case_insensitive_four_slots(media_ctx):
    headers, pid, _cats = setup_project(media_ctx)
    _bid, result = upload(media_ctx, headers, pid, uppercase_names=True)
    assert result["status"] == "ready", result
    base = f"/api/projects/{pid}/videos/{result['video_id']}"
    assert get(media_ctx, base, headers)["selection"] is None


@pytest.mark.parametrize("closed_status", ["rejected", "withdrawn"])
def test_closed_attempt_wrong_rejection_can_be_reset_and_resubmitted(media_ctx, closed_status):
    ctx = media_ctx
    headers, pid, cats = setup_project(ctx)
    _, result = upload(ctx, headers, pid)
    base = f"/api/projects/{pid}/videos/{result['video_id']}"
    save(ctx, base, headers, cats[0])
    state = submit(ctx, base, headers)
    review = ctx.client.get(base + "/review-state", headers=headers).json()
    sid, snapshot_id = review["submission_id"], review["annotations"][0]["id"]
    endpoint = f"{base}/submissions/{sid}/annotations/{snapshot_id}/decision"
    r = ctx.client.put(endpoint, json=dict(status="rejected", feedback="wrong decision",
        expected_decision_revision=review["decision_revision"]), headers=headers)
    assert r.status_code == 200, r.text
    revision = r.json()["decision_revision"]
    closing = dict(expected_submission_id=sid, expected_decision_revision=revision)
    if closed_status == "rejected":
        r = ctx.client.post(base + "/review", json=dict(result="rejected", **closing), headers=headers)
    else:
        r = ctx.client.post(base + "/withdraw", json=closing, headers=headers)
    assert r.status_code == 200, r.text
    before = get(ctx, base, headers)
    # The unchanged, otherwise correct judgment really is blocked by this rejection.
    r = ctx.client.post(base + "/submit", json=context(before), headers=headers)
    assert r.status_code == 409 and "rejected_annotations_not_addressed" in r.text
    for status, expected_revision in (("approved", revision), ("rejected", revision), ("pending", revision - 1)):
        r = ctx.client.put(endpoint, json=dict(status=status, feedback="wrong decision",
            expected_decision_revision=expected_revision), headers=headers)
        assert r.status_code == 409, r.text
    r = ctx.client.put(endpoint, json=dict(status="pending", expected_decision_revision=revision), headers=headers)
    assert r.status_code == 200, r.text
    reset = r.json()
    assert reset["submission_status"] == closed_status
    assert reset["decision_revision"] == revision + 1
    assert reset["annotations"][0]["decision"]["status"] == "pending"
    after = get(ctx, base, headers)
    assert after["annotation_revision"] == before["annotation_revision"]
    assert after["selection"] == before["selection"]
    new = submit(ctx, base, headers)
    assert new["submission_id"] != sid
    current = ctx.client.get(base + "/review-state", headers=headers).json()
    assert current["annotations"][0]["decision"]["status"] == "pending"
    with ctx.session_factory() as db:
        old, latest = db.get(Submission, sid), db.get(Submission, new["submission_id"])
        assert old.annotations[0].material_digest == latest.annotations[0].material_digest
    # Even pending must not mutate an older attempt once a newer one exists.
    r = ctx.client.put(endpoint, json=dict(status="pending", expected_decision_revision=revision + 1), headers=headers)
    assert r.status_code == 409, r.text


@pytest.mark.parametrize("terminal", ["approved", "unusable"])
def test_terminal_judgment_still_rejects_decision_reset(media_ctx, terminal):
    ctx = media_ctx
    headers, pid, cats = setup_project(ctx)
    _, result = upload(ctx, headers, pid)
    base = f"/api/projects/{pid}/videos/{result['video_id']}"
    save(ctx, base, headers, cats[0])
    submit(ctx, base, headers)
    if terminal == "approved":
        sid, snapshot_id = approve(ctx, base, headers)
    else:
        review = ctx.client.get(base + "/review-state", headers=headers).json()
        sid, snapshot_id = review["submission_id"], review["annotations"][0]["id"]
        r = ctx.client.put(f"{base}/submissions/{sid}/annotations/{snapshot_id}/decision",
            json=dict(status="rejected", feedback="unusable", expected_decision_revision=review["decision_revision"]), headers=headers)
        assert r.status_code == 200, r.text
        r = ctx.client.post(base + "/withdraw", json=dict(expected_submission_id=sid,
            expected_decision_revision=r.json()["decision_revision"]), headers=headers)
        assert r.status_code == 200, r.text
        r = ctx.client.put(base + "/judgment", json=dict(judgment_status="unusable",
            **context(get(ctx, base, headers))), headers=headers)
        assert r.status_code == 200, r.text
    before = get(ctx, base, headers)
    review_before = ctx.client.get(base + "/review-state", headers=headers).json()
    r = ctx.client.put(f"{base}/submissions/{sid}/annotations/{snapshot_id}/decision",
        json=dict(status="pending", expected_decision_revision=before["decision_revision"]), headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "heuristic_completed_locked", r.text
    assert get(ctx, base, headers) == before
    assert ctx.client.get(base + "/review-state", headers=headers).json() == review_before
