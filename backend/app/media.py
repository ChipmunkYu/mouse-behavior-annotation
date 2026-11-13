"""媒体执行器（批次 4）：ffmpeg/ffprobe 子进程封装 + 精确片段重编码与缩略图。

- 命令一律以参数列表调用 subprocess，**绝不使用 shell=True**。
- 输入源解析严格限制在配置的 videos_dir 内（越界 / 缺失 → MediaCommandError）。
- 输出长度以帧数为准：`-frames:v <frames>` 精确产出 frames 帧（源多为 VFR，
  按时间 `-t` 裁剪可能丢失末帧）。
- 输出帧率以调用方声明的 `fps` 为准：`-r <fps> -vsync 0` 声明帧率但不改帧时序；
  `-vsync cfr` 会在 VFR 源上补/丢帧，故禁用，输出帧数仍由 `-frames:v` 决定。
- 视频滤镜链末尾追加 `setpts=N/(<fps>*TB)`，把第 0 帧 PTS 归零、相邻帧间隔钉为
  1/fps，使容器时长满足 canonical frame/fps 时间轴（源 PTS 透传会让容器时长漂移，
  触发导出契约 `ffprobe duration mismatch`）。
- 输出先写临时文件，由 worker 成功后原子替换；失败由 worker 清理半成品；
  错误字段不写入原始 stderr、执行命令或绝对路径。
- 本机可能没有 ffmpeg：测试通过 `FakeMediaProcessor` / 其它替换实现注入，
  不要求系统 ffmpeg。真实执行器通过 FFMPEG_PATH / FFPROBE_PATH 配置。
"""
from __future__ import annotations

import json
import math
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Protocol

class MediaCommandError(RuntimeError):
    """可安全展示的媒体操作失败；环境依赖故障使用 MediaDependencyError。"""


class MediaDependencyError(MediaCommandError):
    """Service dependency failure; never include subprocess output or configured paths."""

    code = "media_dependency_unavailable"

    def __init__(self, tool: str):
        self.tool = tool
        self.detail = {
            "code": self.code,
            "message": f"服务媒体依赖 {tool} 不可用；请管理员检查 FFMPEG_PATH / FFPROBE_PATH、执行权限及运行库，运行媒体预检后重试。无需修改上传文件。",
            "tool": tool,
        }
        super().__init__(f"{self.code}: {self.detail['message']}")


def check_media_executable(executable: str, tool: str) -> None:
    """Actually execute the configured binary, including loader/permission checks."""
    try:
        result = subprocess.run([executable, "-version"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=5, shell=False)
    except (OSError, subprocess.TimeoutExpired):
        raise MediaDependencyError(tool) from None
    if result.returncode != 0 or not result.stdout.lower().startswith(f"{tool} version "):
        raise MediaDependencyError(tool)


def run_media_command(command: list[str], *, tool: str, timeout: int):
    """Shared classification for rendering, probing, timestamps and display proxies."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, shell=False)
    except OSError:
        raise MediaDependencyError(tool) from None
    except subprocess.TimeoutExpired:
        raise MediaCommandError(f"Media command timed out after {timeout}s") from None
    if result.returncode != 0:
        # A broken loader/runtime is environmental; a healthy tool rejecting media is not.
        check_media_executable(command[0], tool)
        raise MediaCommandError(f"Media command failed (exit {result.returncode}); video or media operation is invalid")
    return result


def format_time(value: float) -> str:
    """秒 → 命令行时间文本；纳秒级小数避免 30/60 FPS 边界量化。"""
    text = f"{value:.9f}".rstrip("0").rstrip(".")
    return text if text else "0"


class MediaProcessor(Protocol):
    """媒体执行器协议：测试可注入 FakeMediaProcessor / 其它替换实现。"""

    def render_clip(
        self, *, input_path: str, start_frame: int, frames: int, output_path: str,
        fps: float, crop: tuple[int, int, int, int] | None = None,
    ) -> None:
        """把 input_path 从 start_frame 帧起、共 frames 帧重编码为 H.264 MP4 写至 output_path（临时文件）。

        `fps` 为调用方声明的源帧率，输出必须显式声明该帧率且不补帧/丢帧（VFR 源）。
        """

    def render_thumbnail(self, *, input_path: str, at: float, output_path: str,
                         crop: tuple[int, int, int, int] | None = None) -> None:
        """在 at 秒处抽取一帧 JPEG 缩略图写至 output_path（临时文件）。"""

    def probe_clip(self, path: str, *, expected: dict | None = None) -> dict:
        """Return decoded video fps, dimensions and frame count; failure is fatal."""


class FfmpegMediaProcessor:
    """真实 ffmpeg 执行器：H.264(libx264)+yuv420p+faststart 精确重编码 + 中点 JPEG 缩略图。"""

    def __init__(
        self,
        *,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        crf: int = 23,
        preset: str = "veryfast",
        timeout_seconds: int = 600,
        map_audio: bool = False,
    ) -> None:
        self.ffmpeg = ffmpeg_path
        self.ffprobe = ffprobe_path
        self.crf = crf
        self.preset = preset
        self.timeout = timeout_seconds
        self.map_audio = map_audio

    def check_dependencies(self) -> None:
        check_media_executable(self.ffmpeg, "ffmpeg")
        check_media_executable(self.ffprobe, "ffprobe")

    def build_clip_command(
        self, input_path: str, start: float, frames: int, output_path: str,
        fps: float, crop: tuple[int, int, int, int] | None = None,
    ) -> list[str]:
        """精确重编码命令：-ss 定位 + -frames:v 帧数上限 + libx264 + yuv420p + faststart。

        `-frames:v <frames>` 是输出长度权威；绝不使用 `-t`（按时间裁剪在 VFR 源上会丢末帧）。
        `-r <fps> -vsync 0` 把调用方声明的源帧率写入输出，同时透传源帧时序：`-vsync cfr`
        会在 VFR 源上补帧/丢帧（改变帧数与内容），因此不使用。
        视频滤镜链末尾的 `setpts=N/(<fps>*TB)` 把第 0 帧 PTS 归零、相邻帧间隔钉为
        1/fps，使容器时长等于 frames/fps；仅透传源 PTS 会让容器时长漂移（生产 clip 1014
        55 帧 @ 30.012442789554914 fps → 1.866s，超出导出契约的一帧容差）。
        """
        if isinstance(frames, bool) or not isinstance(frames, int) or frames <= 0:
            raise ValueError("clip frames must be a positive integer")
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0:
            raise ValueError("clip fps must be finite and positive")
        cmd = [
            self.ffmpeg,
            "-y",
            "-ss",
            format_time(start),
            "-i",
            input_path,
            "-map",
            "0:v:0",
            "-c:v",
            "libx264",
            "-preset",
            self.preset,
            "-crf",
            str(self.crf),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ]
        if self.map_audio:
            # 可选音频映射（? 后缀：无音轨不报错），aac 编码
            cmd += ["-map", "0:a:0?", "-c:a", "aac"]
        else:
            # Do not let ffmpeg's automatic stream selection add an audio stream.
            cmd += ["-an"]
        if crop is not None:
            x, y, w, h = crop
            filters = [f"crop={w}:{h}:{x}:{y}"]
        else:
            filters = []
        # Pin the canonical frame/fps timeline: frame N lands at N/fps so the
        # container duration equals frames/fps instead of drifting with source PTS.
        filters.append(f"setpts=N/({repr(float(fps))}*TB)")
        cmd += ["-vf", ",".join(filters)]
        # Declare the caller fps with -r but keep frame timing untouched (-vsync 0 /
        # passthrough): cfr would pad or drop frames on VFR sources. Output length
        # stays authoritative in frames. repr() keeps round-trip precision for
        # non-integer source rates.
        cmd += ["-r", repr(float(fps)), "-vsync", "0"]
        # Output length is authoritative in frames; -t must never appear.
        cmd += ["-frames:v", str(frames)]
        # Worker outputs end in .mp4.part, so extension inference is intentionally unavailable.
        cmd += ["-f", "mp4", output_path]
        return cmd

    def build_thumbnail_command(
        self, input_path: str, at: float, output_path: str,
        crop: tuple[int, int, int, int] | None = None,
    ) -> list[str]:
        """片段中点抽帧 JPEG 缩略图命令。"""
        cmd = [
            self.ffmpeg,
            "-y",
            "-ss",
            format_time(at),
            "-i",
            input_path,
            "-frames:v",
            "1",
            "-q:v",
            "2",
        ]
        if crop is not None:
            x, y, w, h = crop
            cmd += ["-vf", f"crop={w}:{h}:{x}:{y}"]
        # Worker outputs end in .jpg.part; force the image muxer instead of inferring from .part.
        cmd += ["-f", "image2", output_path]
        return cmd

    def _run(self, cmd: list[str]) -> None:
        run_media_command(cmd, tool="ffmpeg", timeout=self.timeout)

    def probe_clip(self, path: str, *, expected: dict | None = None) -> dict:
        cmd = [self.ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
               "-show_entries", "stream=width,height,avg_frame_rate,nb_read_frames:format=duration",
               "-of", "json", path]
        proc = run_media_command(cmd, tool="ffprobe", timeout=self.timeout)
        try:
            document = json.loads(proc.stdout)
            stream = document["streams"][0]
            numerator, denominator = stream["avg_frame_rate"].split("/", 1)
            return {"fps": float(numerator) / float(denominator), "width": int(stream["width"]),
                    "height": int(stream["height"]), "frame_count": int(stream["nb_read_frames"]),
                    "duration": float(document["format"]["duration"])}
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MediaCommandError("ffprobe returned incomplete video properties") from exc

    def render_clip(
        self, *, input_path: str, start_frame: int, frames: int, output_path: str,
        fps: float, crop: tuple[int, int, int, int] | None = None,
    ) -> None:
        if isinstance(start_frame, bool) or not isinstance(start_frame, int) or start_frame < 0:
            raise ValueError("clip start_frame must be a non-negative integer")
        start = self._frame_timestamp(str(input_path), start_frame)
        self._run(self.build_clip_command(str(input_path), start, frames, str(output_path),
                                          fps, crop))

    def _frame_timestamp(self, input_path: str, start_frame: int) -> float:
        try:
            path = Path(input_path).resolve()
            stat = path.stat()
        except OSError as exc:
            raise MediaCommandError("Cannot read media input") from None
        timestamps = self._probe_frame_timestamps(str(path), stat.st_size, stat.st_mtime_ns)
        if start_frame >= len(timestamps):
            raise MediaCommandError(
                f"ffprobe returned {len(timestamps)} video frames; start_frame {start_frame} is out of range"
            )
        return timestamps[start_frame]

    @lru_cache(maxsize=32)
    def _probe_frame_timestamps(
        self, input_path: str, _size: int, _mtime_ns: int,
    ) -> tuple[float, ...]:
        cmd = [self.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
               "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", input_path]
        proc = run_media_command(cmd, tool="ffprobe", timeout=self.timeout)
        try:
            frames = json.loads(proc.stdout)["frames"]
            if not isinstance(frames, list) or not frames:
                raise ValueError
            timestamps = tuple(float(frame["best_effort_timestamp_time"]) for frame in frames)
            if (any(not math.isfinite(value) for value in timestamps)
                    or any(later <= earlier for earlier, later in zip(timestamps, timestamps[1:]))):
                raise ValueError
            return timestamps
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MediaCommandError("ffprobe returned invalid frame timestamps") from exc

    def render_thumbnail(self, *, input_path: str, at: float, output_path: str,
                         crop: tuple[int, int, int, int] | None = None) -> None:
        self._run(self.build_thumbnail_command(str(input_path), at, str(output_path), crop))
