"""Pure, shared Submission media interval/crop planning.

Annotation frame ranges are inclusive at both ends.  Render intervals are
half-open: [start_frame/fps, (end_frame+1)/fps).
"""
from dataclasses import dataclass
import math

from .frame_intervals import canonical_frame_interval


@dataclass(frozen=True)
class SubmissionMediaPlan:
    start: float
    end: float
    thumbnail_at: float
    crop: tuple[int, int, int, int] | None
    output_width: int
    output_height: int


def build_submission_media_plan(*, start_time: float, end_time: float,
                                start_frame: int, end_frame: int, fps: float,
                                frame_count: int, width: int, height: int,
                                crop_region: dict | None) -> SubmissionMediaPlan:
    # Time arguments remain accepted for API/caller compatibility, but frames are authoritative.
    interval = canonical_frame_interval(
        start_frame=start_frame, end_frame=end_frame, fps=fps, frame_count=frame_count
    )
    start, end = interval.start_time, interval.end_time
    crop = None
    out_width, out_height = width, height
    if crop_region is not None:
        if not isinstance(crop_region, dict) or set(crop_region) != {"x", "y", "w", "h"}:
            raise ValueError("invalid crop region")
        values = [crop_region[k] for k in ("x", "y", "w", "h")]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            raise ValueError("invalid crop region")
        x, y, w, h = (int(round(v)) for v in values)
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
            raise ValueError("invalid crop region")
        crop, out_width, out_height = (x, y, w, h), w, h
    return SubmissionMediaPlan(start, end, (start + end) / 2, crop, out_width, out_height)
