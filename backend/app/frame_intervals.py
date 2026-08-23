"""Authoritative inclusive frame interval validation and time derivation."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class FrameInterval:
    start_frame: int
    end_frame: int
    start_time: float
    end_time: float

    @property
    def frame_count(self) -> int:
        return self.end_frame - self.start_frame + 1


def canonical_frame_interval(*, start_frame: int, end_frame: int, fps: float,
                             frame_count: int | None = None) -> FrameInterval:
    """Validate an inclusive interval and derive its half-open time boundaries."""
    if (isinstance(fps, bool) or not isinstance(fps, (int, float))
            or not math.isfinite(fps) or fps <= 0):
        raise ValueError("fps must be finite and greater than zero")
    if (isinstance(start_frame, bool) or not isinstance(start_frame, int)
            or isinstance(end_frame, bool) or not isinstance(end_frame, int)):
        raise ValueError("frame boundaries must be integers")
    if start_frame < 0 or end_frame <= start_frame:
        raise ValueError("end_frame must be greater than start_frame")
    if frame_count is not None:
        if isinstance(frame_count, bool) or not isinstance(frame_count, int) or frame_count <= 0:
            raise ValueError("frame_count must be greater than zero")
        if end_frame >= frame_count:
            raise ValueError("frame range exceeds media")
    return FrameInterval(
        start_frame=start_frame,
        end_frame=end_frame,
        start_time=start_frame / fps,
        end_time=(end_frame + 1) / fps,
    )
