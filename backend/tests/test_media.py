"""验收（批次 4）：精确片段重编码与缩略图后台任务。

覆盖契约：
- 媒体执行器：ffmpeg 命令构造（参数列表、无 shell、超时、音频映射、stderr 截断）。
- 自动入队：approved 审核提交后创建当前 video+revision 的 dedupe 任务与每条标注
  pending Clip 并调度；rejected 不入队；生成端点幂等/重试、仅 approved、角色门。
- 单任务：dedupe_key 唯一，重复触发不产生重复任务行。
- 进度 / 失败 / 重试：成功 100；部分失败对应 Clip failed + Job failed 记录摘要、
  成功片段保留，重试只处理 pending/failed。
- 重启恢复：running 视为中断 → 重排（attempts+1）或判失败（重试上限耗尽）。
- 修订隔离：处理前/每片/完成后校验 approved + revision；失效 → 任务 cancelled、
  清理本次产物、不复活已删除 Clip。
- 文件原子性：临时文件成功后原子替换、失败清理半成品；DB 存相对路径且位于
  clips_dir/thumbnails_dir 内；输入源安全限制在 videos_dir 内（越界/缺失拒绝）。
- 全部通过注入 FakeMediaProcessor + 同步 worker，不要求本机 ffmpeg。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.media import FfmpegMediaProcessor, MediaCommandError, format_time
from app.models import Annotation, BackgroundJob, Clip, ProjectMembership, User, Video

from .conftest import auth_headers


# ---------- 辅助 ----------


def _setup(ctx, headers, *, annotations=1, start_times=None, storage_path="src.mp4", with_source=True):
    """创建项目 +（可选）源视频文件 + 元数据视频 + 标注；返回 (project, categories, video, anns)。"""
    project = ctx.client.post("/api/projects", json={"name": "媒体项目"}, headers=headers).json()
    categories = ctx.client.get(f"/api/projects/{project['id']}/categories", headers=headers).json()
    if with_source:
        src = ctx.app.state.settings.videos_dir / storage_path
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"FAKE-SOURCE-VIDEO")
    video = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={
            "filename": "src.mp4",
            "storage_path": storage_path,
            "status": "uploaded",
            "duration": 30.0,
            "fps": 25.0,
        },
        headers=headers,
    ).json()
    anns = []
    times = start_times or [1.0, 4.0, 7.0]
    for i in range(annotations):
        cat = categories[i % len(categories)]
        resp = ctx.client.post(
            f"/api/projects/{project['id']}/videos/{video['id']}/annotations",
            json={
                "category_id": cat["id"],
                "start_time": times[i],
                "end_time": times[i] + 2.0,
                "start_frame": int(times[i] * 25),
                "end_frame": int((times[i] + 2.0) * 25),
            },
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        anns.append(resp.json())
    return project, categories, video, anns


def _submit(ctx, project, video, headers=None):
    h = headers or auth_headers(ctx.client)
    return ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/submit", headers=h
    )


def _reviewer_headers(ctx, project_id, name=None):
    name = name or f"rev{project_id}"
    with ctx.session_factory() as db:
        user = db.query(User).filter(User.username == name).first()
    user_id = user.id if user is not None else ctx.create_user(name)
    ctx.add_member(project_id, user_id, role="reviewer")
    return auth_headers(ctx.client, name, "pw123")


def _review(ctx, project, video, result, headers):
    return ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/review",
        json={"result": result, "comment": "c"},
        headers=headers,
    )


def _approve(ctx, project, video):
    """提交并 approve；返回 (submit_resp, review_resp)。"""
    rev_headers = _reviewer_headers(ctx, project["id"])
    sub = _submit(ctx, project, video)
    rev = _review(ctx, project, video, "approved", rev_headers)
    assert sub.status_code == 200, sub.text
    assert rev.status_code == 200, rev.text
    return sub, rev


def _generate(ctx, project, video, headers=None):
    h = headers or auth_headers(ctx.client)
    return ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/media/generate", headers=h
    )


def _media_status(ctx, project, video, headers=None):
    h = headers or auth_headers(ctx.client)
    return ctx.client.get(
        f"/api/projects/{project['id']}/videos/{video['id']}/media-status", headers=h
    )


def _clips(ctx, project_id=None) -> list[Clip]:
    with ctx.session_factory() as db:
        q = db.query(Clip).order_by(Clip.id)
        if project_id is not None:
            q = q.filter(Clip.project_id == project_id)
        return q.all()


def _jobs(ctx, project_id=None) -> list[BackgroundJob]:
    with ctx.session_factory() as db:
        q = db.query(BackgroundJob).order_by(BackgroundJob.id)
        if project_id is not None:
            q = q.filter(BackgroundJob.project_id == project_id)
        return q.all()


# ---------- 媒体执行器：命令构造 / 无 shell / 超时 / 音频 / stderr 截断 ----------


def _proc(**overrides) -> FfmpegMediaProcessor:
    kwargs = {
        "ffmpeg_path": "ffmpeg",
        "ffprobe_path": "ffprobe",
        "crf": 23,
        "preset": "veryfast",
        "timeout_seconds": 30,
        "map_audio": False,
    }
    kwargs.update(overrides)
    return FfmpegMediaProcessor(**kwargs)


def test_format_time():
    assert format_time(0.0) == "0"
    assert format_time(1.0) == "1"
    assert format_time(1.5) == "1.5"
    assert format_time(2.25) == "2.25"
    assert format_time(3.0) == "3"


def test_clip_command_is_argument_list_no_shell(monkeypatch):
    proc = _proc(crf=23, preset="veryfast", timeout_seconds=45)
    cmd = proc.build_clip_command("C:/in.mp4", 1.5, 3.0, "C:/out.mp4")
    assert cmd[0] == "ffmpeg"
    assert "-y" in cmd
    assert cmd[cmd.index("-ss") + 1] == "1.5"
    assert cmd[cmd.index("-to") + 1] == "3"
    assert cmd[cmd.index("-i") + 1] == "C:/in.mp4"
    assert "-c:v" in cmd and "libx264" in cmd[cmd.index("-c:v") + 1]
    assert "veryfast" in cmd
    assert "23" in cmd
    assert "-pix_fmt" in cmd and "yuv420p" in cmd[cmd.index("-pix_fmt") + 1]
    assert "-movflags" in cmd and "+faststart" in cmd[cmd.index("-movflags") + 1]
    assert cmd[-1] == "C:/out.mp4"

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        import subprocess as sp

        return sp.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("app.media.subprocess.run", fake_run)
    proc.render_clip(input_path="in.mp4", start=0.0, end=1.0, output_path="out.mp4")
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == 45
    assert isinstance(captured["cmd"], list)
    assert all(isinstance(x, str) for x in captured["cmd"])


def test_thumbnail_command_is_argument_list():
    proc = _proc()
    cmd = proc.build_thumbnail_command("in.mp4", 2.0, "th.jpg")
    assert cmd[0] == "ffmpeg"
    assert cmd[cmd.index("-ss") + 1] == "2"
    assert "-frames:v" in cmd and cmd[cmd.index("-frames:v") + 1] == "1"
    assert "-q:v" in cmd
    assert cmd[-1] == "th.jpg"


def test_optional_audio_mapping():
    proc = _proc(map_audio=True)
    cmd = proc.build_clip_command("in.mp4", 0.0, 1.0, "out.mp4")
    assert cmd.count("-map") == 2
    assert "0:v:0" in cmd and "0:a:0?" in cmd
    assert "-c:a" in cmd and "aac" in cmd[cmd.index("-c:a") + 1]
    # 默认不映射音频
    assert _proc().build_clip_command("in.mp4", 0.0, 1.0, "out.mp4").count("-map") == 1


def test_media_error_truncates_stderr(monkeypatch):
    proc = _proc()

    def fake_run(cmd, **kwargs):
        import subprocess as sp

        return sp.CompletedProcess(cmd, 1, "", "E" * 10000)

    monkeypatch.setattr("app.media.subprocess.run", fake_run)
    with pytest.raises(MediaCommandError) as exc:
        proc.render_clip(input_path="in.mp4", start=0.0, end=1.0, output_path="out.mp4")
    msg = str(exc.value)
    assert "Media command failed (exit 1)" in msg
    assert "truncated" in msg
    assert len(msg) < 5000


def test_media_command_timeout(monkeypatch):
    proc = _proc(timeout_seconds=5)

    def fake_run(cmd, **kwargs):
        import subprocess as sp

        raise sp.TimeoutExpired(cmd, timeout=kwargs["timeout"])

    monkeypatch.setattr("app.media.subprocess.run", fake_run)
    with pytest.raises(MediaCommandError, match="timed out after 5s"):
        proc.render_clip(input_path="in.mp4", start=0.0, end=1.0, output_path="out.mp4")


def test_missing_executable_reports_clearly():
    proc = _proc(ffmpeg_path="definitely-not-a-real-ffmpeg-binary-xyz123")
    with pytest.raises(MediaCommandError, match="Media executable not found"):
        proc.render_clip(input_path="in.mp4", start=0.0, end=1.0, output_path="out.mp4")


# ---------- 自动入队 / 生成 / 状态 ----------


def test_approval_auto_enqueues_and_generates_clips(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, _anns = _setup(ctx, headers, annotations=1)

    _approve(ctx, project, video)

    jobs = _jobs(ctx, project["id"])
    assert len(jobs) == 1  # 单任务
    job = jobs[0]
    assert job.job_type == "media"
    assert job.status == "succeeded"
    assert job.progress == 100
    assert job.dedupe_key == f"media:video:{video['id']}:rev:1"
    assert job.payload == {
        "video_id": video["id"],
        "project_id": project["id"],
        "revision": 1,
    }

    clips = _clips(ctx, project["id"])
    assert len(clips) == 1
    clip = clips[0]
    assert clip.status == "ready"
    assert clip.source_revision == 1
    assert clip.clip_path == f"clip_{clip.annotation_id}_rev1.mp4"
    assert clip.thumbnail_path == f"clip_{clip.annotation_id}_rev1.jpg"
    assert clip.error is None
    # 实体文件已生成（相对路径位于 clips_dir/thumbnails_dir 内）
    clips_dir = ctx.app.state.settings.clips_dir
    thumbs_dir = ctx.app.state.settings.thumbnails_dir
    assert (clips_dir / clip.clip_path).is_file()
    assert (thumbs_dir / clip.thumbnail_path).is_file()
    assert (clips_dir / clip.clip_path).resolve().is_relative_to(clips_dir.resolve())
    assert (thumbs_dir / clip.thumbnail_path).resolve().is_relative_to(thumbs_dir.resolve())
    # 缩略图取片段中点（1.0-3.0 → at=2.0）
    _input, at, _out = ctx.processor.thumb_calls[0]
    assert at == 2.0


def test_rejected_review_does_not_enqueue(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, _anns = _setup(ctx, headers, annotations=1)
    rev_headers = _reviewer_headers(ctx, project["id"])
    assert _submit(ctx, project, video).status_code == 200
    assert _review(ctx, project, video, "rejected", rev_headers).status_code == 200

    assert _jobs(ctx, project["id"]) == []
    assert _clips(ctx, project["id"]) == []
    body = _media_status(ctx, project, video).json()
    assert body["total"] == 0
    assert body["latest_job"] is None


def test_generate_idempotent_returns_same_job(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, _anns = _setup(ctx, headers, annotations=1)
    _approve(ctx, project, video)
    job_id = _jobs(ctx, project["id"])[0].id

    resp = _generate(ctx, project, video)
    assert resp.status_code == 200
    assert resp.json()["id"] == job_id  # 幂等：同一任务
    assert resp.json()["status"] == "succeeded"
    assert len(_jobs(ctx, project["id"])) == 1  # 不产生重复任务


def test_generate_after_failure_requeues_same_job(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, anns = _setup(ctx, headers, annotations=1)
    ctx.processor.fail_clips.add(anns[0]["id"])
    _approve(ctx, project, video)
    job = _jobs(ctx, project["id"])[0]
    assert job.status == "failed"
    job_id = job.id

    # 修复后重试：同一任务行回到 queued → succeeded
    ctx.processor.fail_clips.clear()
    resp = _generate(ctx, project, video)
    assert resp.status_code == 200
    assert resp.json()["id"] == job_id
    after = _jobs(ctx, project["id"])
    assert len(after) == 1
    assert after[0].status == "succeeded"
    assert _clips(ctx, project["id"])[0].status == "ready"


def test_generate_requires_approved(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, _anns = _setup(ctx, headers, annotations=1)

    # draft → 400
    assert _generate(ctx, project, video).status_code == 400
    # submitted → 400
    rev_headers = _reviewer_headers(ctx, project["id"])
    assert _submit(ctx, project, video).status_code == 200
    assert _generate(ctx, project, video).status_code == 400
    # rejected → 400
    assert _review(ctx, project, video, "rejected", rev_headers).status_code == 200
    assert _generate(ctx, project, video).status_code == 400


def test_generate_role_gate(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, _anns = _setup(ctx, headers, annotations=1)
    _approve(ctx, project, video)

    # annotator → 403
    uid = ctx.create_user("ann_only")
    ctx.add_member(project["id"], uid, role="annotator")
    assert _generate(ctx, project, video, auth_headers(ctx.client, "ann_only", "pw123")).status_code == 403
    # 非成员 → 403
    ctx.create_user("outsider_g")
    assert _generate(ctx, project, video, auth_headers(ctx.client, "outsider_g", "pw123")).status_code == 403
    # owner → 200（幂等返回已成功任务）
    assert _generate(ctx, project, video).status_code == 200


def test_media_status_success_and_counts(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, _anns = _setup(ctx, headers, annotations=2)
    _approve(ctx, project, video)

    resp = _media_status(ctx, project, video)
    assert resp.status_code == 200
    body = resp.json()
    assert body["video_id"] == video["id"]
    assert body["revision"] == 1
    assert body["workflow_status"] == "approved"
    assert body["total"] == 2
    assert body["ready"] == 2
    assert body["processing"] == 0
    assert body["failed"] == 0
    assert body["pending"] == 0
    assert body["latest_job"]["status"] == "succeeded"
    assert body["latest_job"]["progress"] == 100


def test_media_status_permissions(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, _anns = _setup(ctx, headers, annotations=1)
    _approve(ctx, project, video)

    # 项目成员（annotator）可读
    uid = ctx.create_user("member_ann")
    ctx.add_member(project["id"], uid, role="annotator")
    assert _media_status(ctx, project, video, auth_headers(ctx.client, "member_ann", "pw123")).status_code == 200
    # 非成员 → 403
    ctx.create_user("outsider_s")
    assert _media_status(ctx, project, video, auth_headers(ctx.client, "outsider_s", "pw123")).status_code == 403
    # 跨项目视频 → 404
    other = ctx.client.post("/api/projects", json={"name": "其他项目"}, headers=headers).json()
    assert ctx.client.get(
        f"/api/projects/{other['id']}/videos/{video['id']}/media-status", headers=headers
    ).status_code == 404


def test_inactive_member_cannot_access_media_export_or_jobs(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, _anns = _setup(ctx, headers, annotations=1)
    _approve(ctx, project, video)
    job_id = _jobs(ctx, project["id"])[0].id
    user_id = ctx.create_user("inactive-owner")
    ctx.add_member(project["id"], user_id, role="owner")
    with ctx.session_factory() as db:
        membership = db.query(ProjectMembership).filter_by(
            project_id=project["id"], user_id=user_id
        ).one()
        membership.status = "inactive"
        db.commit()
    inactive = auth_headers(ctx.client, "inactive-owner", "pw123")
    urls = [
        f"/api/projects/{project['id']}/videos/{video['id']}/media-status",
        f"/api/projects/{project['id']}/jobs/{job_id}",
        f"/api/projects/{project['id']}/export/status",
        f"/api/projects/{project['id']}/export/download",
    ]
    assert all(ctx.client.get(url, headers=inactive).status_code == 403 for url in urls)
    assert _generate(ctx, project, video, inactive).status_code == 403
    assert ctx.client.post(
        f"/api/projects/{project['id']}/export", json={}, headers=inactive
    ).status_code == 403


def test_missing_ready_entity_is_reported_and_regenerated(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, _anns = _setup(ctx, headers, annotations=1)
    _approve(ctx, project, video)
    clip = _clips(ctx, project["id"])[0]
    (ctx.app.state.settings.thumbnails_dir / clip.thumbnail_path).unlink()
    calls_before = len(ctx.processor.clip_calls)

    status = _media_status(ctx, project, video).json()
    assert status["ready"] == 0
    assert status["pending"] == 1
    response = _generate(ctx, project, video)
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert len(ctx.processor.clip_calls) == calls_before + 1
    rebuilt = _clips(ctx, project["id"])[0]
    assert rebuilt.status == "ready"
    assert (ctx.app.state.settings.clips_dir / rebuilt.clip_path).is_file()
    assert (ctx.app.state.settings.thumbnails_dir / rebuilt.thumbnail_path).is_file()


def test_get_job_endpoint(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, _anns = _setup(ctx, headers, annotations=1)
    _approve(ctx, project, video)
    job_id = _jobs(ctx, project["id"])[0].id

    resp = ctx.client.get(f"/api/projects/{project['id']}/jobs/{job_id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == job_id
    assert body["job_type"] == "media"
    assert body["status"] == "succeeded"
    assert body["project_id"] == project["id"]
    # 跨项目 → 404；非成员 → 403；不存在 → 404
    other = ctx.client.post("/api/projects", json={"name": "任务其他项目"}, headers=headers).json()
    assert ctx.client.get(f"/api/projects/{other['id']}/jobs/{job_id}", headers=headers).status_code == 404
    ctx.create_user("outsider_j")
    assert ctx.client.get(
        f"/api/projects/{project['id']}/jobs/{job_id}",
        headers=auth_headers(ctx.client, "outsider_j", "pw123"),
    ).status_code == 403
    assert ctx.client.get(
        f"/api/projects/{project['id']}/jobs/999999", headers=headers
    ).status_code == 404


# ---------- 进度 / 失败 / 重试 ----------


def test_progress_tracks_partial_completion(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, anns = _setup(ctx, headers, annotations=3)
    ctx.processor.fail_clips.add(anns[1]["id"])
    _approve(ctx, project, video)

    job = _jobs(ctx, project["id"])[0]
    assert job.status == "failed"
    assert job.progress == 33  # 1/3 就绪
    with ctx.session_factory() as db:
        assert db.query(Clip).filter(Clip.status == "ready").count() == 1
        assert db.query(Clip).filter(Clip.status == "failed").count() == 1
        assert db.query(Clip).filter(Clip.status == "pending").count() == 1


def test_partial_failure_keeps_successful_and_retry_completes(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, anns = _setup(ctx, headers, annotations=2)
    ctx.processor.fail_clips.add(anns[1]["id"])
    _approve(ctx, project, video)

    job = _jobs(ctx, project["id"])[0]
    assert job.status == "failed"
    assert "1 clip(s) failed" in job.error
    clips = _clips(ctx, project["id"])
    assert clips[0].status == "ready"  # 成功片段保留
    assert clips[1].status == "failed"
    clips_dir = ctx.app.state.settings.clips_dir
    assert len(list(clips_dir.glob("*.mp4"))) == 1  # 仅成功的那片落盘

    # 重试：只处理 failed（成功片段跳过）
    calls_before = len(ctx.processor.clip_calls)
    ctx.processor.fail_clips.clear()
    resp = _generate(ctx, project, video)
    assert resp.status_code == 200
    job = _jobs(ctx, project["id"])[0]
    assert job.status == "succeeded"
    assert job.progress == 100
    assert [c.status for c in _clips(ctx, project["id"])] == ["ready", "ready"]
    assert len(ctx.processor.clip_calls) == calls_before + 1  # 只重试失败的那片


def test_processor_receives_resolved_input_and_times(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, _anns = _setup(ctx, headers, annotations=1, start_times=[1.0])
    _approve(ctx, project, video)

    input_path, start, end, _out = ctx.processor.clip_calls[0]
    videos_dir = ctx.app.state.settings.videos_dir.resolve()
    assert Path(input_path).is_relative_to(videos_dir)  # 输入限制在 videos_dir 内
    assert Path(input_path) == (videos_dir / "src.mp4").resolve()
    assert start == 1.0
    assert end == 3.0


# ---------- 文件原子性 / 路径安全 ----------


def test_success_atomic_no_temp_left(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, _anns = _setup(ctx, headers, annotations=1)
    _approve(ctx, project, video)

    clips_dir = ctx.app.state.settings.clips_dir
    thumbs_dir = ctx.app.state.settings.thumbnails_dir
    assert list(clips_dir.glob("*.part")) == []
    assert list(thumbs_dir.glob("*.part")) == []
    assert len(list(clips_dir.glob("*.mp4"))) == 1
    assert len(list(thumbs_dir.glob("*.jpg"))) == 1


def test_failure_cleans_partial_outputs(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, anns = _setup(ctx, headers, annotations=2)
    ctx.processor.fail_thumbnails.add(anns[0]["id"])  # 视频成功、缩略图失败 → 半成品清理
    _approve(ctx, project, video)

    clips_dir = ctx.app.state.settings.clips_dir
    thumbs_dir = ctx.app.state.settings.thumbnails_dir
    assert list(clips_dir.glob("*.mp4")) == []
    assert list(clips_dir.glob("*.part")) == []
    assert list(thumbs_dir.glob("*.jpg")) == []
    assert list(thumbs_dir.glob("*.part")) == []
    with ctx.session_factory() as db:
        clip = db.query(Clip).filter(Clip.annotation_id == anns[0]["id"]).one()
        assert clip.status == "failed"
        assert clip.clip_path is None
        assert clip.thumbnail_path is None
        assert "thumbnail failed" in (clip.error or "")


def test_path_escape_rejected(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    outside = ctx.app.state.settings.data_dir / "outside.mp4"
    outside.write_bytes(b"SECRET-ORIGINAL")
    project, _categories, video, _anns = _setup(
        ctx, headers, annotations=1, storage_path="../outside.mp4", with_source=False
    )
    _approve(ctx, project, video)

    job = _jobs(ctx, project["id"])[0]
    assert job.status == "failed"
    assert "escapes" in job.error
    with ctx.session_factory() as db:
        clip = db.query(Clip).one()
        assert clip.status == "failed"
        assert "escapes" in (clip.error or "")
    assert outside.read_bytes() == b"SECRET-ORIGINAL"  # 越界文件绝不被触碰
    assert list(ctx.app.state.settings.clips_dir.glob("*")) == []


def test_absolute_storage_path_within_videos_dir_ok(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    src = ctx.app.state.settings.videos_dir / "abs.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"X")
    project = ctx.client.post("/api/projects", json={"name": "绝对路径项目"}, headers=headers).json()
    categories = ctx.client.get(f"/api/projects/{project['id']}/categories", headers=headers).json()
    video = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "abs.mp4", "storage_path": str(src), "status": "uploaded"},
        headers=headers,
    ).json()
    resp = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations",
        json={
            "category_id": categories[0]["id"],
            "start_time": 1.0,
            "end_time": 3.0,
            "start_frame": 25,
            "end_frame": 75,
        },
        headers=headers,
    )
    assert resp.status_code == 201
    _approve(ctx, project, video)
    assert _jobs(ctx, project["id"])[0].status == "succeeded"
    assert _clips(ctx, project["id"])[0].status == "ready"


def test_missing_source_file_fails_job(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, _anns = _setup(
        ctx, headers, annotations=1, storage_path="missing.mp4", with_source=False
    )
    _approve(ctx, project, video)
    assert _jobs(ctx, project["id"])[0].status == "failed"
    with ctx.session_factory() as db:
        assert db.query(Clip).one().status == "failed"
        assert "missing on disk" in db.query(Clip).one().error


# ---------- 重启恢复 ----------


def test_restart_requeues_interrupted_running_job(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, anns = _setup(ctx, headers, annotations=1)
    with ctx.session_factory() as db:
        v = db.get(Video, video["id"])
        v.workflow_status = "approved"
        db.commit()
        db.add(
            Clip(
                project_id=project["id"],
                annotation_id=anns[0]["id"],
                source_revision=1,
                status="processing",  # 模拟任务领取 Clip 后进程崩溃
            )
        )
        job = BackgroundJob(
            project_id=project["id"],
            job_type="media",
            status="running",  # 模拟崩溃遗留
            progress=0,
            dedupe_key=f"media:video:{video['id']}:rev:1",
            payload={"video_id": video["id"], "project_id": project["id"], "revision": 1},
        )
        db.add(job)
        db.commit()
        job_id = job.id

    # 模拟重启：worker 重新启动
    ctx.app.state.media_worker.start()
    with ctx.session_factory() as db:
        job = db.get(BackgroundJob, job_id)
        assert job.status == "succeeded"  # 中断 → 重排 → 处理完成
        assert job.attempts == 2  # 中断重排 +1、领取 +1
        assert db.query(Clip).one().status == "ready"


def test_restart_marks_exhausted_running_job_failed(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, _anns = _setup(ctx, headers, annotations=1)
    with ctx.session_factory() as db:
        v = db.get(Video, video["id"])
        v.workflow_status = "approved"
        db.commit()
        job = BackgroundJob(
            project_id=project["id"],
            job_type="media",
            status="running",
            attempts=3,  # 已达重试上限（media_max_attempts=3）
            dedupe_key=f"media:video:{video['id']}:rev:1",
            payload={"video_id": video["id"], "project_id": project["id"], "revision": 1},
        )
        db.add(job)
        db.commit()
        job_id = job.id

    ctx.app.state.media_worker.start()
    with ctx.session_factory() as db:
        job = db.get(BackgroundJob, job_id)
        assert job.status == "failed"
        assert "retry limit" in job.error
        assert db.query(Clip).count() == 0  # 判失败，不再处理


# ---------- 修订隔离 / 失效竞态 ----------


def test_stale_job_cancelled_on_invalidation_without_resurrecting_clips(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, anns = _setup(ctx, headers, annotations=1)
    # 构造 approved rev1 + pending Clip + queued 任务（尚未被处理）
    with ctx.session_factory() as db:
        v = db.get(Video, video["id"])
        v.workflow_status = "approved"
        db.commit()
        db.add(
            Clip(
                project_id=project["id"],
                annotation_id=anns[0]["id"],
                source_revision=1,
                status="pending",
            )
        )
        job = BackgroundJob(
            project_id=project["id"],
            job_type="media",
            status="queued",
            dedupe_key=f"media:video:{video['id']}:rev:1",
            payload={"video_id": video["id"], "project_id": project["id"], "revision": 1},
        )
        db.add(job)
        db.commit()
        job_id = job.id

    # 失效：PATCH 标注 → draft rev2，Clip 行删除
    resp = ctx.client.patch(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations/{anns[0]['id']}",
        json={"end_time": 9.0},
        headers=headers,
    )
    assert resp.status_code == 200
    assert _clips(ctx, project["id"]) == []

    # 处理遗留任务 → 取消；不复活已删除 Clip，不产生新实体文件
    ctx.app.state.media_worker.start()
    clips_dir = ctx.app.state.settings.clips_dir
    thumbs_dir = ctx.app.state.settings.thumbnails_dir
    with ctx.session_factory() as db:
        job = db.get(BackgroundJob, job_id)
        assert job.status == "cancelled"
        assert "not 'approved'" in job.error
        assert db.query(Clip).count() == 0  # 不复活已删除的 Clip
    assert list(clips_dir.glob("*")) == []
    assert list(thumbs_dir.glob("*")) == []


def test_stale_job_cancelled_on_revision_mismatch(media_ctx):
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, anns = _setup(ctx, headers, annotations=1)
    with ctx.session_factory() as db:
        v = db.get(Video, video["id"])
        v.workflow_status = "approved"
        db.commit()
        db.add(
            Clip(
                project_id=project["id"],
                annotation_id=anns[0]["id"],
                source_revision=1,
                status="pending",
            )
        )
        job = BackgroundJob(
            project_id=project["id"],
            job_type="media",
            status="queued",
            dedupe_key=f"media:video:{video['id']}:rev:1",
            payload={"video_id": video["id"], "project_id": project["id"], "revision": 1},
        )
        db.add(job)
        db.commit()
        job_id = job.id
        # 视频修订前进到 2（仍 approved）：遗留任务修订不匹配
        v = db.get(Video, video["id"])
        v.annotation_revision = 2
        db.commit()

    ctx.app.state.media_worker.start()
    with ctx.session_factory() as db:
        job = db.get(BackgroundJob, job_id)
        assert job.status == "cancelled"
        assert "revision mismatch" in job.error
        # 不复活：clip 行保持未处理但绝不新生成
        assert db.query(Clip).count() == 1
        assert db.query(Clip).one().status == "pending"
    assert list(ctx.app.state.settings.clips_dir.glob("*")) == []


def test_worker_never_creates_clips_for_deleted_annotations(media_ctx):
    """worker 只处理已存在的 Clip 行；标注被删除后任务真空完成而非重建片段。"""
    ctx = media_ctx
    headers = auth_headers(ctx.client)
    project, _categories, video, anns = _setup(ctx, headers, annotations=1)
    with ctx.session_factory() as db:
        v = db.get(Video, video["id"])
        v.workflow_status = "approved"
        db.commit()
        job = BackgroundJob(
            project_id=project["id"],
            job_type="media",
            status="queued",
            dedupe_key=f"media:video:{video['id']}:rev:1",
            payload={"video_id": video["id"], "project_id": project["id"], "revision": 1},
        )
        db.add(job)
        db.commit()
        job_id = job.id
        # 标注已删除（无 Clip 行；模拟并发窗口：视频仍 approved、修订未前进）
        db.delete(db.get(Annotation, anns[0]["id"]))
        db.commit()

    ctx.app.state.media_worker.start()
    with ctx.session_factory() as db:
        job = db.get(BackgroundJob, job_id)
        assert job.status == "succeeded"  # 无 Clip 行可处理 → 真空成功
        assert db.query(Clip).count() == 0  # 绝不复活/重建 Clip
    assert list(ctx.app.state.settings.clips_dir.glob("*")) == []
