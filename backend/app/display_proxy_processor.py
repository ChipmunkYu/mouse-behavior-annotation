"""Strict ffmpeg processor for the explicitly named candidate display profile."""
from __future__ import annotations

import json
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


DISPLAY_PROXY_PROFILE_VERSION = "candidate-720p-h264-crf28-g30-sar1"
OUTPUT_FPS_TOLERANCE = 0.01
OUTPUT_DURATION_TOLERANCE_FRAMES = 1.0
FRAME_INTERVAL_TOLERANCE_SECONDS = 0.001
TIMESTAMP_MAPPING_TOLERANCE_SECONDS = 0.001
MAX_TIME_BASE_ALLOWANCE_SECONDS = 0.002
MIN_FRAME_INTERVAL_RATIO = 0.5
MAX_FRAME_INTERVAL_RATIO = 1.5


@dataclass(frozen=True, slots=True)
class DisplayProxyProfile:
    width: int = 1280
    height: int = 720
    codec: str = "libx264"
    crf: int = 28
    preset: str = "veryfast"
    pixel_format: str = "yuv420p"
    gop_size: int = 30
    sample_aspect_ratio: str = "1:1"


CANDIDATE_PROFILE = DisplayProxyProfile()


class DisplayProxyError(RuntimeError):
    """Safe error suitable for persistence."""


class UnsupportedDisplaySource(DisplayProxyError):
    """The candidate deliberately does not support this source."""


def _fraction(value: object) -> float:
    try:
        numerator, denominator = str(value).split("/", 1)
        result = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        raise DisplayProxyError("media probe returned an invalid frame rate") from None
    if not math.isfinite(result) or result <= 0:
        raise DisplayProxyError("media probe returned an invalid frame rate")
    return result


def sanitize_error(value: object, *paths: str | Path, limit: int = 2000) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    for path in paths:
        if path:
            text = text.replace(str(path), "<media-path>")
    # Defensive fallback for diagnostics emitted by subprocesses.
    text = re.sub(r"(?i)(?:[a-z]:\\|/)(?:[^\s:]+[\\/])+[^\s:]+", "<media-path>", text)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


class DisplayProxyProcessor:
    def __init__(self, *, ffmpeg_path="ffmpeg", ffprobe_path="ffprobe", timeout_seconds=3600):
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.timeout_seconds = timeout_seconds

    def transcode_command(self, input_path: str, output_path: str) -> list[str]:
        return [self.ffmpeg_path, "-hide_banner", "-nostdin", "-y", "-i", input_path,
                "-map", "0:v:0", "-map_metadata", "-1", "-an", "-vf",
                "scale=1280:720:flags=lanczos,setsar=1",
                "-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-g", "30", "-keyint_min", "30",
                "-sc_threshold", "0", "-vsync", "0", "-movflags", "+faststart",
                "-f", "mp4", output_path]

    def _run(self, command: list[str], *paths: str | Path) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(command, shell=False, capture_output=True, text=True,
                                    timeout=self.timeout_seconds, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DisplayProxyError(sanitize_error(exc, *paths)) from None
        if result.returncode:
            raise DisplayProxyError(sanitize_error(result.stderr or "media command failed", *paths))
        return result

    def probe(self, path: str | Path) -> dict:
        command = [self.ffprobe_path, "-v", "error", "-count_frames", "-show_streams",
                   "-show_format", "-of", "json", str(path)]
        result = self._run(command, path)
        try:
            value = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            raise DisplayProxyError("media probe returned invalid JSON") from None
        if not isinstance(value, dict):
            raise DisplayProxyError("media probe returned an invalid document")
        return value

    def probe_frame_timestamps(self, path: str | Path) -> tuple[float, ...]:
        """Read compact frame timing fields; long clips only consume a few MB."""
        command = [self.ffprobe_path, "-v", "error", "-select_streams", "v:0",
                   "-show_frames",
                   "-show_entries",
                   "frame=best_effort_timestamp_time,pts_time,pkt_dts_time",
                   "-of", "json", str(path)]
        result = self._run(command, path)
        try:
            document = json.loads(result.stdout)
            frames = document["frames"]
        except (json.JSONDecodeError, TypeError, KeyError):
            raise DisplayProxyError("media frame probe returned an invalid document") from None
        if not isinstance(frames, list):
            raise DisplayProxyError("media frame probe omitted frames")
        timestamps: list[float] = []
        for frame in frames:
            if not isinstance(frame, dict):
                raise DisplayProxyError("media frame probe returned an invalid frame")
            raw = next((frame.get(key) for key in (
                "best_effort_timestamp_time", "pts_time", "pkt_dts_time"
            ) if frame.get(key) not in (None, "N/A")), None)
            try:
                timestamp = float(raw)
            except (TypeError, ValueError):
                raise DisplayProxyError("media frame probe omitted a timestamp") from None
            if not math.isfinite(timestamp):
                raise DisplayProxyError("media frame probe returned an invalid timestamp")
            timestamps.append(timestamp)
        return tuple(timestamps)

    @staticmethod
    def _video_stream(document: dict) -> dict:
        streams = document.get("streams")
        if not isinstance(streams, list):
            raise DisplayProxyError("media probe omitted streams")
        videos = [item for item in streams
                  if isinstance(item, dict) and item.get("codec_type") == "video"]
        if len(videos) != 1:
            raise DisplayProxyError("proxy must contain exactly one video stream and no audio")
        return videos[0]

    @staticmethod
    def _has_rotation(video: dict) -> bool:
        values: list[object] = []
        tags = video.get("tags")
        if isinstance(tags, dict) and "rotate" in tags:
            values.append(tags["rotate"])
        side_data = video.get("side_data_list")
        if isinstance(side_data, list):
            for item in side_data:
                if (isinstance(item, dict)
                        and str(item.get("side_data_type", "")).lower() == "display matrix"):
                    values.append(item.get("rotation"))
        for value in values:
            try:
                rotation = float(value)
            except (TypeError, ValueError):
                return True
            if not math.isfinite(rotation) or not math.isclose(rotation % 360.0, 0.0,
                                                               abs_tol=1e-6):
                return True
        return False

    @staticmethod
    def _metrics(document: dict, *, output: bool) -> tuple[float, float, int]:
        streams = document.get("streams")
        if not isinstance(streams, list):
            raise DisplayProxyError("media probe omitted streams")
        videos = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"]
        audio = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"]
        if len(videos) != 1 or (output and audio):
            raise DisplayProxyError("proxy must contain exactly one video stream and no audio")
        video = videos[0]
        error = DisplayProxyError if output else UnsupportedDisplaySource
        if video.get("sample_aspect_ratio") != "1:1":
            raise error("candidate profile requires 1:1 sample aspect ratio")
        if DisplayProxyProcessor._has_rotation(video):
            raise error("candidate profile does not allow rotated display matrices")
        fps = _fraction(video.get("avg_frame_rate"))
        nominal = _fraction(video.get("r_frame_rate"))
        try:
            duration = float(video.get("duration") or document.get("format", {}).get("duration"))
            frames = int(video.get("nb_read_frames") or video.get("nb_frames"))
        except (TypeError, ValueError):
            raise DisplayProxyError("media probe omitted duration or frame count") from None
        if not math.isfinite(duration) or duration <= 0 or frames <= 0:
            raise DisplayProxyError("media duration and frame count must be positive")
        if abs(frames - duration * fps) > max(2.0, fps * 0.1):
            raise DisplayProxyError("media frame count, duration, and FPS are inconsistent")
        if output:
            if (video.get("codec_name") != "h264" or video.get("pix_fmt") != "yuv420p"
                    or video.get("width") != 1280 or video.get("height") != 720
                    or document.get("format", {}).get("format_name", "").split(",")[0] not in {"mov", "mp4"}):
                raise DisplayProxyError("proxy does not match the candidate MP4/H264/yuv420p profile")
        else:
            try:
                width, height = int(video.get("width")), int(video.get("height"))
            except (TypeError, ValueError):
                raise UnsupportedDisplaySource("candidate profile requires known dimensions") from None
            if width <= 0 or height <= 0 or width * 9 != height * 16:
                raise UnsupportedDisplaySource("candidate profile supports only 16:9 input")
            if not 29.0 <= fps <= 31.0 or not 29.0 <= nominal <= 31.0:
                raise UnsupportedDisplaySource(
                    "candidate profile supports only approximately 30fps input"
                )
        return fps, duration, frames

    @staticmethod
    def _time_base(document: dict) -> float:
        try:
            return _fraction(DisplayProxyProcessor._video_stream(document).get("time_base"))
        except DisplayProxyError:
            # Missing time_base must not weaken the fixed one-millisecond gate.
            return 0.0

    @staticmethod
    def _validate_timestamps(timestamps: tuple[float, ...], metrics: tuple[float, float, int],
                             *, time_base: float, output: bool,
                             nominal_fps: float | None = None) -> None:
        fps, duration, frames = metrics
        error = DisplayProxyError if output else UnsupportedDisplaySource
        if len(timestamps) != frames:
            raise error("media timestamp frame count differs from decoded frame count")
        if not timestamps:
            raise error("media frame probe omitted timestamps")
        expected = 1.0 / (nominal_fps or fps)
        tolerance = max(FRAME_INTERVAL_TOLERANCE_SECONDS,
                         min(time_base * 2.0, MAX_TIME_BASE_ALLOWANCE_SECONDS))
        minimum_interval = expected * MIN_FRAME_INTERVAL_RATIO
        maximum_interval = expected * MAX_FRAME_INTERVAL_RATIO
        previous = timestamps[0]
        for current in timestamps[1:]:
            interval = current - previous
            if interval <= 0:
                raise error("media frame timestamps are not strictly monotonic")
            if interval < minimum_interval or interval > maximum_interval:
                raise error("media frame timestamp interval is outside the supported 30fps VFR bounds")
            previous = current
        timeline_duration = timestamps[-1] - timestamps[0]
        if abs(duration - timeline_duration) > expected + tolerance:
            raise error("media timestamps and duration differ by more than one frame")

    @staticmethod
    def _compare_timestamps(source: tuple[float, ...], output: tuple[float, ...],
                            *, source_time_base: float, output_time_base: float) -> None:
        if len(source) != len(output):
            raise DisplayProxyError("proxy timestamp frame count differs from source")
        time_base_allowance = min(max(source_time_base, output_time_base) * 2.0,
                                  MAX_TIME_BASE_ALLOWANCE_SECONDS)
        tolerance = max(TIMESTAMP_MAPPING_TOLERANCE_SECONDS, time_base_allowance)
        source_origin, output_origin = source[0], output[0]
        for source_value, output_value in zip(source, output):
            if abs((output_value - output_origin) - (source_value - source_origin)) > tolerance:
                raise DisplayProxyError("proxy frame timestamps drift from source")

    @staticmethod
    def _validate_faststart(path: str | Path) -> None:
        """Fail closed unless complete top-level MP4 atoms place moov before mdat."""
        path = Path(path)
        file_size = path.stat().st_size
        positions: dict[bytes, int] = {}
        with path.open("rb") as handle:
            offset = 0
            while offset < file_size:
                if file_size - offset < 8:
                    raise DisplayProxyError("proxy has a truncated MP4 atom header")
                handle.seek(offset)
                header = handle.read(8)
                atom_size = int.from_bytes(header[:4], "big")
                atom_type = header[4:8]
                header_size = 8
                if atom_size == 1:
                    extended = handle.read(8)
                    if len(extended) != 8:
                        raise DisplayProxyError("proxy has a truncated extended MP4 atom header")
                    atom_size = int.from_bytes(extended, "big")
                    header_size = 16
                elif atom_size == 0:
                    atom_size = file_size - offset
                if atom_size < header_size or atom_size > file_size - offset:
                    raise DisplayProxyError("proxy has a truncated or invalid MP4 atom")
                positions.setdefault(atom_type, offset)
                offset += atom_size
        if b"moov" not in positions or b"mdat" not in positions:
            raise DisplayProxyError("proxy MP4 is missing moov or mdat")
        if positions[b"moov"] > positions[b"mdat"]:
            raise DisplayProxyError("proxy MP4 is not faststart (moov must precede mdat)")

    @staticmethod
    def _compare_metrics(source: tuple[float, float, int],
                         output: tuple[float, float, int]) -> None:
        source_fps, source_duration, source_frames = source
        output_fps, output_duration, output_frames = output
        if output_frames != source_frames:
            raise DisplayProxyError("proxy frame count differs from source")
        if abs(output_fps - source_fps) > OUTPUT_FPS_TOLERANCE:
            raise DisplayProxyError("proxy FPS differs from source")
        # One frame at the slower measured rate is the largest accepted duration drift.
        duration_tolerance = OUTPUT_DURATION_TOLERANCE_FRAMES / min(source_fps, output_fps)
        if abs(output_duration - source_duration) > duration_tolerance:
            raise DisplayProxyError("proxy duration differs from source by more than one frame")

    def render(self, *, input_path: str, output_path: str) -> None:
        source_document = self.probe(input_path)
        source_metrics = self._metrics(source_document, output=False)
        source_time_base = self._time_base(source_document)
        source_timestamps = self.probe_frame_timestamps(input_path)
        self._validate_timestamps(source_timestamps, source_metrics,
                                  time_base=source_time_base, output=False,
                                  nominal_fps=_fraction(
                                      self._video_stream(source_document).get("r_frame_rate")
                                  ))
        self._run(self.transcode_command(input_path, output_path), input_path, output_path)
        output_document = self.probe(output_path)
        output_metrics = self._metrics(output_document, output=True)
        output_time_base = self._time_base(output_document)
        output_timestamps = self.probe_frame_timestamps(output_path)
        self._validate_timestamps(output_timestamps, output_metrics,
                                  time_base=output_time_base, output=True,
                                  nominal_fps=_fraction(
                                      self._video_stream(output_document).get("r_frame_rate")
                                  ))
        self._compare_metrics(source_metrics, output_metrics)
        self._compare_timestamps(source_timestamps, output_timestamps,
                                 source_time_base=source_time_base,
                                 output_time_base=output_time_base)
        self._validate_faststart(output_path)
        self._run([self.ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error", "-i",
                   output_path, "-map", "0:v:0", "-f", "null", "-"], output_path)
