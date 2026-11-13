"""Environment failures are early, safe service errors, not bad-input verdicts."""
import asyncio
import json
import logging
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.config import Settings
from app.display_proxy_processor import DisplayProxyProcessor
from app.media import (FfmpegMediaProcessor, MediaCommandError, MediaDependencyError,
                       check_media_executable)
from app.media_preflight import MediaUploadGate
from app.migration import run_migrations
from app.models import Video, VideoImportBatch
from scripts.media_preflight import run_preflight
from tests.conftest import auth_headers
from tests.test_heuristic_judgment import files, setup_project, upload
from tests.test_production_migration_gate import _production_settings


@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError, OSError,
                                   subprocess.TimeoutExpired, "exit", "wrong-tool"])
def test_preflight_executes_and_redacts_failures(monkeypatch, failure):
    def run(cmd, **kwargs):
        assert cmd == ["/private/secret/ffprobe", "-version"]
        assert kwargs["shell"] is False and kwargs["timeout"] == 5
        if failure == "exit":
            return subprocess.CompletedProcess(cmd, 1, "", "credentials /private/secret")
        if failure == "wrong-tool":
            return subprocess.CompletedProcess(cmd, 0, "unrelated executable", "")
        if failure is subprocess.TimeoutExpired:
            raise failure(cmd, 5)
        raise failure("credentials /private/secret")
    monkeypatch.setattr("app.media.subprocess.run", run)
    with pytest.raises(MediaDependencyError) as caught:
        check_media_executable("/private/secret/ffprobe", "ffprobe")
    assert caught.value.detail["code"] == "media_dependency_unavailable"
    assert caught.value.detail["tool"] == "ffprobe"
    assert "/private" not in str(caught.value) and "credentials" not in str(caught.value)


@pytest.mark.parametrize("tool", ["ffmpeg", "ffprobe"])
def test_real_non_media_executable_is_rejected(tool):
    with pytest.raises(MediaDependencyError):
        check_media_executable(sys.executable, tool)


@pytest.mark.parametrize("operation,tool", [("probe", "ffprobe"), ("timestamps", "ffprobe"),
                                         ("clip", "ffprobe"), ("thumbnail", "ffmpeg"),
                                         ("display", "ffprobe")])
def test_all_media_callers_share_dependency_classification(tmp_path, operation, tool):
    missing = str(tmp_path / "private-missing-executable")
    processor = FfmpegMediaProcessor(ffmpeg_path=missing, ffprobe_path=missing)
    source = tmp_path / "input.mp4"
    source.write_bytes(b"not relevant: the executable cannot start")
    with pytest.raises(MediaDependencyError) as caught:
        if operation == "probe":
            processor.probe_clip(str(source))
        elif operation == "timestamps":
            processor._probe_frame_timestamps(str(source), source.stat().st_size, 0)
        elif operation == "clip":
            processor.render_clip(input_path=str(source), start_frame=0, frames=1,
                                  output_path="out.mp4", fps=10)
        elif operation == "thumbnail":
            processor.render_thumbnail(input_path=str(source), at=0, output_path="out.jpg")
        else:
            DisplayProxyProcessor(ffprobe_path=missing).probe(source)
    assert caught.value.detail["tool"] == tool
    assert str(tmp_path) not in str(caught.value)


def test_production_preflight_is_a_hard_gate(tmp_path):
    settings = _production_settings(tmp_path, "sqlite://")
    settings.ffmpeg_path = str(tmp_path / "missing-ffmpeg")
    output = []
    assert run_preflight(lambda: settings, output.append) == 1
    assert "media_dependency_unavailable" in output[0]
    assert str(tmp_path) not in output[0]


def test_production_startup_stops_before_workers(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    run_migrations(url)
    settings = _production_settings(tmp_path, url)
    settings.ffmpeg_path = str(tmp_path / "missing-ffmpeg")
    app = create_app(settings)
    monkeypatch.setattr(app.state.media_worker, "_recover_interrupted",
                        lambda: pytest.fail("worker recovery ran before media preflight"))
    with pytest.raises(MediaDependencyError):
        with TestClient(app):
            pass


def test_development_logs_dependency_error_and_refuses_media(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
                        ffmpeg_path="missing-media-binary-xyz", cleanup_enabled=False)
    app = create_app(settings)
    messages = []
    monkeypatch.setattr(logging.getLogger("app.main"), "error", lambda _format, exc: messages.append(str(exc)))
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        response = client.post("/api/projects/1/videos/upload", content=b"not even parsed as multipart")
        assert response.status_code == 503
        assert response.json()["detail"]["tool"] == "ffmpeg"
    assert "media_dependency_unavailable" in messages[0]


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/projects/1/videos/upload"),
    ("POST", "/api/projects/1/video-import-batches"),
    ("PUT", "/api/projects/1/video-import-batches/1/files/video"),
])
def test_upload_gate_does_not_read_body(method, path):
    processor = FfmpegMediaProcessor(ffmpeg_path="missing-media-binary-xyz")
    async def forbidden(*args):
        pytest.fail("body read or downstream application invoked")
    messages = []
    async def send(message):
        messages.append(message)
    scope = {"type": "http", "method": method, "path": path,
             "app": SimpleNamespace(state=SimpleNamespace(
                 media_worker=SimpleNamespace(processor=processor)))}
    asyncio.run(MediaUploadGate(forbidden)(scope, forbidden, send))
    assert messages[0]["status"] == 503
    assert json.loads(messages[1]["body"])["detail"]["code"] == "media_dependency_unavailable"


def test_api_rejects_before_batch_creation_and_retains_existing_batch(media_ctx):
    ctx = media_ctx
    headers, pid, _ = setup_project(ctx)
    bid = upload(ctx, headers, pid, complete=False)
    class LostRuntime(FfmpegMediaProcessor):
        def check_dependencies(self):
            pass  # Runtime disappears only during actual probe, after the early gate.
        def probe_clip(self, path, *, expected=None):
            raise MediaDependencyError("ffprobe")
    ctx.app.state.media_worker.processor = LostRuntime()
    base = f"/api/projects/{pid}/video-import-batches"
    response = ctx.client.post(f"{base}/{bid}/complete", headers=headers)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "media_dependency_unavailable"
    with ctx.session_factory() as db:
        batch = db.get(VideoImportBatch, bid)
        assert batch.status == "uploading" and batch.created_video_id is None
        assert (ctx.app.state.settings.videos_dir / batch.video_path).is_file()
        assert db.query(Video).count() == 0
    ctx.app.state.media_worker.processor = FfmpegMediaProcessor(ffmpeg_path="missing-media-binary-xyz")
    assert ctx.client.post(base, headers=headers).status_code == 503
    assert ctx.client.get("/api/health").status_code == 200
    with ctx.session_factory() as db:
        assert db.query(VideoImportBatch).count() == 1


def test_real_probe_bad_input_and_heuristic_complete(media_ctx, tmp_path):
    ffmpeg, ffprobe = os.environ.get("FFMPEG_PATH"), os.environ.get("FFPROBE_PATH")
    if not ffmpeg or not ffprobe:
        pytest.skip("Set FFMPEG_PATH / FFPROBE_PATH for real executable evidence")
    processor = FfmpegMediaProcessor(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)
    processor.check_dependencies()
    source = tmp_path / "real.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x48:r=10:d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
                   check=True, capture_output=True, timeout=20)
    assert processor.probe_clip(str(source))["frame_count"] == 10
    broken = tmp_path / "bad.mp4"
    broken.write_bytes(b"not a video")
    with pytest.raises(MediaCommandError) as caught:
        processor.probe_clip(str(broken))
    assert not isinstance(caught.value, MediaDependencyError)
    assert str(tmp_path) not in str(caught.value)
    ctx = media_ctx
    ctx.app.state.media_worker.processor = processor
    headers, pid, _ = setup_project(ctx)
    content = files()
    content["video"] = source.read_bytes()
    bid, result = upload(ctx, headers, pid, content)
    assert result["status"] == "ready"
    assert ctx.client.get(f"/api/projects/{pid}/videos/{result['video_id']}/judgment",
                          headers=headers).json()["workflow_status"] == "draft"
    content["video"] = broken.read_bytes()
    bid, result = upload(ctx, headers, pid, content)
    assert result["status"] == "failed"
    assert result["validation_errors"]["code"] == "invalid_heuristic_input"
    assert result["validation_errors"]["validation_errors"] == ["Input MP4 probe failed"]
