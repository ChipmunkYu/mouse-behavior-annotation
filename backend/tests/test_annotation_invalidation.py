"""验收（批次 3）：标注写入与审核工作流联动（失效回 draft + Clip 清理）。

覆盖契约：
- 标注新增/删除/PATCH 实际字段，在 Video 非 draft（submitted/approved/rejected）时：
  回 draft、revision 仅 +1、清 submitted/approved 字段、删除该视频所有 Clip DB 记录及
  clips_dir/thumbnails_dir 内实体文件、Review 历史保留；已 draft 后连续修改不再增 revision。
- 标注写仅 owner/admin/annotator；reviewer 不能写（403）。
- 创建固定 pending；直接写 review_status → 422（创建非 pending 值 / PATCH 任意值）。
- 清理失败策略：越界路径绝不删除且记入 cleanup-issues.log；文件删除失败记日志不阻断。
"""
from __future__ import annotations

import json
from datetime import datetime

from app.models import Annotation, Clip, DetectionImport, Review, Video


def _now():
    return datetime.utcnow()


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


def _add_detection_import(ctx, video_id, project_id, user_id=1):
    """Helper: add a minimal active detection import so submit works. Idempotent."""
    with ctx.session_factory() as db:
        video = db.get(Video, video_id)
        if video is None or video.detection_import_revision != 0:
            return
        imp = DetectionImport(
            video_id=video_id,
            revision=1,
            schema_version="1.0",
            status="imported",
            active=True,
            created_by=user_id,
            tracks_path="tracks.jsonl",
            tracks_sha256="a" * 64,
            metadata_path="metadata.json",
            metadata_sha256="a" * 64,
            width=1280,
            height=720,
            fps=25.0,
            frame_count=5,
            frame_range={"first_frame": 0, "last_frame": 4},
            detection_count=5,
        )
        db.add(imp)
        video.detection_import_revision = 1
        db.commit()


def _annotate(ctx, headers, project, video, category_id, start_time=1.0):
    resp = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations",
        json={
            "category_id": category_id,
            "start_time": start_time,
            "end_time": start_time + 2.0,
            "start_frame": int(start_time * 25),
            "end_frame": int((start_time + 2.0) * 25),
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _patch(ctx, headers, project, video, ann_id, payload):
    return ctx.client.patch(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations/{ann_id}",
        json=payload,
        headers=headers,
    )


def _delete(ctx, headers, project, video, ann_id):
    return ctx.client.delete(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations/{ann_id}",
        headers=headers,
    )


def _submit(ctx, headers, project, video):
    _add_detection_import(ctx, video["id"], project["id"])
    return ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/submit", headers=headers
    )


def _review(ctx, headers, project, video, result):
    return ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/review",
        json={"result": result, "comment": "c"},
        headers=headers,
    )


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


def _count_clips(ctx, video_id) -> int:
    with ctx.session_factory() as db:
        return (
            db.query(Clip)
            .join(Annotation, Annotation.id == Clip.annotation_id)
            .filter(Annotation.video_id == video_id)
            .count()
        )


def _count_reviews(ctx, video_id) -> int:
    with ctx.session_factory() as db:
        return db.query(Review).filter(Review.video_id == video_id).count()


def _add_clip(
    ctx,
    tmp_path,
    project_id,
    annotation_id,
    clip_path="clip1.mp4",
    thumb_path="thumb1.jpg",
    clip_as_dir=False,
    source_revision=1,
):
    """写入一个 ready Clip 行；默认同时创建 clips_dir/thumbnails_dir 内实体文件。

    同一 annotation 多条 Clip 时须传不同的 source_revision（唯一约束）。
    """
    if clip_path and not clip_as_dir:
        f = tmp_path / "clips" / clip_path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"CLIP")
    if clip_path and clip_as_dir:
        d = tmp_path / "clips" / clip_path
        d.mkdir(parents=True, exist_ok=True)
    if thumb_path:
        t = tmp_path / "thumbnails" / thumb_path
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_bytes(b"THUMB")
    with ctx.session_factory() as db:
        clip = Clip(
            project_id=project_id,
            annotation_id=annotation_id,
            source_revision=source_revision,
            status="ready",
            clip_path=clip_path,
            thumbnail_path=thumb_path,
        )
        db.add(clip)
        db.commit()
        return clip.id


def _read_cleanup_log(ctx, tmp_path) -> list[dict]:
    log = tmp_path / "cleanup-issues.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _reviewer_headers(ctx, login_headers, project_id, username="rev"):
    uid = ctx.create_user(username)
    ctx.add_member(project_id, uid, role="reviewer")
    return login_headers(username=username, password="pw123"), uid


# ---------- 三种非 draft 状态的写入回 draft ----------


def _reach_state(ctx, headers, project, video, reviewer_headers, state):
    """把视频带到指定非 draft 状态，直接操作 DB 以避开提交前置条件。"""
    if state == "draft":
        return
    with ctx.session_factory() as db:
        v = db.get(Video, video["id"])
        v.workflow_status = state
        v.submitted_at = _now()
        if state == "approved":
            v.approved_at = _now()
            v.approved_by = 1
        for ann in db.query(Annotation).filter(Annotation.video_id == video["id"]).all():
            ann.review_status = "approved" if state == "approved" else state
        db.commit()


def test_create_returns_to_draft_in_all_non_draft_states(ctx, login_headers):
    for state in ("approved", "rejected"):
        setup = ctx.make_project_with_video()
        headers, project, categories, video = (
            setup["headers"],
            setup["project"],
            setup["categories"],
            setup["video"],
        )
        _annotate(ctx, headers, project, video, categories[0]["id"])
        reviewer_headers, _ = _reviewer_headers(ctx, login_headers, project["id"], f"rev_{state}")
        _reach_state(ctx, headers, project, video, reviewer_headers, state)
        assert _video_state(ctx, video["id"])["workflow_status"] == state

        # 新增标注 → 回 draft、revision +1、审核字段清空
        resp = ctx.client.post(
            f"/api/projects/{project['id']}/videos/{video['id']}/annotations",
            json={
                "category_id": categories[1]["id"],
                "start_time": 6.0,
                "end_time": 8.0,
                "start_frame": 150,
                "end_frame": 200,
            },
            headers=headers,
        )
        assert resp.status_code == 201, state
        st = _video_state(ctx, video["id"])
        assert st["workflow_status"] == "draft", state
        assert st["annotation_revision"] == 3, state
        assert st["submitted_at"] is None, state
        assert st["approved_at"] is None, state
        assert st["approved_by"] is None, state


def test_patch_returns_to_draft_in_all_non_draft_states(ctx, login_headers):
    for state in ("approved", "rejected"):
        setup = ctx.make_project_with_video()
        headers, project, categories, video = (
            setup["headers"],
            setup["project"],
            setup["categories"],
            setup["video"],
        )
        ann = _annotate(ctx, headers, project, video, categories[0]["id"])
        reviewer_headers, _ = _reviewer_headers(ctx, login_headers, project["id"], f"p_{state}")
        _reach_state(ctx, headers, project, video, reviewer_headers, state)

        resp = _patch(ctx, headers, project, video, ann["id"], {"end_time": 5.0})
        assert resp.status_code == 200, state
        assert resp.json()["end_time"] == 5.0
        st = _video_state(ctx, video["id"])
        assert st["workflow_status"] == "draft", state
        assert st["annotation_revision"] == 3, state
        assert st["submitted_at"] is None, state
        assert st["approved_at"] is None, state


def test_delete_returns_to_draft_in_all_non_draft_states(ctx, login_headers):
    for state in ("approved", "rejected"):
        setup = ctx.make_project_with_video()
        headers, project, categories, video = (
            setup["headers"],
            setup["project"],
            setup["categories"],
            setup["video"],
        )
        ann1 = _annotate(ctx, headers, project, video, categories[0]["id"])
        ann2 = _annotate(ctx, headers, project, video, categories[1]["id"], start_time=6.0)
        reviewer_headers, _ = _reviewer_headers(ctx, login_headers, project["id"], f"d_{state}")
        _reach_state(ctx, headers, project, video, reviewer_headers, state)

        assert _delete(ctx, headers, project, video, ann1["id"]).status_code == 204
        st = _video_state(ctx, video["id"])
        assert st["workflow_status"] == "draft", state
        assert st["annotation_revision"] == 4, state
        assert st["submitted_at"] is None, state
        # 另一条标注保留
        with ctx.session_factory() as db:
            assert db.get(Annotation, ann2["id"]) is not None


# ---------- revision 只增一次 ----------


def test_each_content_change_increments_once_while_draft_and_noop_stays_stable(ctx, login_headers):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    ann = _annotate(ctx, headers, project, video, categories[0]["id"])
    reviewer_headers, _ = _reviewer_headers(ctx, login_headers, project["id"])

    _reach_state(ctx, headers, project, video, reviewer_headers, "rejected")
    assert _video_state(ctx, video["id"])["annotation_revision"] == 2

    # 首次 PATCH 触发失效：revision 2 → 3
    assert _patch(ctx, headers, project, video, ann["id"], {"end_time": 5.0}).status_code == 200
    assert _video_state(ctx, video["id"])["annotation_revision"] == 3
    assert _video_state(ctx, video["id"])["workflow_status"] == "draft"

    # 已 draft 后每次实际内容修改仍推进 revision。
    assert _patch(ctx, headers, project, video, ann["id"], {"end_time": 6.0}).status_code == 200
    assert _video_state(ctx, video["id"])["annotation_revision"] == 4
    # 空 PATCH 与同值 PATCH 均为 no-op。
    assert _patch(ctx, headers, project, video, ann["id"], {}).status_code == 200
    assert _patch(ctx, headers, project, video, ann["id"], {"end_time": 6.0}).status_code == 200
    assert _video_state(ctx, video["id"])["annotation_revision"] == 4
    resp = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations",
        json={
            "category_id": categories[1]["id"],
            "start_time": 9.0,
            "end_time": 11.0,
            "start_frame": 225,
            "end_frame": 275,
        },
        headers=headers,
    )
    assert resp.status_code == 201
    assert _video_state(ctx, video["id"])["annotation_revision"] == 5
    assert _delete(ctx, headers, project, video, ann["id"]).status_code == 204
    assert _video_state(ctx, video["id"])["annotation_revision"] == 6


def test_patch_empty_body_cannot_bypass_submitted_hard_lock(ctx, login_headers):
    """空 PATCH 同样不得绕过 submitted 硬锁。"""
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    ann = _annotate(ctx, headers, project, video, categories[0]["id"])
    _reach_state(ctx, headers, project, video, None, "submitted")

    resp = _patch(ctx, headers, project, video, ann["id"], {})
    assert resp.status_code == 409
    st = _video_state(ctx, video["id"])
    assert st["workflow_status"] == "submitted"
    assert st["annotation_revision"] == 2


# ---------- Clip 行与实体文件删除 ----------


def test_invalidation_deletes_clip_rows_and_files(ctx, login_headers, tmp_path):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    ann = _annotate(ctx, headers, project, video, categories[0]["id"])
    _add_clip(ctx, tmp_path, project["id"], ann["id"], "a.mp4", "a.jpg", source_revision=1)
    _add_clip(ctx, tmp_path, project["id"], ann["id"], "b.mp4", "b.jpg", source_revision=2)
    reviewer_headers, _ = _reviewer_headers(ctx, login_headers, project["id"])
    _reach_state(ctx, headers, project, video, reviewer_headers, "rejected")
    assert _count_clips(ctx, video["id"]) == 2

    assert _patch(ctx, headers, project, video, ann["id"], {"end_time": 5.0}).status_code == 200

    # Clip 行删除、实体文件删除
    assert _count_clips(ctx, video["id"]) == 0
    assert not (tmp_path / "clips" / "a.mp4").exists()
    assert not (tmp_path / "clips" / "b.mp4").exists()
    assert not (tmp_path / "thumbnails" / "a.jpg").exists()
    assert not (tmp_path / "thumbnails" / "b.jpg").exists()
    # 标注本身保留
    with ctx.session_factory() as db:
        assert db.get(Annotation, ann["id"]) is not None
    # 无清理异常日志（全部成功删除）
    assert _read_cleanup_log(ctx, tmp_path) == []


def test_invalidation_only_affects_video_own_clips(ctx, login_headers, tmp_path):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    ann1 = _annotate(ctx, headers, project, video, categories[0]["id"])
    video2 = ctx.client.post(
        f"/api/projects/{project['id']}/videos", json={"filename": "s2.mp4"}, headers=headers
    ).json()
    ann2 = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video2['id']}/annotations",
        json={
            "category_id": categories[1]["id"],
            "start_time": 1.0,
            "end_time": 3.0,
            "start_frame": 25,
            "end_frame": 75,
        },
        headers=headers,
    ).json()

    _add_clip(ctx, tmp_path, project["id"], ann1["id"], "v1.mp4", "v1.jpg")
    _add_clip(ctx, tmp_path, project["id"], ann2["id"], "v2.mp4", "v2.jpg")
    reviewer_headers, _ = _reviewer_headers(ctx, login_headers, project["id"])
    _reach_state(ctx, headers, project, video, reviewer_headers, "rejected")
    _reach_state(ctx, headers, project, video2, reviewer_headers, "rejected")

    assert _patch(ctx, headers, project, video, ann1["id"], {"end_time": 9.0}).status_code == 200

    assert _count_clips(ctx, video["id"]) == 0
    assert _count_clips(ctx, video2["id"]) == 1
    assert not (tmp_path / "clips" / "v1.mp4").exists()
    assert (tmp_path / "clips" / "v2.mp4").exists()
    assert (tmp_path / "thumbnails" / "v2.jpg").exists()


def test_review_history_preserved_on_invalidation(ctx, login_headers):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    ann = _annotate(ctx, headers, project, video, categories[0]["id"])
    reviewer_headers, _ = _reviewer_headers(ctx, login_headers, project["id"])
    _reach_state(ctx, headers, project, video, reviewer_headers, "rejected")
    assert _count_reviews(ctx, video["id"]) == 0

    assert _patch(ctx, headers, project, video, ann["id"], {"end_time": 7.0}).status_code == 200
    # Review 历史保留
    assert _count_reviews(ctx, video["id"]) == 0


# ---------- 越界路径 / 删除失败 ----------


def test_out_of_bounds_paths_not_deleted_but_recorded(ctx, login_headers, tmp_path):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    ann = _annotate(ctx, headers, project, video, categories[0]["id"])

    # 绝对路径逃逸 + 相对 ../ 穿越
    secret = tmp_path / "outside-secret.txt"
    secret.write_bytes(b"KEEP")
    traversal = tmp_path / "outside-traversal.txt"
    traversal.write_bytes(b"KEEP2")
    _add_clip(ctx, tmp_path, project["id"], ann["id"], str(secret), "ok.jpg", source_revision=1)
    _add_clip(ctx, tmp_path, project["id"], ann["id"], "../outside-traversal.txt", "ok2.jpg", source_revision=2)

    reviewer_headers, _ = _reviewer_headers(ctx, login_headers, project["id"])
    _reach_state(ctx, headers, project, video, reviewer_headers, "rejected")

    resp = _patch(ctx, headers, project, video, ann["id"], {"end_time": 5.0})
    assert resp.status_code == 200

    # 越界文件绝不删除
    assert secret.exists()
    assert traversal.exists()
    # Clip 行删除、目录内合法文件删除
    assert _count_clips(ctx, video["id"]) == 0
    assert not (tmp_path / "thumbnails" / "ok.jpg").exists()
    assert not (tmp_path / "thumbnails" / "ok2.jpg").exists()

    # 越界被记录（可观测）
    entries = _read_cleanup_log(ctx, tmp_path)
    oob = [e for e in entries if e["kind"] == "out-of-bounds"]
    assert len(oob) == 2
    for e in oob:
        assert e["clip_id"] is not None
        assert "NOT deleted" in e["message"]


def test_file_delete_failure_recorded_without_blocking(ctx, login_headers, tmp_path):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    ann = _annotate(ctx, headers, project, video, categories[0]["id"])
    # clip 路径指向一个目录 → unlink 必然失败（OSError）
    _add_clip(ctx, tmp_path, project["id"], ann["id"], "dirclip.mp4", "ok.jpg", clip_as_dir=True)
    reviewer_headers, _ = _reviewer_headers(ctx, login_headers, project["id"])
    _reach_state(ctx, headers, project, video, reviewer_headers, "rejected")

    resp = _patch(ctx, headers, project, video, ann["id"], {"end_time": 5.0})
    assert resp.status_code == 200  # 删除失败不阻断请求
    assert _count_clips(ctx, video["id"]) == 0  # DB 记录已删
    assert (tmp_path / "clips" / "dirclip.mp4").is_dir()  # 孤儿仍在磁盘（无害）
    assert not (tmp_path / "thumbnails" / "ok.jpg").exists()  # 可删除的仍删除

    entries = _read_cleanup_log(ctx, tmp_path)
    failed = [e for e in entries if e["kind"] == "delete-failed"]
    assert len(failed) == 1
    assert failed[0]["path"].endswith("dirclip.mp4")
    assert failed[0]["error"]
    assert failed[0]["annotation_id"] == ann["id"]
    assert failed[0]["revision"] == 1
    assert failed[0]["media_kind"] == "clip"
    assert failed[0]["root"] == "clips"


# ---------- 角色与直接 review_status ----------


def test_reviewer_cannot_write_annotations(ctx, login_headers):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    ann = _annotate(ctx, headers, project, video, categories[0]["id"])
    reviewer_headers, _ = _reviewer_headers(ctx, login_headers, project["id"])

    # reviewer 创建 → 403
    resp = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations",
        json={
            "category_id": categories[1]["id"],
            "start_time": 1.0,
            "end_time": 3.0,
            "start_frame": 25,
            "end_frame": 75,
        },
        headers=reviewer_headers,
    )
    assert resp.status_code == 403
    # reviewer 修改 / 删除 → 403
    assert _patch(ctx, reviewer_headers, project, video, ann["id"], {"end_time": 9.0}).status_code == 403
    assert _delete(ctx, reviewer_headers, project, video, ann["id"]).status_code == 403


def test_direct_review_status_writes_rejected_422(ctx, login_headers):
    setup = ctx.make_project_with_video()
    headers, project, categories, video = (
        setup["headers"],
        setup["project"],
        setup["categories"],
        setup["video"],
    )
    base = f"/api/projects/{project['id']}/videos/{video['id']}/annotations"

    # 创建时直接写非 pending 审核状态 → 422
    payload = {
        "category_id": categories[0]["id"],
        "start_time": 1.0,
        "end_time": 3.0,
        "start_frame": 25,
        "end_frame": 75,
        "review_status": "approved",
    }
    assert ctx.client.post(base, json=payload, headers=headers).status_code == 422

    # PATCH 直接写 review_status → 422（任意值，包括 pending）
    ann = _annotate(ctx, headers, project, video, categories[0]["id"])
    assert _patch(ctx, headers, project, video, ann["id"], {"review_status": "rejected"}).status_code == 422
    assert _patch(ctx, headers, project, video, ann["id"], {"review_status": "pending"}).status_code == 422

    # 创建固定 pending；显式传 "pending" 与缺省等价
    payload["review_status"] = "pending"
    assert ctx.client.post(base, json=payload, headers=headers).status_code == 201
