"""Configuration-driven social participant continuity validation."""
from __future__ import annotations

import json
import math
import hashlib
from pathlib import PurePosixPath

from fastapi import HTTPException

from .effective_detections import effective_detection_query
from .export_contract import transform_detection
from .models import BehaviorCategory, DetectionImport, RawDetection
from .integrity_canonical import validate_pose_metadata
from .submission_media_plan import build_submission_media_plan

_CENTER_NAMES = {"base_of_neck", "neck", "left_hip", "right_hip"}


def keypoint_names(settings, imp: DetectionImport) -> list[str]:
    if not imp.metadata_path:
        raise ValueError("检测元数据路径缺失，无法校验社交行为连续性")
    key = imp.metadata_path
    relative = PurePosixPath(key)
    if (relative.is_absolute() or "\\" in key or ":" in key
            or any(part in {"", ".", ".."} for part in key.split("/"))):
        raise ValueError("检测元数据路径无效，无法校验社交行为连续性")
    root = settings.detection_imports_dir.resolve()
    path = (root / key).resolve()
    if path == root or not path.is_relative_to(root) or not path.is_file():
        raise ValueError("检测元数据文件缺失或位于存储目录之外")
    try:
        raw = path.read_bytes()
        if not imp.metadata_sha256 or hashlib.sha256(raw).hexdigest() != imp.metadata_sha256:
            raise ValueError("检测元数据校验和不匹配")
        content = json.loads(raw.decode("utf-8"))
        names, _edges = validate_pose_metadata(
            content.get("keypoint_names"),
            content.get("skeleton_edges", content.get("skeleton_edges_0based")),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("检测元数据"):
            raise
        raise ValueError("检测元数据无效，无法校验社交行为连续性") from exc
    if not any(name in _CENTER_NAMES for name in names):
        raise ValueError("检测元数据缺少颈部或髋部中心关键点")
    return names


def participant_continuity_error(db, imp: DetectionImport | None, category: BehaviorCategory,
                                 mouse_ids: list[int], start_frame: int, end_frame: int,
                                 crop_region: dict | None, *, keypoint_names=None) -> str | None:
    if category.group != "社交行为":
        return None
    if imp is None:
        return "社交行为连续性校验需要有效的检测数据"
    if keypoint_names is None:
        return "无法读取关键点定义，不能校验社交行为参与小鼠连续性"
    indices = [index for index, name in enumerate(keypoint_names) if name in _CENTER_NAMES]
    try:
        plan = build_submission_media_plan(start_time=start_frame / imp.fps,
            end_time=(end_frame + 1) / imp.fps, start_frame=start_frame, end_frame=end_frame,
            fps=imp.fps, frame_count=imp.frame_count, width=imp.width, height=imp.height,
            crop_region=crop_region)
    except (TypeError, ValueError):
        return "裁剪区域无效，无法校验社交行为连续性"
    usable = {track_id: {} for track_id in mouse_ids}
    rows = effective_detection_query(db, imp.id, start_frame=start_frame, end_frame=end_frame).order_by(
        RawDetection.frame_index, RawDetection.frame_detection_index, RawDetection.id)
    for row in rows:
        track_id = row.display_track_id
        if track_id not in usable:
            continue
        try:
            transformed = transform_detection(row.RawDetection, track_id, plan.crop,
                                              plan.output_width, plan.output_height)
        except (KeyError, TypeError, ValueError, OverflowError):
            transformed = None
        if transformed is None:
            continue
        points = transformed.get("keypoints", [])
        usable[track_id][row.RawDetection.frame_index] = any(
            index < len(points) and len(points[index]) >= 2
            and all(isinstance(value, (int, float)) and math.isfinite(value)
                    for value in points[index][:2]) for index in indices)
    for track_id, by_frame in usable.items():
        frames = {frame for frame, is_usable in by_frame.items() if is_usable}
        if not frames:
            return f"轨迹 {track_id} 在帧 [{start_frame}, {end_frame}] 内没有可用于下游的帧"
        run_start = None
        for frame in range(start_frame, end_frame + 2):
            if frame <= end_frame and frame not in frames:
                run_start = frame if run_start is None else run_start
            elif run_start is not None:
                count = frame - run_start
                if count > 5:
                    return f"轨迹 {track_id} 在帧 [{run_start}, {frame - 1}] 连续缺失 {count} 帧（最多允许 5 帧）"
                run_start = None
    return None


def validate_participant_continuity(db, settings, imp, category, mouse_ids, start_frame,
                                    end_frame, crop_region) -> None:
    try:
        names = keypoint_names(settings, imp) if imp is not None and category.group == "社交行为" else []
        reason = participant_continuity_error(
            db, imp, category, mouse_ids, start_frame, end_frame, crop_region,
            keypoint_names=names,
        )
    except ValueError as exc:
        reason = str(exc)
    if reason:
        raise HTTPException(status_code=400, detail=reason)
