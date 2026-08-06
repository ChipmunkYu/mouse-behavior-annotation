"""媒体执行器（批次 4）：ffmpeg/ffprobe 子进程封装 + 精确片段重编码与缩略图。

- 命令一律以参数列表调用 subprocess，**绝不使用 shell=True**。
- 输入源解析严格限制在配置的 videos_dir 内（越界 / 缺失 → MediaCommandError）。
- 输出先写临时文件，由 worker 成功后原子替换；失败由 worker 清理半成品；
  stderr 截断写入错误字段。
- 本机可能没有 ffmpeg：测试通过 `FakeMediaProcessor` / 其它替换实现注入，
  不要求系统 ffmpeg。真实执行器通过 FFMPEG_PATH / FFPROBE_PATH 配置。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol

STDERR_TRUNCATE_LIMIT = 4000


class MediaCommandError(RuntimeError):
    """媒体命令失败（非零退出 / 超时 / 可执行文件缺失 / 路径越界等）。

    错误信息包含截断后的 stderr，供任务与 Clip 记录展示。
    """


def truncate_text(text: str | None, limit: int = STDERR_TRUNCATE_LIMIT) -> str:
    """截断长文本（stderr / 错误信息），尾部注明原长度，避免撑爆 DB 字段。"""
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + f"...[truncated, {len(value)} chars]"


def _stderr_text(stderr: str | bytes | None) -> str:
    if isinstance(stderr, bytes):
        return stderr.decode("utf-8", errors="replace")
    return stderr or ""


def format_time(value: float) -> str:
    """秒 → 命令行时间文本：保留两位小数并去掉多余尾零（0.5 → '0.5'）。"""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text if text else "0"


class MediaProcessor(Protocol):
    """媒体执行器协议：测试可注入 FakeMediaProcessor / 其它替换实现。"""

    def render_clip(
        self, *, input_path: str, start: float, end: float, output_path: str
    ) -> None:
        """把 input_path 的 [start, end) 秒片段重编码为 H.264 MP4 写至 output_path（临时文件）。"""

    def render_thumbnail(self, *, input_path: str, at: float, output_path: str) -> None:
        """在 at 秒处抽取一帧 JPEG 缩略图写至 output_path（临时文件）。"""


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

    def build_clip_command(
        self, input_path: str, start: float, end: float, output_path: str
    ) -> list[str]:
        """精确重编码命令：-ss 前置输入定位（重编码下帧精确）+ libx264 + yuv420p + faststart。"""
        cmd = [
            self.ffmpeg,
            "-y",
            "-ss",
            format_time(start),
            "-to",
            format_time(end),
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
        cmd.append(output_path)
        return cmd

    def build_thumbnail_command(
        self, input_path: str, at: float, output_path: str
    ) -> list[str]:
        """片段中点抽帧 JPEG 缩略图命令。"""
        return [
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
            output_path,
        ]

    def _run(self, cmd: list[str]) -> None:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                shell=False,  # 参数列表直传，禁用 shell
            )
        except subprocess.TimeoutExpired as exc:
            detail = truncate_text(_stderr_text(getattr(exc, "stderr", None)))
            raise MediaCommandError(
                f"Media command timed out after {self.timeout}s: {cmd[0]}"
                + (f"\n{detail}" if detail else "")
            ) from exc
        except FileNotFoundError as exc:
            raise MediaCommandError(
                f"Media executable not found: {cmd[0]!r} "
                "(check FFMPEG_PATH / FFPROBE_PATH)"
            ) from exc
        except OSError as exc:
            raise MediaCommandError(f"Failed to run media command {cmd[0]!r}: {exc}") from exc
        if proc.returncode != 0:
            raise MediaCommandError(
                f"Media command failed (exit {proc.returncode}): {cmd[0]}\n"
                f"{truncate_text(_stderr_text(proc.stderr))}"
            )

    def render_clip(
        self, *, input_path: str, start: float, end: float, output_path: str
    ) -> None:
        self._run(self.build_clip_command(str(input_path), start, end, str(output_path)))

    def render_thumbnail(self, *, input_path: str, at: float, output_path: str) -> None:
        self._run(self.build_thumbnail_command(str(input_path), at, str(output_path)))
