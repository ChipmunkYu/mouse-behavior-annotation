"""Submission-authoritative independent clip export contract."""
from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .media import MediaCommandError

FILES = {"clip.mp4", "annotation.json", "tracks.json", "metadata.json"}
FORBIDDEN_KEYS = {
    "id", "annotation_id", "submission_id", "video_id", "revision", "review",
    "reviewer", "annotator", "user", "storage_key", "sha256", "server_path",
}
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                     *(f"LPT{i}" for i in range(1, 10))}


def safe_part(value: str, *, fallback: str = "untitled", limit: int = 80) -> str:
    """Create one portable path component (Unicode retained, controls/path syntax removed)."""
    value = unicodedata.normalize("NFKC", value or "")
    value = "".join("_" if ord(ch) < 32 or ch in '<>:"/\\|?*' else ch for ch in value)
    value = " ".join(value.split()).strip(" .")[:limit].rstrip(" .") or fallback
    if value.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        value = f"_{value}"
    return value


@dataclass(frozen=True)
class TracksSummary:
    """Bounded-memory facts validated while tracks.json is written."""
    frame_count: int
    valid_track_ids: frozenset[int]


def validate_track_frame(frame: dict, *, expected_frame: int, fps: float,
                         width: int, height: int) -> None:
    """Validate one tracks frame before it is serialized; never reload tracks.json."""
    if frame.get("frame") != expected_frame or not math.isclose(
            float(frame.get("time", -1)), expected_frame / fps, rel_tol=1e-9, abs_tol=1e-9):
        raise MediaCommandError("tracks frame/time sequence mismatch")
    detections = frame.get("detections")
    if not isinstance(detections, list):
        raise MediaCommandError("tracks detections must be an array")
    for detection in detections:
        if isinstance(detection.get("track_id"), bool) or not isinstance(detection.get("track_id"), int):
            raise MediaCommandError("tracks track_id must be an integer")
        box = detection.get("box")
        if (not isinstance(box, list) or len(box) != 4
                or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in box)
                or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)):
            raise MediaCommandError("tracks box is outside clip pixels")
        for point in detection.get("keypoints", []):
            if (not isinstance(point, list) or len(point) != 3
                    or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in point)
                    or not (0 <= point[0] <= width and 0 <= point[1] <= height and 0 <= point[2] <= 1)):
                raise MediaCommandError("tracks keypoint is invalid or outside clip pixels")
        confidence = detection.get("detection_confidence")
        if confidence is not None and (not isinstance(confidence, (int, float))
                                       or not math.isfinite(confidence)):
            raise MediaCommandError("tracks detection_confidence must be finite")
        if isinstance(detection.get("class_id"), bool) or not isinstance(detection.get("class_id"), int):
            raise MediaCommandError("tracks class_id must be an integer")
        _walk_forbidden(detection)


def transform_detection(raw, track_id: int, crop: tuple[int, int, int, int] | None,
                        width: int, height: int) -> dict | None:
    """Transform a detection to clip pixels.

    Boxes with no crop intersection are excluded; intersecting boxes are clamped. Keypoints are
    translated and clamped, and an out-of-frame keypoint receives confidence 0 (same list shape).
    """
    box = raw.box or {}
    if not all(k in box for k in ("x1", "y1", "x2", "y2")):
        return None
    ox, oy = (crop[0], crop[1]) if crop else (0, 0)
    x1, y1, x2, y2 = (float(box[k]) for k in ("x1", "y1", "x2", "y2"))
    x1, x2, y1, y2 = x1 - ox, x2 - ox, y1 - oy, y2 - oy
    if x2 <= 0 or y2 <= 0 or x1 >= width or y1 >= height:
        return None
    out_box = [max(0.0, x1), max(0.0, y1), min(float(width), x2), min(float(height), y2)]
    keypoints = []
    for point in raw.keypoints or []:
        if isinstance(point, dict):
            x, y, confidence = point.get("x_px"), point.get("y_px"), point.get("confidence")
        elif isinstance(point, list) and len(point) >= 3:
            x, y, confidence = point[:3]
        else:
            continue
        x, y, confidence = float(x) - ox, float(y) - oy, float(confidence)
        outside = x < 0 or y < 0 or x > width or y > height
        keypoints.append([min(max(x, 0.0), float(width)), min(max(y, 0.0), float(height)),
                          0.0 if outside else confidence])
    return {"track_id": int(track_id), "box": out_box, "keypoints": keypoints,
            "detection_confidence": raw.detection_confidence,
            "class_id": raw.class_id}


def _walk_forbidden(value) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in FORBIDDEN_KEYS:
                raise MediaCommandError(f"forbidden export field: {key}")
            _walk_forbidden(child)
    elif isinstance(value, list):
        for child in value:
            _walk_forbidden(child)


def validate_clip_directory(directory: Path, probe: dict, summary: TracksSummary) -> None:
    if {path.name for path in directory.iterdir()} != FILES or not all(path.is_file() for path in directory.iterdir()):
        raise MediaCommandError("clip directory must contain exactly four files")
    annotation = json.loads((directory / "annotation.json").read_text(encoding="utf-8"))
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    _walk_forbidden([annotation, metadata])
    clip = metadata.get("clip", {})
    for key in ("fps", "width", "height", "frame_count"):
        matches = (math.isclose(float(clip.get(key, -1)), float(probe.get(key, -2)), rel_tol=1e-6,
                                abs_tol=1e-6) if key == "fps" else clip.get(key) == probe.get(key))
        if key not in probe or not matches:
            raise MediaCommandError(f"ffprobe {key} mismatch")
    count, fps, width, height = clip["frame_count"], clip["fps"], clip["width"], clip["height"]
    if "duration" not in probe or not math.isclose(probe["duration"], count / fps,
                                                    abs_tol=1 / fps + 1e-6):
        raise MediaCommandError("ffprobe duration mismatch")
    if summary.frame_count != count or annotation.get("frame_range") != {"start": 0, "end": max(0, count - 1)}:
        raise MediaCommandError("frame count/range mismatch")
    if not set(annotation.get("mouse_ids", [])).issubset(summary.valid_track_ids):
        raise MediaCommandError("annotation mouse_ids are not snapshot track IDs")
