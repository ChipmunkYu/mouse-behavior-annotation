"""检测导入、替换、稀疏有效状态查询与当前修正结果导出。

不包含 Split/Merge/Suppression 端点。
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..assignee_triggers import ASSIGNEE_CONFLICT_DETAIL, is_assignee_write_conflict
from ..deps import project_access
from ..effective_detections import effective_detection_query, effective_track_summary_query
from ..models import (
    Annotation,
    DetectionImport,
    DetectionStateOverride,
    DraftIdentityEdit,
    ProjectMembership,
    RawDetection,
    User,
    Video,
    VideoImportBatch,
)
from ..permissions import is_manager
from ..schemas import (
    BatchStatusOut,
    DetectionImportCurrentOut,
    DetectionImportReplaceOut,
    DetectionWithTrackOut,
    PageOut,
    VideoImportBatchOut,
)
from ..track_ids import TRACK_ID_UPPER_BOUND, is_valid_track_id, next_display_track_id
from ..video_write_gate import video_write_gate

router = APIRouter(tags=["detection-imports"])


def _after_corrected_export_candidate() -> None:
    """Test synchronization point after materialization and before publish validation."""

ALLOWED_TRACKS_EXT = ".jsonl"
ALLOWED_METADATA_EXT = ".json"
SUPPORTED_SCHEMA_VERSIONS = ["1.0"]


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _safe_display_name(raw: str) -> str:
    name = raw.replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable())
    name = name.strip()
    if not name:
        raise ValueError("Filename is invalid")
    return name[:255]


def _source_basename(raw: str) -> str:
    """Return a basename for metadata paths emitted on either POSIX or Windows."""
    return raw.replace("\\", "/").rsplit("/", 1)[-1]


def _validate_source_filename(meta_info: dict, video_filename: str) -> None:
    source_relative = meta_info.get("source_relative")
    if source_relative is None:
        return
    source_name = _source_basename(source_relative)
    expected_name = _source_basename(video_filename)
    if not source_name or source_name != expected_name:
        raise HTTPException(
            status_code=400,
            detail={"validation_errors": [
                "metadata source_relative basename "
                f"'{source_name}' does not match video filename '{expected_name}'"
            ]},
        )


def _validate_replacement_compatibility(
    db: Session, video: Video, meta_info: dict
) -> None:
    errors: list[str] = []
    source_relative = meta_info.get("source_relative")
    if source_relative is not None:
        source_name = _source_basename(source_relative)
        video_name = _source_basename(video.filename)
        if not source_name or source_name != video_name:
            errors.append(
                "metadata source_relative basename "
                f"'{source_name}' does not match video filename '{video_name}'"
            )

    for field in ("width", "height"):
        existing = getattr(video, field)
        incoming = meta_info[field]
        if existing is not None and existing != incoming:
            errors.append(f"metadata {field} {incoming} does not match video {field} {existing}")
    if video.fps is not None and not math.isclose(video.fps, meta_info["fps"], rel_tol=1e-6, abs_tol=1e-6):
        errors.append(f"metadata fps {meta_info['fps']} does not match video fps {video.fps}")

    active_import = (
        db.query(DetectionImport)
        .filter(DetectionImport.video_id == video.id, DetectionImport.active == True)
        .first()
    )
    if (
        active_import is not None
        and active_import.frame_count is not None
        and active_import.frame_count != meta_info["frame_count"]
    ):
        errors.append(
            f"metadata frame_count {meta_info['frame_count']} does not match "
            f"current import frame_count {active_import.frame_count}"
        )
    if errors:
        raise HTTPException(status_code=400, detail={"validation_errors": errors})


def _prevent_traversal(filename: str) -> None:
    if ".." in filename or "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Path traversal in filename prevented")


def _check_non_empty(file: UploadFile) -> None:
    if file.size is not None and file.size == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")


def _require_batch(db: Session, batch_id: int, project_id: int) -> VideoImportBatch:
    batch = db.get(VideoImportBatch, batch_id)
    if batch is None or batch.project_id != project_id:
        raise HTTPException(status_code=404, detail="Import batch not found")
    return batch


def _require_video(db: Session, video_id: int, project_id: int) -> Video:
    video = db.get(Video, video_id)
    if video is None or video.project_id != project_id:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


def _is_finite_number(value) -> bool:
    """Reject NaN, Infinity, bool disguised as number."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _validate_numeric_field(value, field_name: str, errors: list[str], context: str = "") -> None:
    if value is None:
        return
    if isinstance(value, bool):
        errors.append(f"{context} {field_name}: expected number, got boolean")
        return
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            errors.append(f"{context} {field_name}: value is NaN or Infinity")
        return
    errors.append(f"{context} {field_name}: invalid numeric type {type(value).__name__}")


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _positive_frame_count(content: dict, key: str, errors: list[str]) -> int | None:
    if key not in content:
        return None
    value = content[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        errors.append(f"metadata: missing or invalid {key}")
        return None
    return value


def _validate_metadata_json(content: dict, settings) -> dict:
    errors: list[str] = []

    schema_version = content.get("schema_version")
    if not schema_version or not isinstance(schema_version, str):
        errors.append("metadata: missing or invalid schema_version")
    elif schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(
            f"metadata: unsupported schema_version '{schema_version}'; "
            f"supported: {', '.join(SUPPORTED_SCHEMA_VERSIONS)}"
        )

    video_id = content.get("video_id")
    if not video_id or not isinstance(video_id, str):
        errors.append("metadata: missing or invalid video_id")

    width = content.get("width")
    if isinstance(width, bool) or not isinstance(width, (int, float)) or width <= 0:
        errors.append("metadata: missing or invalid width")
    elif isinstance(width, float) and not math.isfinite(width):
        errors.append("metadata: width is NaN or Infinity")

    height = content.get("height")
    if isinstance(height, bool) or not isinstance(height, (int, float)) or height <= 0:
        errors.append("metadata: missing or invalid height")
    elif isinstance(height, float) and not math.isfinite(height):
        errors.append("metadata: height is NaN or Infinity")

    fps = content.get("fps")
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or fps <= 0:
        errors.append("metadata: missing or invalid fps")
    elif isinstance(fps, float) and not math.isfinite(fps):
        errors.append("metadata: fps is NaN or Infinity")

    canonical_frame_count = _positive_frame_count(content, "frame_count", errors)
    processed_frames = _positive_frame_count(content, "processed_frames", errors)
    declared_frame_count = _positive_frame_count(content, "declared_frame_count", errors)

    if "processed_frames" in content and "declared_frame_count" in content:
        if (
            processed_frames is not None
            and declared_frame_count is not None
            and processed_frames > declared_frame_count
        ):
            errors.append(
                "metadata: processed_frames "
                f"({processed_frames}) exceeds declared_frame_count ({declared_frame_count})"
            )

    if "frame_count" in content:
        frame_count = canonical_frame_count
    elif "processed_frames" in content:
        frame_count = processed_frames
    else:
        frame_count = declared_frame_count

    if not any(key in content for key in ("frame_count", "processed_frames", "declared_frame_count")):
        errors.append(
            "metadata: missing frame_count; accepted aliases are processed_frames "
            "and declared_frame_count"
        )

    source_relative = content.get("source_relative")
    if source_relative is not None and not isinstance(source_relative, str):
        errors.append("metadata: source_relative must be a string")

    if errors:
        raise HTTPException(status_code=400, detail={"validation_errors": errors})

    return {
        "schema_version": str(schema_version),
        "video_id": str(video_id),
        "width": int(width),
        "height": int(height),
        "fps": float(fps),
        "frame_count": int(frame_count),
        "processed_frames": processed_frames,
        "declared_frame_count": declared_frame_count,
        "source_relative": source_relative,
        "model_name": content.get("model_name") or content.get("model"),
        "model_weights_sha256": (
            content.get("model_weights_sha256") or content.get("model_sha256")
        ),
        "tracker_name": content.get("tracker_name") or content.get("tracker"),
        "tracker_params": content.get("tracker_params") or content.get("parameters"),
        "keypoint_names": content.get("keypoint_names"),
        "skeleton_edges": content.get("skeleton_edges") or content.get("skeleton_edges_0based"),
    }


def _validate_tracks_jsonl(file_path: Path, meta: dict, settings) -> tuple[set[int], list[tuple[int, int, dict]]]:
    """逐行校验 tracks.jsonl；返回 (seen_frames, all_detections)。

    seen_frames 包含所有出现的帧索引（含 zero-detection 帧），用于 frame_range。
    all_detections 仅包含有实际检测的帧条目。
    """
    errors: list[str] = []
    seen_frames: set[int] = set()
    all_detections: list[tuple[int, int, dict]] = []  # (frame_index, det_index, det)
    max_frame = meta["frame_count"] - 1
    meta_width = meta["width"]
    meta_height = meta["height"]
    meta_video_id = meta["video_id"]
    meta_schema = meta["schema_version"]
    expected_kp_count = len(meta.get("keypoint_names") or [])
    max_frames = settings.detection_import_max_frames
    max_det_per_frame = settings.detection_import_max_detections_per_frame
    max_errors = settings.detection_import_max_errors

    with open(file_path, "r", encoding="utf-8") as fh:
        for line_num, line in enumerate(fh, 1):
            if len(errors) >= max_errors:
                errors.append(
                    f"tracks.jsonl: validation error limit ({max_errors}) reached; "
                    "further errors truncated"
                )
                break

            line = line.strip()
            if not line:
                errors.append(f"tracks.jsonl L{line_num}: empty line")
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"tracks.jsonl L{line_num}: invalid JSON: {e}")
                continue

            if not isinstance(frame, dict):
                errors.append(
                    f"tracks.jsonl L{line_num}: expected JSON object, got {type(frame).__name__}"
                )
                continue

            # 交叉校验 schema_version
            fsv = frame.get("schema_version")
            if not fsv or not isinstance(fsv, str):
                errors.append(
                    f"tracks.jsonl L{line_num}: missing or invalid schema_version"
                )
            elif fsv != meta_schema:
                errors.append(
                    f"tracks.jsonl L{line_num}: schema_version '{fsv}' "
                    f"does not match metadata '{meta_schema}'"
                )

            # 交叉校验 video_id
            fvid = frame.get("video_id")
            if isinstance(fvid, str) and fvid != meta_video_id:
                errors.append(
                    f"tracks.jsonl L{line_num}: video_id '{fvid}' "
                    f"does not match metadata '{meta_video_id}'"
                )

            fi = frame.get("frame_index")
            if isinstance(fi, bool) or not isinstance(fi, int) or fi < 0:
                errors.append(f"tracks.jsonl L{line_num}: missing or invalid frame_index")
                continue
            if fi > max_frame:
                errors.append(
                    f"tracks.jsonl L{line_num}: frame_index {fi} exceeds metadata frame_count {meta['frame_count']}"
                )
            if fi in seen_frames:
                errors.append(f"tracks.jsonl L{line_num}: duplicate frame_index {fi}")

            if len(seen_frames) >= max_frames:
                errors.append(
                    f"tracks.jsonl: frame count exceeds limit ({max_frames}); import aborted"
                )
                break

            seen_frames.add(fi)

            # 校验 timestamp_sec
            ts = frame.get("timestamp_sec")
            if ts is not None:
                if not _is_finite_number(ts) or ts < 0:
                    errors.append(
                        f"tracks.jsonl L{line_num}: timestamp_sec must be a non-negative finite number"
                    )

            dc = frame.get("detection_count", 0)
            if isinstance(dc, bool):
                errors.append(
                    f"tracks.jsonl L{line_num}: detection_count is boolean, expected integer"
                )
                dc = -1
            elif not isinstance(dc, int):
                errors.append(
                    f"tracks.jsonl L{line_num}: detection_count must be an integer"
                )
                dc = -1

            dets = frame.get("detections", [])
            if not isinstance(dets, list):
                errors.append(f"tracks.jsonl L{line_num}: detections is not a list")
                continue

            if dc >= 0 and dc != len(dets):
                errors.append(
                    f"tracks.jsonl L{line_num}: detection_count {dc} != len(detections) {len(dets)}"
                )

            if len(dets) > max_det_per_frame:
                errors.append(
                    f"tracks.jsonl L{line_num}: {len(dets)} detections exceeds per-frame limit ({max_det_per_frame})"
                )

            for di, det in enumerate(dets):
                if not isinstance(det, dict):
                    errors.append(f"tracks.jsonl L{line_num} detection[{di}]: not an object")
                    continue

                ctx = f"tracks.jsonl L{line_num} detection[{di}]"

                # track_id
                tid = det.get("track_id")
                if isinstance(tid, bool) or not isinstance(tid, int):
                    errors.append(f"{ctx}: missing or invalid track_id")
                    continue
                if not is_valid_track_id(tid):
                    errors.append(
                        f"{ctx}: track_id must satisfy 0 <= id < {TRACK_ID_UPPER_BOUND}"
                    )
                    continue

                # box_xyxy_px
                box = det.get("box_xyxy_px")
                if box is not None:
                    if not isinstance(box, list) or len(box) != 4:
                        errors.append(f"{ctx}: invalid box_xyxy_px (expected list of 4 numbers)")
                    else:
                        for vi, v in enumerate(box):
                            if not _is_finite_number(v):
                                errors.append(f"{ctx}: box_xyxy_px[{vi}] is not a finite number")
                        if all(_is_finite_number(v) for v in box):
                            x1, y1, x2, y2 = float(box[0]), float(box[1]), float(box[2]), float(box[3])
                            if x1 >= x2:
                                errors.append(f"{ctx}: box_xyxy_px x1 ({x1}) >= x2 ({x2})")
                            if y1 >= y2:
                                errors.append(f"{ctx}: box_xyxy_px y1 ({y1}) >= y2 ({y2})")
                            # 越界检查（宽松：允许少量超出边界）
                            margin = 10
                            if x1 < -margin or y1 < -margin or x2 > meta_width + margin or y2 > meta_height + margin:
                                errors.append(
                                    f"{ctx}: box_xyxy_px [{x1},{y1},{x2},{y2}] "
                                    f"far outside image bounds ({meta_width}x{meta_height})"
                                )

                # detection_confidence
                conf = det.get("detection_confidence")
                if conf is not None:
                    if isinstance(conf, bool):
                        errors.append(f"{ctx}: detection_confidence is boolean, expected number")
                    elif not isinstance(conf, (int, float)):
                        errors.append(f"{ctx}: detection_confidence is not a number")
                    elif isinstance(conf, float) and not math.isfinite(conf):
                        errors.append(f"{ctx}: detection_confidence is NaN or Infinity")
                    elif conf < 0 or conf > 1:
                        errors.append(f"{ctx}: detection_confidence {conf} out of [0,1]")

                # keypoints
                kp = det.get("keypoints")
                if kp is not None:
                    if not isinstance(kp, list):
                        errors.append(f"{ctx}: keypoints is not a list")
                    else:
                        if expected_kp_count > 0 and len(kp) != expected_kp_count:
                            errors.append(
                                f"{ctx}: keypoint count {len(kp)} != "
                                f"metadata keypoint_names count {expected_kp_count}"
                            )
                        for ki, k in enumerate(kp):
                            kctx = f"{ctx} keypoints[{ki}]"
                            if not isinstance(k, dict):
                                errors.append(f"{kctx}: not an object")
                                continue
                            for kf in ("x_px", "y_px", "confidence"):
                                kv = k.get(kf)
                                if kv is None:
                                    errors.append(f"{kctx}: missing '{kf}'")
                                elif kf == "confidence":
                                    if isinstance(kv, bool):
                                        errors.append(f"{kctx}: confidence is boolean, expected number")
                                    elif not isinstance(kv, (int, float)):
                                        errors.append(f"{kctx}: confidence is not a number")
                                    elif isinstance(kv, float) and not math.isfinite(kv):
                                        errors.append(f"{kctx}: confidence is NaN or Infinity")
                                    elif kv < 0 or kv > 1:
                                        errors.append(f"{kctx}: confidence {kv} out of [0,1]")
                                else:
                                    if isinstance(kv, bool):
                                        errors.append(f"{kctx}: {kf} is boolean, expected number")
                                    elif not isinstance(kv, (int, float)):
                                        errors.append(f"{kctx}: {kf} is not a number")
                                    elif isinstance(kv, float) and not math.isfinite(kv):
                                        errors.append(f"{kctx}: {kf} is NaN or Infinity")
                                    elif kv < 0:
                                        errors.append(f"{kctx}: {kf} is negative ({kv})")

                # class_id
                cid = det.get("class_id")
                if cid is not None:
                    _validate_numeric_field(cid, "class_id", errors, ctx)

                all_detections.append((fi, di, det))

    if errors:
        raise HTTPException(status_code=400, detail={"validation_errors": errors})

    # 交叉校验：JSONL 覆盖帧数 vs metadata frame_count
    jsonl_frames = len(seen_frames)
    if jsonl_frames != meta["frame_count"]:
        extra_detail = (
            f"metadata declares {meta['frame_count']} frames, "
            f"tracks.jsonl covers {jsonl_frames} frames"
        )
        errors.append(extra_detail)
        raise HTTPException(status_code=400, detail={"validation_errors": errors})

    return seen_frames, all_detections


# ---------------------------------------------------------------------------
# 公共校验流水线
# ---------------------------------------------------------------------------

def _run_validation_pipeline(
    tracks_path: str,
    metadata_path: str,
    detection_imports_dir: Path,
    settings,
) -> tuple[dict, set[int], list[tuple[int, int, dict]]]:
    """读取并校验 metadata.json + tracks.jsonl。

    返回 (meta_info, seen_frames, flat_detections)。
    seen_frames 含所有帧（含 zero-detection 帧）。
    """
    meta_path = detection_imports_dir / metadata_path
    jsonl_path = detection_imports_dir / tracks_path

    if not meta_path.is_file():
        raise HTTPException(status_code=400, detail="metadata.json file not found on disk")
    if not jsonl_path.is_file():
        raise HTTPException(status_code=400, detail="tracks.jsonl file not found on disk")

    with open(meta_path, "r", encoding="utf-8") as fh:
        try:
            meta_raw = json.load(fh)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"metadata.json is not valid JSON: {e}")

    if not isinstance(meta_raw, dict):
        raise HTTPException(
            status_code=400,
            detail={"validation_errors": [
                f"metadata.json: expected JSON object, got {type(meta_raw).__name__}"
            ]},
        )

    meta_info = _validate_metadata_json(meta_raw, settings)
    seen_frames, flat_detections = _validate_tracks_jsonl(jsonl_path, meta_info, settings)
    return meta_info, seen_frames, flat_detections


def _streaming_sha256(file_path: Path) -> str:
    """流式计算文件 SHA256，避免大文件全部读入内存。"""
    h = hashlib.sha256()
    with open(file_path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)  # 1 MB chunks
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _insert_detection_import_data(
    db: Session,
    video_id: int,
    revision: int,
    meta_info: dict,
    tracks_rel_path: str,
    metadata_rel_path: str,
    seen_frames: set[int],
    flat_detections: list[tuple[int, int, dict]],
    user_id: int,
    detection_imports_dir: Path,
) -> DetectionImport:
    """在同一事务内创建 DetectionImport 与 immutable RawDetection baseline。"""
    tracks_full = detection_imports_dir / tracks_rel_path
    metadata_full = detection_imports_dir / metadata_rel_path
    tracks_sha = _streaming_sha256(tracks_full)
    metadata_sha = _streaming_sha256(metadata_full)

    # frame_range 来自 JSONL 中出现的所有帧（含 zero-detection 帧）
    if seen_frames:
        first_frame = min(seen_frames)
        last_frame = max(seen_frames)
    else:
        first_frame = 0
        last_frame = 0

    imp = DetectionImport(
        video_id=video_id,
        revision=revision,
        schema_version=meta_info["schema_version"],
        tracks_path=tracks_rel_path,
        tracks_sha256=tracks_sha,
        metadata_path=metadata_rel_path,
        metadata_sha256=metadata_sha,
        model_name=meta_info.get("model_name"),
        model_weights_sha256=meta_info.get("model_weights_sha256"),
        tracker_name=meta_info.get("tracker_name"),
        tracker_params=meta_info.get("tracker_params"),
        width=meta_info["width"],
        height=meta_info["height"],
        fps=meta_info["fps"],
        frame_count=meta_info["frame_count"],
        frame_range={"first_frame": first_frame, "last_frame": last_frame},
        detection_count=len(flat_detections),
        source_relative=meta_info.get("source_relative"),
        status="imported",
        active=True,
        edit_version=0,
        next_display_track_id=0,
        created_by=user_id,
    )
    db.add(imp)
    db.flush()

    # 批量插入 RawDetection（每 500 条 flush 一次）
    if flat_detections:
        batch = []
        for idx, (fi, di, det) in enumerate(flat_detections):
            box_data = None
            box = det.get("box_xyxy_px")
            if box and isinstance(box, list) and len(box) == 4:
                box_data = {"x1": box[0], "y1": box[1], "x2": box[2], "y2": box[3]}

            rd = RawDetection(
                detection_import_id=imp.id,
                frame_index=fi,
                frame_detection_index=di,
                raw_track_id=det.get("track_id", 0),
                box=box_data,
                keypoints=det.get("keypoints"),
                detection_confidence=det.get("detection_confidence"),
                class_id=det.get("class_id"),
            )
            batch.append(rd)
            if len(batch) >= 500:
                db.add_all(batch)
                db.flush()
                batch = []
        if batch:
            db.add_all(batch)
            db.flush()

    # 收集唯一 raw_track_id
    unique_tids: dict[int, dict] = {}  # raw_track_id → {first_frame, last_frame, count}
    for fi, _di, det in flat_detections:
        tid = det.get("track_id", 0)
        if tid not in unique_tids:
            unique_tids[tid] = {"first_frame": fi, "last_frame": fi, "count": 0}
        info = unique_tids[tid]
        info["first_frame"] = min(info["first_frame"], fi)
        info["last_frame"] = max(info["last_frame"], fi)
        info["count"] += 1

    # Fresh imports initialize the monotonic sparse-edit display-ID cursor.
    imp.next_display_track_id = next_display_track_id(unique_tids)

    return imp


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

@router.post(
    "/api/projects/{project_id}/video-import-batches",
    response_model=VideoImportBatchOut,
    status_code=201,
)
def create_import_batch(
    project_id: int,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> VideoImportBatch:
    _project, membership = access
    if membership.status != "active":
        raise HTTPException(status_code=403, detail="Project membership is not active")

    batch = VideoImportBatch(project_id=project_id)
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


@router.put(
    "/api/projects/{project_id}/video-import-batches/{batch_id}/files/{role}",
    response_model=VideoImportBatchOut,
)
async def upload_batch_file(
    project_id: int,
    batch_id: int,
    role: str,
    request: Request,
    file: UploadFile = File(...),
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
) -> VideoImportBatch:
    if role not in ("video", "tracks", "metadata"):
        raise HTTPException(status_code=400, detail="Invalid role; use video, tracks, or metadata")

    _project, membership = access
    if membership.status != "active":
        raise HTTPException(status_code=403, detail="Project membership is not active")

    batch = _require_batch(db, batch_id, project_id)

    raw_name = file.filename or ""
    if not raw_name:
        raise HTTPException(status_code=400, detail="Upload filename is required")
    try:
        display_name = _safe_display_name(raw_name)
    except ValueError:
        raise HTTPException(status_code=400, detail="Upload filename is invalid")
    _prevent_traversal(raw_name)

    settings = request.app.state.settings

    # Fix 5: 文件大小限制（非视频文件）
    if role in ("tracks", "metadata") and file.size is not None:
        if file.size > settings.detection_import_max_file_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"{role} file too large: {file.size} bytes "
                f"(max {settings.detection_import_max_file_bytes} bytes)",
            )

    if role == "video":
        ext = display_name.rsplit(".", 1)[-1].lower() if "." in display_name else ""
        if ext not in {"mp4", "mov", "avi", "mkv", "webm", "m4v", "wmv", "mpeg", "mpg"}:
            raise HTTPException(status_code=400, detail="Video file extension is not allowed")
        suffix = f".{ext}" if ext else ".mp4"
        dir_path = settings.videos_dir.resolve()
    elif role == "tracks":
        suffix = ALLOWED_TRACKS_EXT
        dir_path = settings.detection_imports_dir.resolve()
    else:  # metadata
        suffix = ALLOWED_METADATA_EXT
        dir_path = settings.detection_imports_dir.resolve()

    _check_non_empty(file)

    try:
        final_path, written = await _atomic_save_async(
            file, dir_path, suffix, settings.upload_chunk_size,
            settings.upload_disk_reserve_bytes,
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to save uploaded file")
    finally:
        await file.close()

    if written == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    # Fix 5: 落盘后二次检查文件大小
    if role in ("tracks", "metadata"):
        actual_size = final_path.stat().st_size
        if actual_size > settings.detection_import_max_file_bytes:
            _remove_if_exists(final_path)
            raise HTTPException(
                status_code=413,
                detail=f"{role} file too large after save: {actual_size} bytes "
                f"(max {settings.detection_import_max_file_bytes} bytes)",
            )

    rel_path = final_path.name

    if role == "video":
        batch.video_path = rel_path
        batch.video_filename = display_name
        batch.video_upload_state = "uploaded"
    elif role == "tracks":
        batch.tracks_path = rel_path
        batch.tracks_upload_state = "uploaded"
    else:
        batch.metadata_path = rel_path
        batch.metadata_upload_state = "uploaded"

    db.commit()
    db.refresh(batch)
    return batch


def _check_upload_disk_space(dir_path: Path, reserve: int, extra: int = 0) -> None:
    if shutil.disk_usage(dir_path).free - reserve < extra:
        raise HTTPException(status_code=507, detail="Insufficient disk space to store upload")


async def _atomic_save_async(
    file: UploadFile, dir_path: Path, suffix: str, chunk_size: int, reserve: int
) -> tuple[Path, int]:
    """按块检查容量并原子保存上传。"""
    dir_path.mkdir(parents=True, exist_ok=True)
    uid = uuid4().hex
    temp_path = dir_path / f"{uid}.part"
    final_name = f"{uid}{suffix}"
    final_path = dir_path / final_name
    written = 0
    temp_file = None
    try:
        _check_upload_disk_space(dir_path, reserve)
        temp_file = open(temp_path, "wb")
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            _check_upload_disk_space(dir_path, reserve, len(chunk))
            temp_file.write(chunk)
            written += len(chunk)
        temp_file.flush()
        os.fsync(temp_file.fileno())
        temp_file.close()
        temp_file = None
        if written == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")
        os.replace(temp_path, final_path)
        return final_path, written
    finally:
        if temp_file is not None:
            try:
                temp_file.close()
            except OSError:
                pass
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _media_status_for_ext(ext: str) -> str:
    PLAYABLE = {"mp4", "webm", "mov", "m4v"}
    return "uploaded" if ext in PLAYABLE else "needs_transcode"


@router.post("/api/projects/{project_id}/video-import-batches/{batch_id}/complete")
def complete_import_batch(
    project_id: int,
    batch_id: int,
    request: Request,
    assignee_membership_id: int | None = None,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
):
    _project, membership = access
    if membership.status != "active":
        raise HTTPException(status_code=403, detail="Project membership is not active")
    if assignee_membership_id is not None:
        if not is_manager(membership):
            raise HTTPException(status_code=403, detail="Only owner/admin may specify an assignee")
        assignee = db.get(ProjectMembership, assignee_membership_id)
        if assignee is None or assignee.project_id != project_id or assignee.status != "active":
            raise HTTPException(status_code=400, detail="Assignee must be an active member of this project")

    batch = _require_batch(db, batch_id, project_id)

    # Fix 1: 幂等 — 已成功的批次直接返回已有结果
    if batch.status == "ready" and batch.created_video_id is not None:
        return _build_ready_response(db, batch)

    if batch.status == "video_only" and batch.created_video_id is not None:
        return _build_video_only_response(batch)

    # Fix 1: 并发拒绝 — 正在处理的批次返回 409
    if batch.status == "processing":
        raise HTTPException(status_code=409, detail="Import batch is already being processed")

    if batch.status == "failed":
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Import batch previously failed. Create a new batch to retry.",
                "previous_errors": batch.validation_errors,
            },
        )

    if batch.video_upload_state != "uploaded" or not batch.video_path:
        raise HTTPException(status_code=400, detail="Video file must be uploaded before completing the batch")

    settings = request.app.state.settings
    videos_dir = settings.videos_dir.resolve()
    video_path = videos_dir / batch.video_path
    if not video_path.is_file():
        raise HTTPException(status_code=400, detail="Video file not found on disk")

    # Fix 1: 先标记 processing 防止并发
    batch.status = "processing"
    batch.validation_errors = None
    db.commit()

    # Fix 1: 使用已有 video_id，避免重复创建视频
    if batch.created_video_id is not None:
        video = db.get(Video, batch.created_video_id)
        if video is None:
            # video 被外部删除，创建新的
            batch.created_video_id = None
            video = None
        else:
            video_id_created = video.id
    else:
        video = None
        video_id_created = None

    if video is None:
        ext = (
            batch.video_filename.rsplit(".", 1)[-1].lower()
            if batch.video_filename and "." in batch.video_filename
            else ""
        )
        video = Video(
            project_id=project_id,
            filename=batch.video_filename or "imported_video",
            storage_path=batch.video_path,
            status=_media_status_for_ext(ext),
            uploaded_by=membership.user_id,
            workflow_status="draft",
            annotation_revision=1,
            detection_import_revision=0,
            identity_revision=0,
            assignee_membership_id=assignee_membership_id,
        )
        try:
            db.add(video)
            db.flush()
            video_id_created = video.id
            batch.created_video_id = video_id_created
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            if not is_assignee_write_conflict(exc):
                raise
            # ``processing`` was committed before video creation. Restore the
            # retryable pre-completion state instead of stranding the batch.
            batch = _require_batch(db, batch_id, project_id)
            batch.status = "uploading"
            batch.created_video_id = None
            batch.validation_errors = {"assignee_conflict": ASSIGNEE_CONFLICT_DETAIL}
            db.commit()
            raise HTTPException(status_code=409, detail=ASSIGNEE_CONFLICT_DETAIL) from None
    else:
        video_id_created = video.id

    tracks_ready = batch.tracks_upload_state == "uploaded" and batch.tracks_path
    metadata_ready = batch.metadata_upload_state == "uploaded" and batch.metadata_path

    if not tracks_ready or not metadata_ready:
        # Fix 1: 仅有视频无检测数据 → video_only 状态
        batch.status = "video_only"
        db.commit()
        db.refresh(batch)
        return _build_video_only_response(batch)

    # 先提交视频，确保视频行不因后续导入失败而回滚
    db.commit()

    detection_imports_dir = settings.detection_imports_dir.resolve()

    # Fix 1: DB 级别事务校验 — 在事务中重新读取 batch 确认状态仍为 processing
    try:
        meta_info, seen_frames, flat_detections = _run_validation_pipeline(
            batch.tracks_path, batch.metadata_path, detection_imports_dir, settings
        )
        _validate_source_filename(meta_info, batch.video_filename or video.filename)
    except HTTPException as exc:
        batch = _require_batch(db, batch_id, project_id)
        if isinstance(exc.detail, dict) and "validation_errors" in exc.detail:
            batch.validation_errors = exc.detail["validation_errors"]
        else:
            batch.validation_errors = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
        batch.status = "failed"
        db.commit()
        db.refresh(batch)
        validation_errors = (
            exc.detail.get("validation_errors", []) if isinstance(exc.detail, dict) else []
        )
        if any("track_id must satisfy" in str(error) for error in validation_errors):
            # Domain violations are rejected as input errors before ORM insertion;
            # retain the failed batch record while preserving HTTP 400 semantics.
            raise exc
        return {
            "batch_id": batch.id,
            "video_id": batch.created_video_id,
            "created_video_id": batch.created_video_id,
            "status": "failed",
            "validation_errors": batch.validation_errors,
            "message": "Validation failed. Fix the files and create a new batch to retry.",
        }

    try:
        # Fix 1: DB 级检查 — 事务中重新读取 batch
        batch_check = db.get(VideoImportBatch, batch_id)
        if batch_check is None or batch_check.status != "processing":
            raise HTTPException(
                status_code=409,
                detail="Batch state changed during processing; concurrent completion detected",
            )

        imp = _insert_detection_import_data(
            db=db,
            video_id=video_id_created,
            revision=1,
            meta_info=meta_info,
            tracks_rel_path=batch.tracks_path,
            metadata_rel_path=batch.metadata_path,
            seen_frames=seen_frames,
            flat_detections=flat_detections,
            user_id=membership.user_id,
            detection_imports_dir=detection_imports_dir,
        )
        video_ref = db.get(Video, video_id_created)
        video_ref.fps = meta_info["fps"]
        video_ref.width = meta_info["width"]
        video_ref.height = meta_info["height"]
        video_ref.duration = meta_info["frame_count"] / meta_info["fps"]
        video_ref.detection_import_revision = 1
        batch_ref = _require_batch(db, batch_id, project_id)
        batch_ref.status = "ready"
        db.commit()
    except HTTPException:
        db.rollback()
        batch_ref = _require_batch(db, batch_id, project_id)
        batch_ref.status = "failed"
        batch_ref.validation_errors = {"import_failed": True}
        db.commit()
        raise
    except Exception:
        db.rollback()
        batch_ref = _require_batch(db, batch_id, project_id)
        batch_ref.status = "failed"
        batch_ref.validation_errors = {"import_failed": True}
        db.commit()
        raise HTTPException(status_code=500, detail="Failed to import detection data")

    return _build_ready_response(db, batch_ref, imp)


def _build_ready_response(db: Session, batch: VideoImportBatch, imp: DetectionImport | None = None) -> dict:
    """构建完成状态的响应。如果已有 imp 则使用；否则从 DB 查找当前活动的导入。"""
    if imp is None and batch.created_video_id:
        imp = (
            db.query(DetectionImport)
            .filter(
                DetectionImport.video_id == batch.created_video_id,
                DetectionImport.active == True,
            )
            .first()
        )

    if imp is not None:
        return {
            "batch_id": batch.id,
            "video_id": batch.created_video_id,
            "created_video_id": batch.created_video_id,
            "detection_import_id": imp.id,
            "revision": imp.revision,
            "detection_count": imp.detection_count,
            "status": "ready",
            "message": "Detection import completed successfully.",
        }
    else:
        return _build_video_only_response(batch)


def _build_video_only_response(batch: VideoImportBatch) -> dict:
    return {
        "batch_id": batch.id,
        "video_id": batch.created_video_id,
        "created_video_id": batch.created_video_id,
        "status": "video_only" if batch.status == "video_only" else "ready",
        "message": "Video created. No detection data imported (tracks/metadata missing). Mouse ID features unavailable.",
    }


@router.get(
    "/api/projects/{project_id}/video-import-batches/{batch_id}",
    response_model=BatchStatusOut,
)
def get_import_batch(
    project_id: int,
    batch_id: int,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
):
    _project, membership = access
    batch = _require_batch(db, batch_id, project_id)
    return batch


# ---------------------------------------------------------------------------
# 已有视频的检测导入操作
# ---------------------------------------------------------------------------

@router.post("/api/projects/{project_id}/videos/{video_id}/detection-imports")
async def replace_detection_import(
    project_id: int,
    video_id: int,
    request: Request,
    tracks_file: UploadFile = File(...),
    metadata_file: UploadFile = File(...),
    confirm: bool = Query(False, description="Confirmation required to proceed with replacement"),
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
):
    _project, membership = access
    if membership.status != "active":
        raise HTTPException(status_code=403, detail="Project membership is not active")

    video = _require_video(db, video_id, project_id)
    initial_active = db.query(DetectionImport).filter_by(video_id=video_id, active=True).first()
    initial_active_id = initial_active.id if initial_active else None
    initial_edit_version = initial_active.edit_version if initial_active else None
    initial_detection_revision = video.detection_import_revision

    settings = request.app.state.settings
    detection_imports_dir = settings.detection_imports_dir.resolve()

    # Fix 5: 文件大小限制
    for role, file in [("tracks", tracks_file), ("metadata", metadata_file)]:
        if file.size is not None and file.size > settings.detection_import_max_file_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"{role} file too large: {file.size} bytes "
                f"(max {settings.detection_import_max_file_bytes} bytes)",
            )

    # 保存文件
    _prevent_traversal(tracks_file.filename or "")
    _prevent_traversal(metadata_file.filename or "")
    _check_non_empty(tracks_file)
    _check_non_empty(metadata_file)

    tracks_final: Path | None = None
    metadata_final: Path | None = None
    try:
        tracks_final, _tw = await _atomic_save_async(
            tracks_file, detection_imports_dir, ALLOWED_TRACKS_EXT,
            settings.upload_chunk_size, settings.upload_disk_reserve_bytes,
        )
        metadata_final, _mw = await _atomic_save_async(
            metadata_file, detection_imports_dir, ALLOWED_METADATA_EXT,
            settings.upload_chunk_size, settings.upload_disk_reserve_bytes,
        )
    except HTTPException:
        if tracks_final is not None:
            _remove_if_exists(tracks_final)
        if metadata_final is not None:
            _remove_if_exists(metadata_final)
        raise
    except Exception:
        if tracks_final is not None:
            _remove_if_exists(tracks_final)
        if metadata_final is not None:
            _remove_if_exists(metadata_final)
        raise HTTPException(status_code=500, detail="Failed to save detection files")
    finally:
        await tracks_file.close()
        await metadata_file.close()

    tracks_rel = tracks_final.name
    metadata_rel = metadata_final.name

    # Fix 5: 落盘后大小检查
    for role, path in [("tracks", tracks_final), ("metadata", metadata_final)]:
        actual_size = path.stat().st_size
        if actual_size > settings.detection_import_max_file_bytes:
            _remove_if_exists(tracks_final)
            _remove_if_exists(metadata_final)
            raise HTTPException(
                status_code=413,
                detail=f"{role} file too large after save: {actual_size} bytes "
                f"(max {settings.detection_import_max_file_bytes} bytes)",
            )

    # 校验
    try:
        meta_info, seen_frames, flat_detections = _run_validation_pipeline(tracks_rel, metadata_rel, detection_imports_dir, settings)
        _validate_replacement_compatibility(db, video, meta_info)
    except Exception:
        _remove_if_exists(tracks_final)
        _remove_if_exists(metadata_final)
        raise

    # Fix 4: 确认步骤 — 不确认时返回受影响摘要预览
    if not confirm:
        # 计算受影响数量预览
        existing_active = (
            db.query(DetectionImport)
            .filter(DetectionImport.video_id == video_id, DetectionImport.active == True)
            .first()
        )
        affected_annotations = (
            db.query(Annotation)
            .filter(Annotation.video_id == video_id)
            .count()
        )
        preview = {
            "preview": True,
            "message": (
                "Replacing detection import will deactivate the current import"
                + (f" (revision {existing_active.revision})" if existing_active else "")
                + f", reset identity revision to 0, and mark {affected_annotations} "
                "existing annotation(s) as 'needs_mouse_ids'. "
                "Pass confirm=true to proceed."
            ),
            "current_revision": video.detection_import_revision,
            "new_revision": video.detection_import_revision + 1,
            "affected_annotations_count": affected_annotations,
            "detection_count": len(flat_detections),
            "unique_track_count": len({d[2].get("track_id", 0) for d in flat_detections}),
        }
        _remove_if_exists(tracks_final)
        _remove_if_exists(metadata_final)
        return preview

    try:
        with video_write_gate(
            db, project_id=project_id, video_id=video_id,
            expected_active_import_id=initial_active_id,
            expected_detection_revision=initial_detection_revision,
            expected_edit_version=initial_edit_version,
        ) as state:
            video = state.video
            new_revision = video.detection_import_revision + 1
            old_active = state.detection_import
            if old_active is not None:
                db.query(DetectionStateOverride).filter(
                    DetectionStateOverride.detection_import_id == old_active.id
                ).delete(synchronize_session=False)
                db.query(DraftIdentityEdit).filter(
                    DraftIdentityEdit.detection_import_id == old_active.id
                ).delete(synchronize_session=False)
                old_active.active = False

            imp = _insert_detection_import_data(
                db=db, video_id=video_id, revision=new_revision, meta_info=meta_info,
                tracks_rel_path=tracks_rel, metadata_rel_path=metadata_rel,
                seen_frames=seen_frames, flat_detections=flat_detections,
                user_id=membership.user_id, detection_imports_dir=detection_imports_dir,
            )
            video.detection_import_revision = new_revision
            video.identity_revision = 0
            affected_annotations = db.query(Annotation).filter(
                Annotation.video_id == video_id
            ).update({
                "mouse_id_status": "needs_mouse_ids",
                "detection_import_revision": 0,
                "identity_revision": 0,
            }, synchronize_session=False)
            db.query(Annotation).filter(
                Annotation.video_id == video_id, Annotation.review_status == "approved"
            ).update({"review_status": "pending", "reviewer_id": None}, synchronize_session=False)
            if video.workflow_status == "approved":
                video.workflow_status = "draft"
                video.submitted_at = None
                video.approved_at = None
                video.approved_by = None
            db.commit()
    except Exception:
        db.rollback()
        _remove_if_exists(tracks_final)
        _remove_if_exists(metadata_final)
        raise

    track_count = db.query(RawDetection.raw_track_id).filter(
        RawDetection.detection_import_id == imp.id
    ).distinct().count()

    return {
        "id": imp.id,
        "video_id": imp.video_id,
        "revision": imp.revision,
        "detection_count": imp.detection_count,
        "track_count": track_count,
        "status": imp.status,
        "affected_annotations_count": affected_annotations,
        "message": (
            f"Detection import revision {new_revision} created. "
            f"{affected_annotations} existing annotation(s) set to needs_mouse_ids. "
            "Old imports preserved. Annotations need re-confirmation of mouse_ids."
        ),
    }


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


@router.get(
    "/api/projects/{project_id}/videos/{video_id}/detection-imports/current",
    response_model=DetectionImportCurrentOut,
)
def get_current_detection_import(
    project_id: int,
    video_id: int,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
):
    _project, membership = access
    _require_video(db, video_id, project_id)

    imp = (
        db.query(DetectionImport)
        .filter(DetectionImport.video_id == video_id, DetectionImport.active == True)
        .first()
    )
    if imp is None:
        raise HTTPException(status_code=404, detail="No active detection import for this video")
    return imp


@router.get("/api/projects/{project_id}/videos/{video_id}/detections")
def get_detections(
    project_id: int,
    video_id: int,
    start_frame: int = 0,
    end_frame: int | None = None,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
):
    _project, membership = access
    video = _require_video(db, video_id, project_id)

    imp = (
        db.query(DetectionImport)
        .filter(DetectionImport.video_id == video_id, DetectionImport.active == True)
        .first()
    )
    if imp is None:
        return {"detections": [], "total": 0}

    if end_frame is None:
        end_frame = start_frame + 100
    query = effective_detection_query(
        db, imp.id, start_frame=start_frame, end_frame=end_frame
    )

    total = query.count()
    rows = query.order_by(RawDetection.frame_index, RawDetection.frame_detection_index).limit(500).all()

    results = []
    for row in rows:
        raw = row.RawDetection
        box = raw.box
        box_xyxy_px = None
        if box and isinstance(box, dict) and all(k in box for k in ("x1", "y1", "x2", "y2")):
            box_xyxy_px = [box["x1"], box["y1"], box["x2"], box["y2"]]

        results.append({
            "detection_id": raw.id,
            "frame_index": raw.frame_index,
            "raw_track_id": raw.raw_track_id,
            "display_track_id": row.display_track_id,
            "box_xyxy_px": box_xyxy_px,
            "keypoints": raw.keypoints,
            "confidence": raw.detection_confidence,
            "import_revision": imp.revision,
            "identity_revision": imp.edit_version,
        })

    return {"detections": results, "total": total}


@router.get("/api/projects/{project_id}/videos/{video_id}/corrected-tracks")
def get_corrected_tracks(
    project_id: int,
    video_id: int,
    current_frame: int | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
):
    _project, membership = access
    video = _require_video(db, video_id, project_id)

    imp = (
        db.query(DetectionImport)
        .filter(DetectionImport.video_id == video_id, DetectionImport.active == True)
        .first()
    )
    if imp is None:
        return {"items": [], "total": 0, "page": page, "page_size": page_size, "pages": 0}

    summaries = effective_track_summary_query(db, imp.id).order_by("display_track_id").all()
    if search:
        summaries = [row for row in summaries if str(row.display_track_id).startswith(search)]

    total = len(summaries)
    pages = max(1, math.ceil(total / page_size)) if total > 0 else 0
    summaries = summaries[(page - 1) * page_size:page * page_size]

    items = []
    for row in summaries:
        visible = None
        if current_frame is not None:
            visible = (
                effective_detection_query(
                    db,
                    imp.id,
                    start_frame=current_frame,
                    end_frame=current_frame,
                    display_track_id=row.display_track_id,
                )
                .first()
                is not None
            )

        items.append({
            "display_track_id": row.display_track_id,
            "first_frame": row.first_frame,
            "last_frame": row.last_frame,
            "detection_count": row.detection_count,
            "visible_in_current_frame": visible,
        })

    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": pages}


def _load_import_pose_metadata(
    imp: DetectionImport, detection_imports_dir: Path | None
) -> dict:
    if detection_imports_dir is None or not imp.metadata_path:
        return {"keypoint_names": None, "skeleton_edges": None}

    root = detection_imports_dir.resolve()
    metadata_path = (root / imp.metadata_path).resolve()
    if not metadata_path.is_relative_to(root) or not metadata_path.is_file():
        return {"keypoint_names": None, "skeleton_edges": None}

    try:
        content = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"keypoint_names": None, "skeleton_edges": None}
    if not isinstance(content, dict):
        return {"keypoint_names": None, "skeleton_edges": None}

    return {
        "keypoint_names": content.get("keypoint_names"),
        "skeleton_edges": content.get("skeleton_edges") or content.get("skeleton_edges_0based"),
    }


def generate_corrected_tracks(
    db: Session,
    video: Video,
    imp: DetectionImport,
    id_rev: int,
    detection_imports_dir: Path | None = None,
) -> dict | None:
    if not imp.active or id_rev != imp.edit_version:
        raise HTTPException(
            status_code=409,
            detail="Only the current active detection import/edit version can be exported",
        )
    rows = effective_detection_query(db, imp.id).order_by(
        RawDetection.frame_index, RawDetection.frame_detection_index, RawDetection.id
    ).yield_per(500)

    jsonl_buffer = io.StringIO()
    current_frame = 0
    current_detections: list[dict] = []

    def emit(frame_index: int, detections: list[dict]) -> None:
        jsonl_buffer.write(json.dumps({
            "schema_version": imp.schema_version,
            "video_id": str(video.id),
            "frame_index": frame_index,
            "timestamp_sec": frame_index / imp.fps if imp.fps else None,
            "detection_count": len(detections),
            "detections": detections,
        }, ensure_ascii=False))
        jsonl_buffer.write("\n")

    for row in rows:
        raw = row.RawDetection
        display_track_id = row.display_track_id
        while current_frame < raw.frame_index:
            emit(current_frame, current_detections)
            current_frame += 1
            current_detections = []
        current_detections.append({
            "track_id": display_track_id,
            "box_xyxy_px": (
                [raw.box["x1"], raw.box["y1"], raw.box["x2"], raw.box["y2"]]
                if raw.box and all(key in raw.box for key in ("x1", "y1", "x2", "y2"))
                else None
            ),
            "box_xywhn": None,
            "area_n": None,
            "detection_confidence": raw.detection_confidence,
            "class_id": raw.class_id,
            "keypoints": raw.keypoints,
        })

    while current_frame < (imp.frame_count or 0):
        emit(current_frame, current_detections)
        current_frame += 1
        current_detections = []

    tracks_sha256 = hashlib.sha256()
    tracks_content = jsonl_buffer.getvalue()
    tracks_sha256.update(tracks_content.encode("utf-8"))
    output_sha256 = tracks_sha256.hexdigest()

    pose_metadata = _load_import_pose_metadata(imp, detection_imports_dir)
    manifest = {
        "video_id": str(video.id),
        "file_paths": [],
        "detection_import_revision": imp.revision,
        "identity_revision": id_rev,
        "schema_version": imp.schema_version,
        "source_relative": video.filename,
        "fps": imp.fps,
        "width": imp.width,
        "height": imp.height,
        "frame_count": imp.frame_count,
        "source_tracks_sha256": imp.tracks_sha256,
        "output_sha256": output_sha256,
        "keypoint_names": pose_metadata["keypoint_names"],
        "skeleton_edges": pose_metadata["skeleton_edges"],
    }
    return {
        "tracks_corrected": tracks_content.splitlines(),
        "tracks_corrected_text": tracks_content,
        "manifest": manifest,
    }


@router.get("/api/projects/{project_id}/videos/{video_id}/detections/export")
def export_corrected_detections(
    project_id: int,
    video_id: int,
    request: Request,
    import_revision: int | None = None,
    identity_revision: int | None = None,
    access: tuple = Depends(project_access),
    db: Session = Depends(get_db),
    format: str = "json",
):
    _project, membership = access
    video = _require_video(db, video_id, project_id)

    imp = db.query(DetectionImport).filter(
        DetectionImport.video_id == video_id, DetectionImport.active == True
    ).first()
    if imp is None:
        raise HTTPException(status_code=404, detail="No active detection import for this video")
    if import_revision is not None and import_revision != imp.revision:
        raise HTTPException(status_code=409, detail="Historical detection imports require Phase 3 snapshots")
    id_rev = identity_revision if identity_revision is not None else imp.edit_version
    if id_rev != imp.edit_version:
        raise HTTPException(status_code=409, detail="Historical identity revisions require Phase 3 snapshots")

    initial_import_id = imp.id
    initial_detection_revision = video.detection_import_revision
    initial_edit_version = imp.edit_version
    result = generate_corrected_tracks(
        db, video, imp, id_rev, request.app.state.settings.detection_imports_dir
    )
    db.rollback()
    _after_corrected_export_candidate()
    with video_write_gate(
        db, project_id=project_id, video_id=video_id, require_active_import=True,
        expected_active_import_id=initial_import_id,
        expected_detection_revision=initial_detection_revision,
        expected_edit_version=initial_edit_version,
        allow_submitted=True,
    ):
        db.commit()
    if result is None:
        return {"tracks_corrected": [], "manifest": {}}

    return {
        "tracks_corrected": result["tracks_corrected"],
        "manifest": result["manifest"],
    }
