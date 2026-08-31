"""Fail-closed recognition of background jobs which reference one video.

This module deliberately only identifies jobs.  It does not cancel, update, or
delete them, so callers can use the result both before and inside a deletion
transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from sqlalchemy.orm import Session

from .models import BackgroundJob
from .display_proxy_processor import DISPLAY_PROXY_DELETE_PROFILE_VERSIONS


ACTIVE_STATUSES = frozenset({"queued", "running"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class RelatedJobRef:
    """Stable, payload-free reference returned to deletion code."""

    id: int
    job_type: str
    status: str
    project_id: int | None
    result_path: str | None


@dataclass(frozen=True, slots=True)
class RelatedVideoJobs:
    """Related jobs split by deletion-gate semantics, in stable id order."""

    active: tuple[RelatedJobRef, ...]
    terminal: tuple[RelatedJobRef, ...]
    unknown: tuple[RelatedJobRef, ...]


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _id_list(value: object, *, sorted_values: bool = False) -> bool:
    if not isinstance(value, list) or any(not _integer(item) for item in value):
        return False
    if len(value) != len(set(value)):
        return False
    return not sorted_values or value == sorted(value)


def _hex_digest(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def _category_map(value: object, category_ids: list[int], *, tokens: bool) -> bool:
    if not isinstance(value, Mapping) or set(value) != {str(item) for item in category_ids}:
        return False
    values = list(value.values())
    if tokens:
        return all(isinstance(item, str) and len(item) == 32
                   and all(character in "0123456789abcdef" for character in item)
                   for item in values)
    return (all(isinstance(item, str) and bool(item) and item not in {".", ".."}
                and "/" not in item and "\\" not in item for item in values)
            and len({item.casefold() for item in values}) == len(values))


def _export_ref(ref: object) -> bool:
    if not isinstance(ref, Mapping):
        return False
    return (_integer(ref.get("submission_id"))
            and _integer(ref.get("submission_annotation_id"))
            and _integer(ref.get("snapshot_id"))
            and _nonnegative_integer(ref.get("source_media_revision"))
            and _hex_digest(ref.get("source_sha256"))
            and _nonnegative_integer(ref.get("source_file_size"))
            and _nonnegative_integer(ref.get("source_mtime_ns"))
            and _nonnegative_integer(ref.get("source_device"))
            and _nonnegative_integer(ref.get("source_inode"))
            and _hex_digest(ref.get("raw_digest"))
            and _hex_digest(ref.get("state_digest"))
            and _hex_digest(ref.get("metadata_digest"))
            and isinstance(ref.get("opaque_token"), str)
            and len(ref["opaque_token"]) >= 24)


def _matching_ids(value: object, targets: frozenset[int]) -> bool:
    if _integer(value):
        return value in targets
    if isinstance(value, str) and value.isdigit():
        return int(value) in targets
    if isinstance(value, (list, tuple, set)):
        return any(_integer(item) and item in targets for item in value)
    return False


def _ref_ids(refs: object, key: str) -> list[object]:
    if not isinstance(refs, list):
        return []
    return [ref.get(key) for ref in refs if isinstance(ref, Mapping)]


def _possibly_related(payload: object, video_id: int,
                      submission_ids: frozenset[int],
                      submission_annotation_ids: frozenset[int]) -> bool:
    """Find an explicit target marker even when the surrounding payload is bad.

    Project id is intentionally absent: a project-level match cannot establish
    that an export contains this video.
    """
    if not isinstance(payload, Mapping):
        return False
    payload_video_id = payload.get("video_id")
    if payload_video_id == video_id or (
        isinstance(payload_video_id, str) and payload_video_id.isdigit()
        and int(payload_video_id) == video_id
    ):
        return True
    revisions = payload.get("video_revisions")
    if isinstance(revisions, Mapping) and (
        video_id in revisions or str(video_id) in revisions
    ):
        return True
    if _matching_ids(payload.get("submission_id"), submission_ids):
        return True
    if _matching_ids(payload.get("submission_ids"), submission_ids):
        return True
    if _matching_ids(payload.get("submission_annotation_ids"), submission_annotation_ids):
        return True
    refs = payload.get("refs")
    return (_matching_ids(_ref_ids(refs, "submission_id"), submission_ids)
            or _matching_ids(_ref_ids(refs, "submission_annotation_id"),
                             submission_annotation_ids))


def _classify_media(payload: Mapping[str, object], project_id: int, video_id: int,
                    submission_ids: frozenset[int],
                    submission_annotation_ids: frozenset[int]) -> tuple[bool, bool]:
    """Return ``(related, well_formed)`` for either media payload generation."""
    has_legacy = "video_id" in payload
    has_submission = "submission_id" in payload or "submission_annotation_ids" in payload
    if has_legacy and has_submission:
        return _possibly_related(payload, video_id, submission_ids,
                                 submission_annotation_ids), False
    if has_legacy:
        related = payload.get("video_id") == video_id
        valid = (_integer(payload.get("video_id"))
                 and payload.get("project_id") == project_id
                 and _integer(payload.get("revision")))
        return related, valid
    if has_submission:
        related = (_matching_ids(payload.get("submission_id"), submission_ids)
                   or _matching_ids(payload.get("submission_annotation_ids"),
                                    submission_annotation_ids))
        valid = (_integer(payload.get("submission_id"))
                 and _id_list(payload.get("submission_annotation_ids"), sorted_values=True)
                 and bool(payload.get("submission_annotation_ids")))
        return related, valid
    return False, False


def _classify_export(payload: Mapping[str, object], project_id: int, video_id: int,
                     submission_ids: frozenset[int],
                     submission_annotation_ids: frozenset[int],
                     annotation_ids: frozenset[int]) -> tuple[bool, bool]:
    modern = any(key in payload for key in ("contract_version", "submission_ids",
                                             "submission_annotation_ids", "refs"))
    if modern:
        related = _possibly_related(payload, video_id, submission_ids,
                                    submission_annotation_ids)
        submissions = payload.get("submission_ids")
        annotations = payload.get("submission_annotation_ids")
        refs = payload.get("refs")
        categories = payload.get("category_ids")
        valid = (payload.get("contract_version") == 1
                 and payload.get("project_id") == project_id
                 and _id_list(categories, sorted_values=True)
                 and bool(categories)
                 and _category_map(payload.get("category_directories"), categories, tokens=False)
                 and _category_map(payload.get("category_tokens"), categories, tokens=True)
                 and _id_list(submissions, sorted_values=True)
                 and bool(submissions)
                 and _id_list(annotations)
                 and bool(annotations)
                 and isinstance(refs, list)
                 and len(refs) == len(annotations)  # type: ignore[arg-type]
                 and all(_export_ref(ref) for ref in refs))
        if valid:
            ref_submissions = {ref["submission_id"] for ref in refs}
            ref_annotations = [ref["submission_annotation_id"] for ref in refs]
            valid = (ref_submissions == set(submissions)  # type: ignore[arg-type]
                     and ref_annotations == annotations)
        return related, valid

    # Compatibility with pre-Submission exports.
    related = _possibly_related(payload, video_id, submission_ids,
                                submission_annotation_ids)
    annotation_values = payload.get("annotation_ids")
    revisions = payload.get("video_revisions")
    valid = (_id_list(annotation_values)
             and isinstance(revisions, Mapping)
             and bool(revisions)
             and all((isinstance(key, str) and key.isdigit() and int(key) > 0)
                     or _integer(key) for key in revisions)
             and all(_integer(value) for value in revisions.values()))
    if _matching_ids(annotation_values, annotation_ids):
        related = True
    return related, valid


def _classify_display_proxy(payload: Mapping[str, object], project_id: int,
                            video_id: int) -> tuple[bool, bool]:
    related = payload.get("video_id") == video_id
    valid = (set(payload) == {"video_id", "project_id", "source_sha256", "profile_version"}
             and _integer(payload.get("video_id"))
             and payload.get("project_id") == project_id
             and _hex_digest(payload.get("source_sha256"))
             and payload.get("profile_version") in DISPLAY_PROXY_DELETE_PROFILE_VERSIONS)
    return related, valid


def identify_related_video_jobs(
    db: Session,
    *,
    project_id: int,
    video_id: int,
    annotation_ids: Iterable[int] = (),
    submission_ids: Iterable[int] = (),
    submission_annotation_ids: Iterable[int] = (),
    refs: Iterable[Mapping[str, object]] = (),
    submission_annotation_refs: Iterable[Mapping[str, object]] = (),
) -> RelatedVideoJobs:
    """Identify jobs referencing a frozen view of one video's authority rows.

    A malformed media/export payload in the target project is returned in
    ``unknown`` as a deletion blocker, even when no target marker survives.
    Valid payloads still need an explicit target reference, and unrelated job
    types are never selected merely by project. Unknown statuses on related
    jobs follow the same fail-closed rule.
    """
    if not _integer(project_id) or not _integer(video_id):
        raise ValueError("project_id and video_id must be positive integers")
    live_annotations = set(annotation_ids)
    submissions = set(submission_ids)
    submission_annotations = set(submission_annotation_ids)
    for ref in (*tuple(refs), *tuple(submission_annotation_refs)):
        submission_id = ref.get("submission_id")
        annotation_id = ref.get("submission_annotation_id")
        if _integer(submission_id):
            submissions.add(submission_id)
        if _integer(annotation_id):
            submission_annotations.add(annotation_id)
    if any(not _integer(item) for item in
           live_annotations | submissions | submission_annotations):
        raise ValueError("annotation and frozen ids must be positive integers")
    live_annotation_ids = frozenset(live_annotations)
    frozen_submissions = frozenset(submissions)
    frozen_submission_annotations = frozenset(submission_annotations)

    groups: dict[str, list[RelatedJobRef]] = {"active": [], "terminal": [], "unknown": []}
    for job in db.query(BackgroundJob).order_by(BackgroundJob.id).all():
        payload = job.payload
        possible = _possibly_related(payload, video_id, frozen_submissions,
                                     frozen_submission_annotations)
        dedupe = job.dedupe_key or ""
        possible = (possible or dedupe.startswith(f"media:video:{video_id}:")
                    or dedupe.startswith(f"display-proxy:video:{video_id}:")
                    or any(dedupe == f"media:submission:{item}"
                           for item in frozen_submissions))
        same_project_job = job.project_id == project_id
        guarded_type = job.job_type in {"media", "export", "display_proxy"}
        if job.job_type == "cleanup":
            continue
        if not isinstance(payload, Mapping):
            if possible or (same_project_job and guarded_type):
                groups["unknown"].append(RelatedJobRef(
                    job.id, job.job_type, job.status, job.project_id, job.result_path))
            continue
        if job.job_type == "media":
            related, valid = _classify_media(payload, project_id, video_id, frozen_submissions,
                                             frozen_submission_annotations)
        elif job.job_type == "export":
            related, valid = _classify_export(payload, project_id, video_id, frozen_submissions,
                                              frozen_submission_annotations,
                                              live_annotation_ids)
        elif job.job_type == "display_proxy":
            related, valid = _classify_display_proxy(payload, project_id, video_id)
        else:
            related, valid = possible, False
        if same_project_job and guarded_type and not valid:
            ref = RelatedJobRef(job.id, job.job_type, job.status,
                                job.project_id, job.result_path)
            groups["unknown"].append(ref)
            continue
        if guarded_type and not same_project_job:
            if possible or related:
                groups["unknown"].append(RelatedJobRef(
                    job.id, job.job_type, job.status, job.project_id, job.result_path))
            continue
        if not related and not (possible and not valid):
            continue

        ref = RelatedJobRef(job.id, job.job_type, job.status, job.project_id, job.result_path)
        if not valid or job.status not in ACTIVE_STATUSES | TERMINAL_STATUSES:
            groups["unknown"].append(ref)
        elif job.status in ACTIVE_STATUSES:
            groups["active"].append(ref)
        else:
            groups["terminal"].append(ref)
    return RelatedVideoJobs(*(tuple(groups[name]) for name in ("active", "terminal", "unknown")))


# Concise alias for callers which prefer discovery terminology.
find_related_video_jobs = identify_related_video_jobs
