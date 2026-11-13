"""Single-event input adaptation and judgment transactions; never rewrite originals."""
from __future__ import annotations

import json
import math
from pathlib import Path

from fastapi import HTTPException

from .models import Annotation, BehaviorCategory, Submission
from .permissions import require_editor, require_reviewer
from .submission_service import create_submission, resolve_and_hash_source, validate_annotations
from .behavior_review import ensure_material_evidence, material_values, material_digest
from .media import MediaDependencyError

JSON_LIMIT = 1024 * 1024
TRACKS_LIMIT = 64 * 1024 * 1024


def conflict(code, message=None):
    raise HTTPException(409, detail={"code": code, "message": message or code})


def published_source_condition(source="all"):
    from sqlalchemy import and_, or_
    from .models import Video
    return and_(Video.source.in_(["manual", "heuristic"] if source == "all" else [source]),
                or_(Video.source == "manual", and_(Video.workflow_status == "approved",
                    Video.judgment_status == "usable", Submission.status == "approved")))


def invalid(message, code="invalid_heuristic_input"):
    raise HTTPException(400, detail={"code": code, "validation_errors": [message]})


def _reject_constant(value):
    raise ValueError("Non-finite JSON number")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def read_json(path: Path, limit=JSON_LIMIT):
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            invalid("JSON exceeds size limit")
        return json.loads(raw, parse_constant=_reject_constant, parse_float=_finite_float)
    except (OSError, ValueError, RecursionError):
        invalid("Malformed JSON input")


def normalized_names(batch):
    return (batch.tracks_path + ".normalized.jsonl", batch.metadata_path + ".normalized.json")


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def source_mapping(metadata, annotation, media):
    source = metadata.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("video_filename"), str) or not source["video_filename"].strip():
        invalid("Missing source.video_filename", "unsupported_source_mapping")
    flat_keys = ("start_frame", "end_frame", "start_time", "end_time")
    nested = source.get("clip")
    flat = {k: source.get(k) for k in flat_keys} if any(k in source for k in flat_keys) else None
    if nested is not None and (not isinstance(nested, dict) or flat is not None and any(nested.get(k) != flat[k] for k in flat_keys)):
        invalid("Conflicting source ranges", "unsupported_source_mapping")
    clip = nested if nested is not None else flat
    fps, count = media["fps"], media["frame_count"]
    if (not isinstance(clip, dict) or any(not _integer(clip.get(k)) for k in flat_keys[:2])
            or clip["start_frame"] < 0 or clip["end_frame"] - clip["start_frame"] + 1 != count
            or any(not _number(clip.get(k)) for k in flat_keys[2:])
            or not math.isclose(clip["start_time"], clip["start_frame"] / fps, abs_tol=1e-6)
            or not math.isclose(clip["end_time"], (clip["end_frame"] + 1) / fps, abs_tol=1e-6)):
        invalid("Source must prove one-to-one frame/time mapping", "unsupported_source_mapping")
    s, e = annotation["frame_range"]["start"], annotation["frame_range"]["end"]
    absolute = dict(start_frame=clip["start_frame"] + s, end_frame=clip["start_frame"] + e,
                    start_time=(clip["start_frame"] + s) / fps, end_time=(clip["start_frame"] + e + 1) / fps)
    if "annotation" in source and (not isinstance(source["annotation"], dict) or any(
            source["annotation"].get(k) != value for k, value in absolute.items())):
        invalid("Source annotation disagrees with local event", "unsupported_source_mapping")
    filename = source["video_filename"].replace("\\", "/").rsplit("/", 1)[-1]
    crop = source.get("crop_region")
    if not filename.strip() or (crop is not None and (
            not isinstance(crop, dict) or set(crop) != {"x", "y", "w", "h"}
            or any(not _number(crop[k]) for k in ("x", "y", "w", "h"))
            or crop["x"] < 0 or crop["y"] < 0 or crop["w"] <= 0 or crop["h"] <= 0)):
        invalid("Invalid source filename/crop evidence", "unsupported_source_mapping")
    return dict(video_filename=filename, clip={k: clip[k] for k in flat_keys},
                annotation=absolute, crop_region=crop)


def prepare_input(batch, settings, processor):
    from .routers.detection_imports import _run_validation_pipeline, _streaming_sha256
    from .integrity_canonical import validate_pose_metadata
    root = settings.detection_imports_dir.resolve()
    annotation = read_json(root / batch.annotation_path)
    metadata = read_json(root / batch.metadata_path)
    if (not isinstance(annotation, dict) or any(k in annotation for k in ("events", "Multiple_behavior_candidates"))
            or annotation.get("behavior") == "Multiple_behavior_candidates"):
        invalid("Expected exactly one event object")
    if (not isinstance(metadata, dict) or not isinstance(metadata.get("schema_version"), str)
            or not metadata["schema_version"].strip()):
        invalid("Missing metadata schema_version")
    media = metadata.get("clip", {})
    pose = metadata.get("pose", {})
    tracks_header = metadata.get("tracks", {})
    if not all(isinstance(item, dict) for item in (media, pose, tracks_header)):
        invalid("Invalid metadata sections")
    if (media.get("filename") != "clip.mp4" or any(not _integer(media.get(k)) or media[k] <= 0 for k in ("width", "height", "frame_count"))
            or not _number(media.get("fps")) or media["fps"] <= 0
            or tracks_header.get("frame_origin") != 0 or isinstance(tracks_header.get("frame_origin"), bool)
            or tracks_header.get("time_origin") != 0 or isinstance(tracks_header.get("time_origin"), bool)
            or tracks_header.get("coordinate_system") != "clip_pixels"):
        invalid("Invalid local media/track coordinates")
    if media["frame_count"] > settings.detection_import_max_frames:
        invalid("Too many frames")
    try:
        names, edges = validate_pose_metadata(pose.get("keypoint_names"), pose.get("skeleton_edges"))
    except ValueError:
        invalid("Invalid pose metadata")
    boundary = annotation.get("frame_range")
    if (not isinstance(boundary, dict) or any(not _integer(boundary.get(k)) for k in ("start", "end"))
            or not 0 <= boundary["start"] < boundary["end"] < media["frame_count"]):
        invalid("Invalid single event frame range")
    times = dict(start=boundary["start"] / media["fps"], end=(boundary["end"] + 1) / media["fps"])
    if "time_range" in annotation and (not isinstance(annotation["time_range"], dict) or any(
            not _number(annotation["time_range"].get(k)) or not math.isclose(annotation["time_range"][k], v, abs_tol=1e-6)
            for k, v in times.items())):
        invalid("Event time/frame mismatch")
    mapping = source_mapping(metadata, annotation, media)
    scale = metadata.get("distance_scale")
    if scale is not None and (not isinstance(scale, dict) or set(scale) != {"cm_per_pixel", "unit"}
            or scale.get("unit") != "cm" or not _number(scale.get("cm_per_pixel")) or scale["cm_per_pixel"] <= 0):
        invalid("Invalid input distance_scale")
    expected = {k: media[k] for k in ("fps", "width", "height", "frame_count")}
    try:
        measured = processor.probe_clip(str(settings.videos_dir / batch.video_path), expected=expected)
    except MediaDependencyError:
        raise
    except Exception:
        invalid("Input MP4 probe failed")
    if (any(measured.get(k) != expected[k] for k in ("width", "height", "frame_count"))
            or not _number(measured.get("fps")) or not math.isclose(measured["fps"], media["fps"], rel_tol=1e-6, abs_tol=1e-6)
            or not _number(measured.get("duration")) or abs(measured["duration"] - media["frame_count"] / media["fps"]) > 1 / media["fps"]):
        invalid("Input MP4 does not match metadata")
    # ponytail: cap the array at 64 MiB; use incremental decoding if real accepted inputs exceed this ceiling.
    frames = read_json(root / batch.tracks_path, TRACKS_LIMIT)
    if not isinstance(frames, list) or len(frames) != media["frame_count"]:
        invalid("tracks must contain every local frame exactly once")
    tracks_name, meta_name = normalized_names(batch)
    normalized_meta = dict(schema_version="1.0", video_id="clip", **expected,
                           keypoint_names=names, skeleton_edges=edges)
    try:
        with (root / (tracks_name + ".part")).open("x", encoding="utf-8") as stream:
            for index, frame in enumerate(frames):
                if (not isinstance(frame, dict) or not _integer(frame.get("frame")) or frame["frame"] != index
                        or not _number(frame.get("time")) or not math.isclose(frame["time"], index / media["fps"], abs_tol=1e-6)
                        or not isinstance(frame.get("detections"), list)
                        or len(frame["detections"]) > settings.detection_import_max_detections_per_frame):
                    invalid("Invalid or non-contiguous tracks frame")
                detections, ids = [], set()
                for det in frame["detections"]:
                    if not isinstance(det, dict):
                        invalid("Invalid detection")
                    box, points = det.get("box"), det.get("keypoints")
                    if (not isinstance(box, list) or len(box) != 4 or any(not _number(v) for v in box)
                            or not 0 <= box[0] < box[2] <= media["width"] or not 0 <= box[1] < box[3] <= media["height"]
                            or not isinstance(points, list) or len(points) != len(names)):
                        invalid("Invalid detection geometry")
                    normalized_points = []
                    for point in points:
                        if isinstance(point, list) and len(point) == 3:
                            point = dict(zip(("x_px", "y_px", "confidence"), point))
                        if (not isinstance(point, dict) or any(not _number(point.get(k)) for k in ("x_px", "y_px", "confidence"))
                                or not 0 <= point["x_px"] <= media["width"] or not 0 <= point["y_px"] <= media["height"]
                                or not 0 <= point["confidence"] <= 1):
                            invalid("Invalid keypoint geometry")
                        normalized_points.append(point)
                    tid = det.get("track_id")
                    from .track_ids import is_valid_track_id
                    if not is_valid_track_id(tid) or tid in ids:
                        invalid("Invalid or repeated track ID")
                    ids.add(tid)
                    if (not _number(det.get("detection_confidence")) or not 0 <= det["detection_confidence"] <= 1
                            or not _integer(det.get("class_id")) or det["class_id"] < 0):
                        invalid("Invalid detection confidence/class")
                    detections.append(dict(track_id=tid, box_xyxy_px=box, keypoints=normalized_points,
                                           detection_confidence=det["detection_confidence"], class_id=det["class_id"]))
                stream.write(json.dumps(dict(schema_version="1.0", video_id="clip", frame_index=index,
                                              timestamp_sec=frame["time"], detection_count=len(detections), detections=detections), allow_nan=False) + "\n")
        (root / (tracks_name + ".part")).replace(root / tracks_name)
        with (root / (meta_name + ".part")).open("x", encoding="utf-8") as stream:
            json.dump(normalized_meta, stream, allow_nan=False)
        (root / (meta_name + ".part")).replace(root / meta_name)
        validated = _run_validation_pipeline(tracks_name, meta_name, root, settings)
    except (OSError, ValueError):
        invalid("Could not prepare normalized input")
    sha = {role: _streaming_sha256((settings.videos_dir if role == "video" else root) / getattr(batch, role + "_path"))
           for role in ("video", "tracks", "metadata", "annotation")}
    evidence = dict(annotation=annotation, metadata=metadata, sha256=sha,
                    event_frame_range=boundary, media=expected, source=mapping, distance_scale=scale)
    return evidence, validated, tracks_name, meta_name


def selection(db, video):
    ann = db.query(Annotation).filter_by(video_id=video.id).one_or_none()
    return (dict(annotation_id=ann.id, category_id=ann.category_id, mouse_ids=list(ann.mouse_ids or []),
                 participant_roles=dict(ann.participant_roles or {})) if ann else None)


def judgment_out(db, video):
    if video.source != "heuristic":
        conflict("not_heuristic_video")
    if video.workflow_status not in {"draft", "rejected", "submitted", "approved", "unusable"}:
        conflict("invalid_judgment_state")
    evidence = video.heuristic_input
    suggestion = evidence["annotation"]
    public = {k: suggestion[k] for k in ("behavior", "canonical_behavior", "confidence")
              if isinstance(suggestion.get(k), str)}
    for key in ("actor_id", "target_id"):
        if _integer(suggestion.get(key)):
            public[key] = suggestion[key]
    if isinstance(suggestion.get("mouse_ids"), list) and all(_integer(i) for i in suggestion["mouse_ids"]):
        public["mouse_ids"] = suggestion["mouse_ids"]
    if isinstance(suggestion.get("participants"), list):
        public["participants"] = [
            {key: item[key] for key in ("role_key", "role_name", "track_ids") if key in item}
            for item in suggestion["participants"] if isinstance(item, dict)
        ]
    public["frame_range"] = evidence["event_frame_range"]
    s, e = public["frame_range"]["start"], public["frame_range"]["end"]
    public["time_range"] = dict(start=s / video.fps, end=(e + 1) / video.fps)
    attempt = db.query(Submission).filter_by(video_id=video.id).order_by(Submission.attempt_no.desc()).first()
    return dict(video_id=video.id, source="heuristic", workflow_status=video.workflow_status,
                judgment_status=video.judgment_status, annotation_revision=video.annotation_revision,
                detection_import_revision=video.detection_import_revision, identity_revision=video.identity_revision,
                input=public, selection=selection(db, video), submission_id=attempt.id if attempt else None,
                decision_revision=attempt.decision_revision if attempt else 0)


def check_context(db, video, imp, body, membership, *, selection_permission=True):
    if video.source != "heuristic":
        conflict("not_heuristic_video")
    if video.workflow_status not in {"draft", "rejected", "submitted"}:
        conflict("heuristic_completed_locked")
    if (body.expected_annotation_revision is None or body.base_detection_import_revision is None or body.base_identity_revision is None):
        raise HTTPException(422, detail="Heuristic writes require all content revisions")
    if (body.expected_annotation_revision != video.annotation_revision
            or body.base_detection_import_revision != video.detection_import_revision
            or body.base_identity_revision != video.identity_revision or imp is None or imp.edit_version != video.identity_revision):
        conflict("stale_judgment_revision")
    latest = db.query(Submission).filter_by(video_id=video.id).order_by(Submission.attempt_no.desc()).first()
    if video.workflow_status == "submitted":
        require_reviewer(membership)
        if (latest is None or latest.status != "submitted" or body.expected_submission_id != latest.id
                or body.expected_decision_revision != latest.decision_revision):
            conflict("stale_submission")
    else:
        require_editor(membership)
        if (body.expected_submission_id != (latest.id if latest else None)
                or body.expected_decision_revision != (latest.decision_revision if latest else None)):
            conflict("stale_submission")
        ann = db.query(Annotation).filter_by(video_id=video.id).one_or_none()
        if selection_permission and ann is not None and ann.annotator_id != membership.user_id and membership.role not in {"owner", "admin"}:
            raise HTTPException(403, detail="Only the annotator or project owner/admin can modify judgment")
    return latest if video.workflow_status == "submitted" else None


def apply_selection(db, video, imp, payload, membership, settings):
    from .models import Project
    if db.get(Project, video.project_id).category_scheme_locked_at is None:
        conflict("category_scheme_not_locked")
    category = db.get(BehaviorCategory, payload.category_id)
    if category is None or category.project_id != video.project_id or not category.is_active:
        raise HTTPException(400, detail="Category must be active in this project")
    from .participant_roles import ParticipantRoleError, canonicalize_participant_roles
    roles = payload.participant_roles or {}
    if category.participant_mode == "role_based":
        if "mouse_ids" in payload.model_fields_set:
            raise HTTPException(422, detail="role_based must not submit mouse_ids")
        try:
            roles, ids, status = canonicalize_participant_roles(category.role_definitions or [], roles)
        except ParticipantRoleError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        if status != "valid":
            raise HTTPException(400, detail="Complete participant roles are required")
    else:
        if roles:
            raise HTTPException(422, detail="unordered must have empty participant_roles")
        ids = sorted(set(payload.mouse_ids or []))
    ann = db.query(Annotation).filter_by(video_id=video.id).one_or_none()
    before = selection(db, video)
    boundary = video.heuristic_input["event_frame_range"]
    if ann is None:
        ann = Annotation(video_id=video.id, annotator_id=membership.user_id,
                         start_frame=boundary["start"], end_frame=boundary["end"],
                         start_time=boundary["start"] / imp.fps, end_time=(boundary["end"] + 1) / imp.fps,
                         confidence="certain", crop_region=None)
        db.add(ann)
    ann.category_id, ann.mouse_ids, ann.participant_roles = category.id, ids, roles
    ann.detection_import_revision, ann.identity_revision = imp.revision, imp.edit_version
    ann.mouse_id_status, ann.participant_status = "valid", "valid"
    db.flush()
    validate_annotations(db, video, imp, settings)
    if before != selection(db, video):
        ann.material_revision = (ann.material_revision or 1) + (1 if before else 0)
        ann.material_state = material_values(ann)
        ann.material_digest = material_digest(ann)
    ensure_material_evidence(ann)
    return before != selection(db, video)


def replace_attempt(db, settings, video, imp, membership, old, source_identity):
    if old is None:
        return
    old.status = "withdrawn"
    db.flush()
    create_submission(db, settings, video, imp, membership.user_id, source_identity=source_identity)


def save_judgment(db, settings, video, imp, body, membership):
    old = check_context(db, video, imp, body, membership)
    if body.judgment_status == "unusable":
        if old is not None:
            conflict("review_required_for_unusable")
        video.judgment_status = video.workflow_status = "unusable"
        video.annotation_revision += 1
    else:
        source_identity = resolve_and_hash_source(settings, video) if old else None
        if source_identity and source_identity[1] != video.heuristic_input["sha256"]["video"]:
            conflict("heuristic_source_changed")
        changed = apply_selection(db, video, imp, body, membership, settings)
        if changed or video.judgment_status != "usable":
            video.judgment_status = "usable"
            video.annotation_revision += 1
            replace_attempt(db, settings, video, imp, membership, old, source_identity)
    db.commit()
    return judgment_out(db, video)
