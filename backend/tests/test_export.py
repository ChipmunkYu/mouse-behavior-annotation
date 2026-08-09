"""验收：导出字段与类别名。"""
from __future__ import annotations

EXPECTED_FIELDS = {
    "annotation_id",
    "video_id",
    "clip_file",
    "start_time",
    "end_time",
    "start_frame",
    "end_frame",
    "behavior",
    "mouse_ids",
    "detection_import_revision",
    "identity_revision",
    "crop_region",
    "confidence",
    "annotator",
    "reviewer",
    "review_status",
}


def test_export_event_fields_and_behavior_name(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = setup["headers"], setup["project"], setup["categories"], setup["video"]
    base_url = f"/api/projects/{project['id']}/videos/{video['id']}/annotations"

    # 用中文类别名标注，带 crop_region
    cat = next(c for c in categories if c["name"] == "奔跑")
    created = ctx.client.post(
        base_url,
        json={
            "category_id": cat["id"],
            "start_time": 5.0,
            "end_time": 8.5,
            "start_frame": 125,
            "end_frame": 212,
            "confidence": "certain",
            "crop_region": {"x": 10, "y": 20, "w": 50, "h": 30},
        },
        headers=headers,
    )
    assert created.status_code == 201

    resp = ctx.client.get(f"{base_url}/export", headers=headers)
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) == 1
    ev = events[0]

    assert set(ev.keys()) == EXPECTED_FIELDS
    assert ev["annotation_id"] == created.json()["id"]
    assert ev["video_id"] == f"video_{video['id']}"
    assert ev["clip_file"] is None
    assert ev["start_time"] == 5.0
    assert ev["end_time"] == 8.5
    assert ev["start_frame"] == 125
    assert ev["end_frame"] == 212
    assert ev["behavior"] == "奔跑"  # 类别名
    assert ev["mouse_ids"] == []
    assert ev["detection_import_revision"] == 0
    assert ev["identity_revision"] == 0
    assert ev["crop_region"] == {"x": 10, "y": 20, "w": 50, "h": 30}
    assert ev["confidence"] == "certain"
    assert ev["annotator"] == "demo"
    assert ev["reviewer"] is None
    assert ev["review_status"] == "pending"


def test_export_empty_video(ctx):
    setup = ctx.make_project_with_video()
    headers, project, video = setup["headers"], setup["project"], setup["video"]
    resp = ctx.client.get(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations/export",
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_export_non_member_rejected(ctx, login_headers):
    setup = ctx.make_project_with_video()
    project, video = setup["project"], setup["video"]
    alice_id = ctx.create_user("alice")
    alice_headers = login_headers(username="alice", password="pw123")
    resp = ctx.client.get(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations/export",
        headers=alice_headers,
    )
    assert resp.status_code == 403


def test_export_multiple_annotations_ordered(ctx):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = setup["headers"], setup["project"], setup["categories"], setup["video"]
    base_url = f"/api/projects/{project['id']}/videos/{video['id']}/annotations"

    for cat, t0 in ((categories[0], 1.0), (categories[1], 10.0)):
        resp = ctx.client.post(
            base_url,
            json={
                "category_id": cat["id"],
                "start_time": t0,
                "end_time": t0 + 2.0,
                "start_frame": int(t0 * 25),
                "end_frame": int((t0 + 2.0) * 25),
            },
            headers=headers,
        )
        assert resp.status_code == 201

    events = ctx.client.get(f"{base_url}/export", headers=headers).json()
    assert len(events) == 2
    assert [e["start_time"] for e in events] == [1.0, 10.0]
    assert events[0]["behavior"] == categories[0]["name"]
    assert events[1]["behavior"] == categories[1]["name"]
