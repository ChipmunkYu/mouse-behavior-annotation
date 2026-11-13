"""Read-only release hard gate: execute both configured media binaries, no app/DB startup."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import Settings
from app.media import FfmpegMediaProcessor, MediaDependencyError


def run_preflight(settings_loader=Settings, output=print, *, env_file=None) -> int:
    try:
        if env_file is not None:
            if not Path(env_file).is_file():
                raise ValueError("configuration unavailable")
            load_dotenv(env_file, override=True, encoding="utf-8")
        settings = settings_loader()
        FfmpegMediaProcessor(ffmpeg_path=settings.ffmpeg_path,
                             ffprobe_path=settings.ffprobe_path).check_dependencies()
    except MediaDependencyError as exc:
        output(str(exc))
        return 1
    except Exception:
        output("media_preflight_configuration_error: 无法读取媒体配置，请管理员检查指定环境文件。")
        return 2
    output("media_preflight_ok: ffmpeg / ffprobe 均可执行")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    raise SystemExit(run_preflight(env_file=parser.parse_args().env_file))
