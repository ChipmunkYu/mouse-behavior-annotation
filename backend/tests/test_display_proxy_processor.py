import json
import shutil
import subprocess

import pytest

from app.display_proxy_processor import (DISPLAY_PROXY_PROFILE_VERSION, DisplayProxyError,
                                         DisplayProxyProcessor, UnsupportedDisplaySource,
                                         sanitize_error)


def _probe(*, width=1280, height=720, fps="30/1", codec="h264", pix="yuv420p",
           audio=False, frames="300", duration="10.0", sar="1:1", rotation=None,
           time_base="1/15360"):
    streams = [{"codec_type": "video", "codec_name": codec, "pix_fmt": pix,
                "width": width, "height": height, "avg_frame_rate": fps,
                "r_frame_rate": fps, "nb_read_frames": frames, "duration": duration,
                "sample_aspect_ratio": sar, "time_base": time_base}]
    if rotation is not None:
        streams[0]["side_data_list"] = [
            {"side_data_type": "Display Matrix", "rotation": rotation}
        ]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac"})
    return {"streams": streams, "format": {"format_name": "mov,mp4,m4a", "duration": duration}}


def test_candidate_command_snapshot_has_fixed_timing_and_no_r():
    processor = DisplayProxyProcessor(ffmpeg_path="ffmpeg-x")
    command = processor.transcode_command("IN", "OUT")
    assert command == ["ffmpeg-x", "-hide_banner", "-nostdin", "-y", "-i", "IN",
                       "-map", "0:v:0", "-map_metadata", "-1", "-an", "-vf",
                       "scale=1280:720:flags=lanczos,setsar=1",
                       "-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
                       "-pix_fmt", "yuv420p", "-g", "30", "-keyint_min", "30",
                       "-sc_threshold", "0", "-vsync", "0", "-movflags", "+faststart",
                       "-f", "mp4", "OUT"]
    assert "-r" not in command
    assert DISPLAY_PROXY_PROFILE_VERSION == "candidate-720p-h264-crf28-g30-sar1"


def test_frame_timestamp_probe_requests_frame_sections(monkeypatch):
    processor = DisplayProxyProcessor(ffprobe_path="ffprobe-x")
    captured = []
    monkeypatch.setattr(
        processor,
        "_run",
        lambda command, *paths: (
            captured.append(command)
            or subprocess.CompletedProcess(command, 0, '{"frames": []}', "")
        ),
    )
    assert processor.probe_frame_timestamps("source.mp4") == ()
    assert "-show_frames" in captured[0]
    assert captured[0][captured[0].index("-show_entries") + 1].startswith("frame=")


def test_source_rejects_non_16_by_9_and_non_30fps_rates():
    with pytest.raises(UnsupportedDisplaySource, match="16:9"):
        DisplayProxyProcessor._metrics(_probe(width=640, height=480), output=False)
    document = _probe()
    document["streams"][0]["r_frame_rate"] = "24/1"
    with pytest.raises(UnsupportedDisplaySource, match="approximately 30fps"):
        DisplayProxyProcessor._metrics(document, output=False)


def test_source_metrics_accepts_realistic_30fps_vfr_rates():
    document = _probe(fps="27010000/899917", frames="5402", duration="179.9834")
    document["streams"][0]["r_frame_rate"] = "30/1"
    fps, duration, frames = DisplayProxyProcessor._metrics(document, output=False)
    assert fps == pytest.approx(30.013878)
    assert (duration, frames) == (179.9834, 5402)


@pytest.mark.parametrize("change", [{"sar": "4:3"}, {"rotation": 90}])
def test_source_rejects_non_square_sar_and_rotation(change):
    with pytest.raises(UnsupportedDisplaySource):
        DisplayProxyProcessor._metrics(_probe(**change), output=False)


@pytest.mark.parametrize("change", [
    {"codec": "hevc"}, {"pix": "yuv444p"}, {"width": 640}, {"audio": True},
    {"frames": "200"}, {"sar": "4:3"}, {"rotation": -90},
])
def test_output_probe_is_fail_closed(change):
    with pytest.raises(DisplayProxyError):
        DisplayProxyProcessor._metrics(_probe(**change), output=True)


def test_probe_bad_json_and_path_redaction(monkeypatch, tmp_path):
    source = tmp_path / "secret" / "input.mp4"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "{", ""))
    with pytest.raises(DisplayProxyError, match="invalid JSON"):
        DisplayProxyProcessor().probe(source)
    assert str(tmp_path) not in sanitize_error(f"failed at {source}", source)


def _timestamps(count=300, period=1 / 30):
    return tuple(index * period for index in range(count))


def test_timestamp_validation_accepts_bounded_vfr_with_matching_summary_fps():
    values = list(_timestamps())
    values[100] = values[99] + 0.0294
    values[151] = values[150] + 0.048133
    DisplayProxyProcessor._validate_timestamps(tuple(values), (30.0, 10.0, 300),
                                               time_base=1 / 15360, output=False,
                                               nominal_fps=30.0)


def test_timestamp_validation_rejects_interval_outside_vfr_bounds():
    values = list(_timestamps())
    values[151] += 0.02
    with pytest.raises(UnsupportedDisplaySource, match="VFR bounds"):
        DisplayProxyProcessor._validate_timestamps(tuple(values), (30.0, 10.0, 300),
                                                   time_base=1 / 15360, output=False)


def test_timestamp_validation_accepts_normal_30fps():
    DisplayProxyProcessor._validate_timestamps(_timestamps(), (30.0, 10.0, 300),
                                               time_base=1 / 15360, output=False)


def test_output_timestamp_validation_rejects_non_monotonic_values():
    values = list(_timestamps())
    values[100] = values[99]
    with pytest.raises(DisplayProxyError, match="strictly monotonic"):
        DisplayProxyProcessor._validate_timestamps(tuple(values), (30.0, 10.0, 300),
                                                   time_base=1 / 15360, output=True)


def test_timestamp_mapping_normalizes_origin_and_rejects_drift():
    source = _timestamps()
    shifted = tuple(value + 2.5 for value in source)
    DisplayProxyProcessor._compare_timestamps(source, shifted,
                                              source_time_base=1 / 15360,
                                              output_time_base=1 / 15360)
    drifted = list(shifted)
    drifted[200] += 0.003
    with pytest.raises(DisplayProxyError, match="drift"):
        DisplayProxyProcessor._compare_timestamps(source, tuple(drifted),
                                                  source_time_base=1 / 15360,
                                                  output_time_base=1 / 15360)


def test_render_probes_transcodes_then_fully_decodes(monkeypatch):
    calls = []
    documents = iter([_probe(), _probe()])
    processor = DisplayProxyProcessor()
    monkeypatch.setattr(processor, "probe", lambda _path: next(documents))
    monkeypatch.setattr(processor, "probe_frame_timestamps", lambda _path: _timestamps())
    monkeypatch.setattr(processor, "_run", lambda command, *paths: calls.append(command))
    monkeypatch.setattr(processor, "_validate_faststart", lambda path: calls.append(["faststart", path]))
    processor.render(input_path="in.mp4", output_path="out.part")
    assert calls[0] == processor.transcode_command("in.mp4", "out.part")
    assert calls[1] == ["faststart", "out.part"]
    assert calls[2][-4:] == ["0:v:0", "-f", "null", "-"]


@pytest.mark.parametrize(("output", "message"), [
    (_probe(frames="299"), "frame count"),
    (_probe(fps="30020/1000"), "FPS"),
    (_probe(duration="10.04"), "duration"),
])
def test_render_rejects_output_timing_drift(monkeypatch, output, message):
    processor = DisplayProxyProcessor()
    documents = iter([_probe(), output])
    monkeypatch.setattr(processor, "probe", lambda _path: next(documents))
    monkeypatch.setattr(processor, "probe_frame_timestamps", lambda _path: _timestamps())
    monkeypatch.setattr(processor, "_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(processor, "_validate_faststart", lambda _path: None)
    with pytest.raises(DisplayProxyError, match=message):
        processor.render(input_path="in.mp4", output_path="out.part")


def _atom(kind: bytes, payload: bytes = b"") -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def test_faststart_accepts_moov_before_mdat(tmp_path):
    path = tmp_path / "fast.mp4"
    path.write_bytes(_atom(b"ftyp") + _atom(b"moov") + _atom(b"mdat", b"data"))
    DisplayProxyProcessor._validate_faststart(path)


def test_faststart_rejects_moov_after_mdat(tmp_path):
    path = tmp_path / "slow.mp4"
    path.write_bytes(_atom(b"ftyp") + _atom(b"mdat", b"data") + _atom(b"moov"))
    with pytest.raises(DisplayProxyError, match="faststart"):
        DisplayProxyProcessor._validate_faststart(path)


def test_faststart_rejects_truncated_atom(tmp_path):
    path = tmp_path / "truncated.mp4"
    path.write_bytes(_atom(b"moov") + (20).to_bytes(4, "big") + b"mdat" + b"short")
    with pytest.raises(DisplayProxyError, match="truncated"):
        DisplayProxyProcessor._validate_faststart(path)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="local ffmpeg/ffprobe unavailable")
def test_real_ffmpeg_candidate_round_trip(tmp_path):
    source, output = tmp_path / "source.mp4", tmp_path / "proxy.part"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=1",
                    "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
                   shell=False, check=True)
    DisplayProxyProcessor(timeout_seconds=60).render(input_path=str(source),
                                                      output_path=str(output))
    assert output.is_file() and output.stat().st_size > 0
