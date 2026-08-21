"""Shared role-reference conflict projection for draft track corrections."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import Annotation, BehaviorCategory
from .participant_roles import ParticipantRoleError, canonicalize_participant_roles


def role_track_conflicts(
    db: Session, video_id: int, track_ids: set[int], *, split_frame: int | None = None
) -> list[dict]:
    conflicts: list[dict] = []
    rows = (db.query(Annotation, BehaviorCategory)
            .join(BehaviorCategory, BehaviorCategory.id == Annotation.category_id)
            .filter(Annotation.video_id == video_id,
                    BehaviorCategory.participant_mode == "role_based").all())
    for annotation, category in rows:
        if split_frame is not None and annotation.end_frame < split_frame:
            continue
        try:
            roles, _ids, _status = canonicalize_participant_roles(
                category.role_definitions or [], annotation.participant_roles or {}
            )
        except ParticipantRoleError:
            # Persisted malformed role data is unsafe to rewrite.  Still project every
            # unambiguous integer reference instead of letting malformed siblings hide it.
            raw_roles = annotation.participant_roles
            roles = raw_roles if isinstance(raw_roles, dict) else {}
        names = {item.get("key"): item.get("name") for item in category.role_definitions or []
                 if isinstance(item, dict)}
        for role_key, assigned in roles.items():
            if not isinstance(assigned, list):
                continue
            known_ids = {
                value for value in assigned
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            }
            for track_id in sorted(track_ids.intersection(known_ids)):
                conflicts.append({
                    "annotation_id": annotation.id,
                    "start_time": annotation.start_time, "end_time": annotation.end_time,
                    "start_frame": annotation.start_frame, "end_frame": annotation.end_frame,
                    "role_key": str(role_key), "role_name": names.get(role_key), "track_id": track_id,
                })
    return sorted(conflicts, key=lambda item: (item["annotation_id"], item["role_key"], item["track_id"]))


def conflict_payload(message: str, conflicts: list[dict]) -> dict:
    return {"message": message, "conflicts": conflicts}


def reject_role_conflicts(message: str, conflicts: list[dict]) -> None:
    if conflicts:
        raise HTTPException(status_code=409, detail=conflict_payload(message, conflicts))
