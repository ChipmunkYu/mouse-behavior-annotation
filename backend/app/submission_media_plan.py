"""Pure, shared Submission media interval/crop planning.

Annotation frame ranges are inclusive at both ends.  Render intervals are
half-open: [start_frame/fps, (end_frame+1)/fps).
"""
from dataclasses import dataclass
import math


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
    if not math.isfinite(fps) or fps <= 0 or start_frame < 0 or end_frame < start_frame:
        raise ValueError("invalid frame range or fps")
    if end_frame >= frame_count:
        raise ValueError("frame range exceeds media")
    start = start_frame / fps
    end = (end_frame + 1) / fps
    # Existing annotation clients round boundaries to either adjacent frame.
    tolerance = 1.0 / fps + 1e-6
    if (not math.isfinite(start_time) or not math.isfinite(end_time)
            or abs(start_time - start) > tolerance or abs(end_time - end) > tolerance):
        raise ValueError("time range is inconsistent with inclusive frame range")
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
