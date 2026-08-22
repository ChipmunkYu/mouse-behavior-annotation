"""Real FFmpeg checks for direct rendering plus one complete MediaWorker DB run."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.media import FfmpegMediaProcessor, MediaCommandError
from app.media_jobs import media_dedupe_key, render_submission_clip_files
from app.models import BackgroundJob, Clip, Video
from tests.conftest import auth_headers
from tests.test_media import _setup


@pytest.fixture(scope="module")
def ffmpeg_path():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("real FFmpeg integration skipped: ffmpeg is not available on PATH")
    return ffmpeg


@pytest.fixture(scope="module")
def ffprobe_path():
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        pytest.skip("real media probe skipped: ffprobe is not available on PATH")
    return ffprobe


def _run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, timeout=60, shell=False)
    assert result.returncode == 0, result.stderr


def _probe(ffprobe: str, path: Path) -> dict:
    result = subprocess.run([
        ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
        "-show_entries",
        "stream=codec_name,pix_fmt,width,height,avg_frame_rate,nb_read_frames:format=format_name,duration",
        "-of", "json", str(path),
    ], capture_output=True, text=True, timeout=30, shell=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _source(ffmpeg: str, path: Path, fps: int, *, duration: int = 1) -> None:
    _run([
        ffmpeg, "-y", "-f", "lavfi", "-i",
        f"testsrc=size=322x242:rate={fps}:duration={duration}", "-an", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "-f", "mp4", str(path),
    ])


def _entities(tmp_path: Path, fps: int):
    settings = SimpleNamespace(clips_dir=tmp_path / "clips", thumbnails_dir=tmp_path / "thumbs")
    snapshot = SimpleNamespace(fps=float(fps), frame_count=fps, width=322, height=242)
    submission = SimpleNamespace(id=7, detection_snapshot=snapshot)
    annotation = SimpleNamespace(
        id=9, submission_id=7, start_time=5 / fps, end_time=15 / fps,
        start_frame=5, end_frame=14, crop_region={"x": 10, "y": 12, "w": 300, "h": 200},
    )
    clip = SimpleNamespace(annotation_id=None, source_revision=None, clip_path=None,
                           thumbnail_path=None, status="pending", error=None,
                           generated_at=None, updated_at=None)
    return settings, submission, annotation, clip


@pytest.mark.parametrize("fps", [25, 30, 60])
def test_real_submission_render_part_publish_and_probe(tmp_path, ffmpeg_path, ffprobe_path, fps):
    ffmpeg, ffprobe = ffmpeg_path, ffprobe_path
    source = tmp_path / f"source-{fps}.mp4"
    _source(ffmpeg, source, fps)
    settings, submission, annotation, clip = _entities(tmp_path, fps)
    processor = FfmpegMediaProcessor(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe,
                                     timeout_seconds=60, map_audio=False)

    created = render_submission_clip_files(
        processor, settings, submission, annotation, clip, input_path=source)

    assert clip.status == "ready" and len(created) == 2
    video = _probe(ffprobe, settings.clips_dir / clip.clip_path)
    stream, media_format = video["streams"][0], video["format"]
    numerator, denominator = map(int, stream["avg_frame_rate"].split("/"))
    assert stream["codec_name"] == "h264" and stream["pix_fmt"] == "yuv420p"
    assert "mp4" in media_format["format_name"]
    assert (stream["width"], stream["height"]) == (300, 200)
    assert numerator / denominator == pytest.approx(fps)
    assert int(stream["nb_read_frames"]) == 10
    assert float(media_format["duration"]) == pytest.approx(10 / fps, abs=1 / fps)
    image = _probe(ffprobe, settings.thumbnails_dir / clip.thumbnail_path)
    assert image["streams"][0]["codec_name"] == "mjpeg"
    assert (image["streams"][0]["width"], image["streams"][0]["height"]) == (300, 200)
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob("*.staging"))


def test_real_clip_then_thumbnail_failure_publishes_no_final(tmp_path, ffmpeg_path):
    ffmpeg = ffmpeg_path
    source = tmp_path / "source.mp4"
    _source(ffmpeg, source, 25)
    settings, submission, annotation, clip = _entities(tmp_path, 25)

    class ThumbnailFailure(FfmpegMediaProcessor):
        def render_thumbnail(self, **kwargs):
            raise MediaCommandError("injected thumbnail failure after real clip encode")

    processor = ThumbnailFailure(ffmpeg_path=ffmpeg, timeout_seconds=60)
    with pytest.raises(MediaCommandError, match="thumbnail failure"):
        render_submission_clip_files(
            processor, settings, submission, annotation, clip, input_path=source)
    assert not list(settings.clips_dir.glob("*"))
    assert not list(settings.thumbnails_dir.glob("*"))


def test_real_processor_complete_media_worker_updates_job_and_clip(media_ctx, ffmpeg_path):
    """Unlike the direct-render tests above, this exercises worker claim and DB transitions."""
    ctx = media_ctx
    ffmpeg = ffmpeg_path
    headers = auth_headers(ctx.client)
    project, _categories, video_data, annotations = _setup(
        ctx, headers, annotations=1, start_times=[1.0])
    source = ctx.app.state.settings.videos_dir / "src.mp4"
    _source(ffmpeg, source, 25, duration=4)
    with ctx.session_factory() as db:
        video = db.get(Video, video_data["id"])
        video.workflow_status = "approved"
        clip = Clip(project_id=project["id"], annotation_id=annotations[0]["id"],
                    source_revision=video.media_revision, status="pending")
        job = BackgroundJob(
            project_id=project["id"], job_type="media", status="queued",
            dedupe_key=media_dedupe_key(video.id, video.media_revision),
            payload={"video_id": video.id, "project_id": project["id"],
                     "revision": video.media_revision},
        )
        db.add_all([clip, job]); db.commit()
        clip_id, job_id = clip.id, job.id

    worker = ctx.app.state.media_worker
    original = worker.processor
    worker.processor = FfmpegMediaProcessor(
        ffmpeg_path=ffmpeg, timeout_seconds=60, map_audio=False)
    try:
        worker._run_job(job_id)
    finally:
        worker.processor = original

    with ctx.session_factory() as db:
        job, clip = db.get(BackgroundJob, job_id), db.get(Clip, clip_id)
        assert job.status == "succeeded" and job.progress == 100 and job.attempts == 1
        assert clip.status == "ready"
        assert (ctx.app.state.settings.clips_dir / clip.clip_path).is_file()
        assert (ctx.app.state.settings.thumbnails_dir / clip.thumbnail_path).is_file()
