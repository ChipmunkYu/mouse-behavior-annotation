"""Database half of the crash-safe hard-video-delete protocol.

``freeze_video_delete`` is read-only and is intended to run in a short caller-owned
transaction.  After the returned files have been quarantined, callers pass a
session factory (or engine) to ``delete_frozen_video``.  That wrapper owns the
independent SQLite ``BEGIN IMMEDIATE`` transaction completely.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Callable, Iterable, Mapping, Sequence

from sqlalchemy import Engine, or_, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .authority_triggers import TRIGGERS
from .config import Settings
from .models import (
    Annotation, BackgroundJob, Clip, CorrectedDetectionAssignment, CorrectedTrack,
    DetectionImport, DetectionSnapshot, DetectionSnapshotState, DetectionStateOverride,
    DetectionSuppression, DraftDetectionChange, DraftIdentityEdit, IdentityEdit,
    ProjectMembership, RawDetection, Review, Submission, SubmissionAnnotation,
    SuppressionDetection, Video, VideoImportBatch,
)
from .related_video_jobs import identify_related_video_jobs
from .video_delete_io import DeletePath


class VideoDeleteDBError(RuntimeError):
    """Safe, typed database-delete failure (never contains SQL or filesystem paths)."""

    kind = "integrity"

    def __init__(self, message: str):
        self.safe_message = message
        super().__init__(message)


class VideoDeleteForbiddenError(VideoDeleteDBError):
    kind = "forbidden"


class VideoDeleteNotFoundError(VideoDeleteDBError):
    kind = "not-found"


class VideoDeleteConflictError(VideoDeleteDBError):
    kind = "conflict"


class VideoDeleteIntegrityError(VideoDeleteDBError):
    kind = "integrity"


@dataclass(frozen=True, slots=True)
class FrozenVideoDelete:
    """Complete equality token shared by the database and filesystem phases."""

    project_id: int
    video_id: int
    actor_user_id: int
    frozen_ids_by_table: tuple[tuple[str, tuple[int, ...]], ...]
    frozen_keys_by_table: tuple[tuple[str, tuple[tuple[int, ...], ...]], ...]
    terminal_job_ids: tuple[int, ...]
    paths: tuple[DeletePath, ...]

    def ids(self, table: str) -> tuple[int, ...]:
        return dict(self.frozen_ids_by_table).get(table, ())


_ID_MODELS = (
    Annotation, Review, Clip, Submission, SubmissionAnnotation, DetectionImport,
    RawDetection, CorrectedTrack, CorrectedDetectionAssignment, IdentityEdit,
    DetectionSuppression, DraftIdentityEdit, DetectionSnapshot, VideoImportBatch,
)
_TABLES_WITH_ID = {model.__tablename__ for model in _ID_MODELS}
_DELETE_ORDER = (
    "clips", "reviews", "submission_annotations", "submissions",
    "detection_snapshot_states", "detection_snapshots", "draft_detection_changes",
    "detection_state_overrides", "suppression_detections",
    "corrected_detection_assignments", "identity_edits", "detection_suppressions",
    "draft_identity_edits", "corrected_tracks", "raw_detections",
    "detection_imports", "annotations", "video_import_batches", "background_jobs",
    "videos",
)
_DROPPED_TRIGGERS = ("trg_annotation_delete", "trg_live_annotation_delete")


def _positive(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _ids(rows: Iterable[object]) -> tuple[int, ...]:
    return tuple(sorted(row.id for row in rows))  # type: ignore[attr-defined]


def _safe_key(stored: str, root: Path) -> str:
    """Turn a legacy absolute-in-root value or a relative value into an IO key."""
    if not isinstance(stored, str) or not stored or "\x00" in stored:
        raise VideoDeleteIntegrityError("A frozen file reference is invalid")
    raw = Path(stored)
    root_abs = Path(os.path.abspath(root))
    candidate = Path(os.path.abspath(raw if raw.is_absolute() else root_abs / raw))
    try:
        relative = candidate.relative_to(root_abs).as_posix()
    except ValueError as exc:
        raise VideoDeleteIntegrityError("A frozen file reference is outside its controlled root") from exc
    if not relative or any(part in {"", ".", ".."} for part in PurePath(relative).parts):
        raise VideoDeleteIntegrityError("A frozen file reference is invalid")
    return relative


def _rows(db: Session, model, criterion) -> list:
    return db.query(model).filter(criterion).order_by(*model.__table__.primary_key.columns).all()


def _authorize(db: Session, project_id: int, video_id: int, actor_user_id: int) -> Video:
    db.expire_all()
    video = (db.query(Video).execution_options(populate_existing=True)
             .filter(Video.id == video_id, Video.project_id == project_id).one_or_none())
    if video is None:
        raise VideoDeleteNotFoundError("Video not found in this project")
    membership = (db.query(ProjectMembership).execution_options(populate_existing=True)
                  .filter(ProjectMembership.project_id == project_id,
                          ProjectMembership.user_id == actor_user_id).one_or_none())
    if membership is None or membership.status != "active" or membership.role not in {"owner", "admin"}:
        raise VideoDeleteForbiddenError("An active project owner or admin is required")
    if video.workflow_status not in {"draft", "rejected"}:
        raise VideoDeleteConflictError("Only draft or rejected videos may be deleted")
    return video


def _collect(db: Session, *, project_id: int, video_id: int, actor_user_id: int,
             settings: Settings) -> FrozenVideoDelete:
    video = _authorize(db, project_id, video_id, actor_user_id)

    annotations = _rows(db, Annotation, Annotation.video_id == video_id)
    annotation_ids = _ids(annotations)
    submissions = _rows(db, Submission, Submission.video_id == video_id)
    submission_ids = _ids(submissions)
    submission_annotations = _rows(
        db, SubmissionAnnotation, SubmissionAnnotation.submission_id.in_(submission_ids)
    ) if submission_ids else []
    submission_annotation_ids = _ids(submission_annotations)
    reviews = _rows(db, Review, Review.video_id == video_id)
    clips = _rows(db, Clip, or_(
        Clip.annotation_id.in_(annotation_ids) if annotation_ids else text("0"),
        Clip.submission_annotation_id.in_(submission_annotation_ids)
        if submission_annotation_ids else text("0"),
    ))
    imports = _rows(db, DetectionImport, DetectionImport.video_id == video_id)
    import_ids = _ids(imports)
    raw = _rows(db, RawDetection, RawDetection.detection_import_id.in_(import_ids)) if import_ids else []
    raw_ids = _ids(raw)
    tracks = _rows(db, CorrectedTrack, CorrectedTrack.detection_import_id.in_(import_ids)) if import_ids else []
    track_ids = _ids(tracks)
    assignments = _rows(db, CorrectedDetectionAssignment, or_(
        CorrectedDetectionAssignment.raw_detection_id.in_(raw_ids) if raw_ids else text("0"),
        CorrectedDetectionAssignment.corrected_track_id.in_(track_ids) if track_ids else text("0"),
    ))
    identity_edits = _rows(db, IdentityEdit, IdentityEdit.video_id == video_id)
    suppressions = _rows(db, DetectionSuppression, DetectionSuppression.video_id == video_id)
    suppression_ids = _ids(suppressions)
    suppression_detections = _rows(db, SuppressionDetection, or_(
        SuppressionDetection.suppression_id.in_(suppression_ids)
        if suppression_ids else text("0"),
        SuppressionDetection.raw_detection_id.in_(raw_ids) if raw_ids else text("0"),
    ))
    overrides = _rows(db, DetectionStateOverride,
                      DetectionStateOverride.detection_import_id.in_(import_ids)) if import_ids else []
    drafts = _rows(db, DraftIdentityEdit,
                   DraftIdentityEdit.detection_import_id.in_(import_ids)) if import_ids else []
    draft_ids = _ids(drafts)
    draft_changes = _rows(db, DraftDetectionChange,
                          DraftDetectionChange.edit_id.in_(draft_ids)) if draft_ids else []
    snapshots = _rows(db, DetectionSnapshot,
                      DetectionSnapshot.detection_import_id.in_(import_ids)) if import_ids else []
    snapshot_ids = _ids(snapshots)
    snapshot_states = _rows(db, DetectionSnapshotState,
                            DetectionSnapshotState.snapshot_id.in_(snapshot_ids)) if snapshot_ids else []
    batches = _rows(db, VideoImportBatch, VideoImportBatch.created_video_id == video_id)

    # Every graph edge must remain inside this video/project.
    if any(row.project_id != project_id for row in reviews):
        raise VideoDeleteConflictError("A review crosses the video project boundary")
    if any(row.project_id not in {None, project_id} for row in clips):
        raise VideoDeleteConflictError("A clip crosses the video project boundary")
    if any(row.project_id != project_id for row in batches):
        raise VideoDeleteConflictError("An import batch crosses the video project boundary")
    if any(row.detection_import_id not in import_ids for row in identity_edits + suppressions):
        raise VideoDeleteConflictError("A detection row crosses the video import boundary")
    if any(row.submission_id is not None and row.submission_id not in submission_ids
           for row in reviews):
        raise VideoDeleteConflictError("A review crosses the video submission boundary")
    if any((row.annotation_id is not None and row.annotation_id not in annotation_ids)
           or (row.submission_annotation_id is not None
               and row.submission_annotation_id not in submission_annotation_ids)
           for row in clips):
        raise VideoDeleteConflictError("A clip crosses the video annotation boundary")
    if any(row.source_annotation_id is not None and row.source_annotation_id not in annotation_ids
           for row in submission_annotations):
        raise VideoDeleteConflictError("A frozen annotation crosses the video boundary")
    if any(row.merged_into_id is not None and row.merged_into_id not in track_ids for row in tracks):
        raise VideoDeleteConflictError("A corrected track crosses the video import boundary")
    if any(row.raw_detection_id not in raw_ids or row.corrected_track_id not in track_ids
           for row in assignments):
        raise VideoDeleteConflictError("A corrected assignment crosses the video import boundary")
    if any(row.suppression_id not in suppression_ids or row.raw_detection_id not in raw_ids
           for row in suppression_detections):
        raise VideoDeleteConflictError("A suppression detection crosses the video import boundary")
    external_audit = (db.query(IdentityEdit.id).filter(
        IdentityEdit.detection_import_id.in_(import_ids), IdentityEdit.video_id != video_id
    ).first() if import_ids else None)
    external_suppression = (db.query(DetectionSuppression.id).filter(
        DetectionSuppression.detection_import_id.in_(import_ids),
        DetectionSuppression.video_id != video_id
    ).first() if import_ids else None)
    external_review = (db.query(Review.id).filter(
        Review.submission_id.in_(submission_ids), Review.video_id != video_id
    ).first() if submission_ids else None)
    if external_audit or external_suppression or external_review:
        raise VideoDeleteConflictError("A frozen row is linked from another video")
    incoming_identity_edit = (db.query(IdentityEdit.id).filter(
        IdentityEdit.reverted_edit_id.in_(_ids(identity_edits)),
        ~IdentityEdit.id.in_(_ids(identity_edits)),
    ).first() if identity_edits else None)
    incoming_suppression = (db.query(DetectionSuppression.id).filter(
        DetectionSuppression.reverted_suppression_id.in_(suppression_ids),
        ~DetectionSuppression.id.in_(suppression_ids),
    ).first() if suppression_ids else None)
    incoming_track = (db.query(CorrectedTrack.id).filter(
        CorrectedTrack.merged_into_id.in_(track_ids), ~CorrectedTrack.id.in_(track_ids),
    ).first() if track_ids else None)
    incoming_annotation = (db.query(SubmissionAnnotation.id)
        .join(Submission, Submission.id == SubmissionAnnotation.submission_id)
        .filter(SubmissionAnnotation.source_annotation_id.in_(annotation_ids),
                Submission.video_id != video_id).first() if annotation_ids else None)
    if any((incoming_identity_edit, incoming_suppression, incoming_track,
            incoming_annotation)):
        raise VideoDeleteConflictError("A frozen row is linked from another video")
    if submissions and any(row.detection_snapshot_id not in snapshot_ids for row in submissions):
        raise VideoDeleteConflictError("A submission references a snapshot outside this video")
    external_snapshot_ref = (db.query(Submission.id).filter(
        Submission.detection_snapshot_id.in_(snapshot_ids), Submission.video_id != video_id
    ).first() if snapshot_ids else None)
    if external_snapshot_ref is not None:
        raise VideoDeleteConflictError("A frozen snapshot is referenced by another video")

    jobs = identify_related_video_jobs(
        db, project_id=project_id, video_id=video_id, annotation_ids=annotation_ids,
        submission_ids=submission_ids, submission_annotation_ids=submission_annotation_ids,
    )
    if jobs.active:
        raise VideoDeleteConflictError(
            "A related background job is active; contact an administrator before retrying"
        )
    if jobs.unknown:
        raise VideoDeleteConflictError(
            "A related background job cannot be classified safely; contact an administrator before retrying"
        )

    rows_by_table: dict[str, Sequence[object]] = {
        "annotations": annotations, "reviews": reviews, "clips": clips,
        "submissions": submissions, "submission_annotations": submission_annotations,
        "detection_imports": imports, "raw_detections": raw,
        "corrected_tracks": tracks, "corrected_detection_assignments": assignments,
        "identity_edits": identity_edits, "detection_suppressions": suppressions,
        "draft_identity_edits": drafts, "detection_snapshots": snapshots,
        "video_import_batches": batches,
    }
    ids_by_table = {name: _ids(rows) for name, rows in rows_by_table.items()}
    ids_by_table["background_jobs"] = tuple(job.id for job in jobs.terminal)
    ids_by_table["videos"] = (video_id,)
    keys_by_table = {
        "detection_state_overrides": tuple((row.raw_detection_id,) for row in overrides),
        "draft_detection_changes": tuple((row.edit_id, row.raw_detection_id) for row in draft_changes),
        "suppression_detections": tuple((row.suppression_id, row.raw_detection_id)
                                         for row in suppression_detections),
        "detection_snapshot_states": tuple((row.snapshot_id, row.raw_detection_id)
                                            for row in snapshot_states),
    }

    path_items: list[DeletePath] = []
    owners: dict[tuple[str, str], set[tuple[str, int]]] = {}
    def add_path(root_kind: str, stored: str | None, table: str, row_id: int) -> None:
        if not stored:
            return
        root = {"videos": settings.videos_dir, "detection_imports": settings.detection_imports_dir,
                "clips": settings.clips_dir, "thumbnails": settings.thumbnails_dir,
                "exports": settings.exports_dir,
                "display_proxies": settings.display_proxies_dir}[root_kind]
        key = _safe_key(stored, root)
        path_items.append(DeletePath(root_kind, key))
        owners.setdefault((root_kind, os.path.normcase(key)), set()).add((table, row_id))

    add_path("videos", video.storage_path, "videos", video_id)
    add_path("display_proxies", video.display_path, "videos", video_id)
    for row in submissions:
        add_path("videos", row.source_storage_key, "submissions", row.id)
    for row in imports:
        add_path("detection_imports", row.tracks_path, "detection_imports", row.id)
        add_path("detection_imports", row.metadata_path, "detection_imports", row.id)
    for row in batches:
        add_path("videos", row.video_path, "video_import_batches", row.id)
        add_path("detection_imports", row.tracks_path, "video_import_batches", row.id)
        add_path("detection_imports", row.metadata_path, "video_import_batches", row.id)
    for row in clips:
        add_path("clips", row.clip_path, "clips", row.id)
        add_path("thumbnails", row.thumbnail_path, "clips", row.id)
    for job in jobs.terminal:
        if job.job_type == "export":
            add_path("exports", job.result_path, "background_jobs", job.id)
        elif job.job_type == "display_proxy":
            # Display-proxy results have their own controlled namespace.  In
            # particular, never reinterpret one as a legacy Clip artifact.
            add_path("display_proxies", job.result_path, "background_jobs", job.id)
        elif job.result_path:
            # Current media workers normally leave this NULL.  A legacy value is
            # accepted only when it names one of this job's already-frozen Clip
            # entities, which determines the real controlled root unambiguously.
            clip_keys = {path.relative_key for path in path_items if path.root_kind == "clips"}
            thumb_keys = {path.relative_key for path in path_items if path.root_kind == "thumbnails"}
            candidates: list[str] = []
            for root_kind, root, frozen_keys in (
                ("clips", settings.clips_dir, clip_keys),
                ("thumbnails", settings.thumbnails_dir, thumb_keys),
            ):
                try:
                    key = _safe_key(job.result_path, root)
                except VideoDeleteIntegrityError:
                    continue  # Outside one candidate root may still be inside the other.
                if key in frozen_keys:
                    candidates.append(root_kind)
            if len(candidates) != 1:
                raise VideoDeleteIntegrityError("A media job result has no unambiguous controlled root")
            add_path(candidates[0], job.result_path, "background_jobs", job.id)

    # A terminal worker is immutable, but a crash/cleanup failure can leave files
    # which were never persisted in result_path. Enumerate only namespaces whose
    # job/Clip identifiers prove ownership; near-miss names fail closed.
    worker_artifacts = _terminal_worker_artifacts(
        settings=settings, project_id=project_id, jobs=jobs.terminal, clips=clips,
        submission_annotations=submission_annotations,
    )
    for artifact in worker_artifacts:
        path_items.append(artifact)
        owners.setdefault((artifact.root_kind, os.path.normcase(artifact.relative_key)), set()).add(
            ("background_jobs", jobs.terminal[0].id) if jobs.terminal else ("clips", clips[0].id)
        )

    _reject_shared_paths(db, owners, ids_by_table, settings)
    return FrozenVideoDelete(
        project_id, video_id, actor_user_id,
        tuple(sorted((name, tuple(values)) for name, values in ids_by_table.items())),
        tuple(sorted((name, tuple(sorted(values))) for name, values in keys_by_table.items())),
        tuple(job.id for job in jobs.terminal), tuple(path_items),
    )


def _terminal_worker_artifacts(*, settings: Settings, project_id: int,
                               jobs: Sequence[object], clips: Sequence[Clip],
                               submission_annotations: Sequence[SubmissionAnnotation]) -> list[DeletePath]:
    artifacts: list[DeletePath] = []

    def scan(root_kind: str, root: Path, accepted: Sequence[tuple[re.Pattern[str], str]],
             suspicious: Sequence[re.Pattern[str]]) -> None:
        if not root.exists():
            return
        try:
            names = [entry.name for entry in os.scandir(root)]
        except OSError as exc:
            raise VideoDeleteIntegrityError("A worker artifact root cannot be inspected safely") from exc
        for name in names:
            matches = [(pattern, kind) for pattern, kind in accepted if pattern.fullmatch(name)]
            if len(matches) == 1:
                artifacts.append(DeletePath(root_kind, name, matches[0][1]))
            elif len(matches) > 1:
                raise VideoDeleteIntegrityError("A worker artifact name is ambiguous")
            elif any(pattern.match(name) for pattern in suspicious):
                raise VideoDeleteConflictError("A suspicious worker artifact cannot be attributed safely")

    for job in jobs:
        if job.job_type == "export":
            current = re.escape(f".export-{job.id}")
            legacy = re.escape(f".export_{project_id}_{job.id}")
            scan("exports", settings.exports_dir, (
                (re.compile(current + r"\.staging"), "directory"),
                (re.compile(current + r"\.tmp\.zip"), "file"),
                (re.compile(legacy + r"\.staging"), "directory"),
                (re.compile(legacy + r"\.tmp\.zip"), "file"),
            ), (re.compile(rf"^(?:{current}|{legacy})(?:[._-]|$)"),))
        elif job.job_type == "media":
            current = re.escape(f".submission-media-job-{job.id}-")
            legacy = re.escape(f".submission_media_job_{job.id}_")
            token = r"[0-9a-fA-F]{32}"
            scan("videos", settings.videos_dir, (
                (re.compile(current + token + r"\.staging"), "file"),
                (re.compile(legacy + token + r"\.staging"), "file"),
            ), (re.compile(rf"^(?:{current}|{legacy})"),))

    clip_patterns: dict[str, list[tuple[re.Pattern[str], str]]] = {"clips": [], "thumbnails": []}
    clip_suspicious: dict[str, list[re.Pattern[str]]] = {"clips": [], "thumbnails": []}
    submission_by_annotation = {row.id: row.submission_id for row in submission_annotations}
    for clip in clips:
        if clip.annotation_id is not None:
            stem = re.escape(f".clip_{clip.annotation_id}_rev{clip.source_revision}.")
        elif clip.submission_annotation_id is not None:
            # Submission Clip filenames use the immutable annotation and submission ids.
            submission_id = submission_by_annotation.get(clip.submission_annotation_id)
            if submission_id is None:
                raise VideoDeleteIntegrityError("A frozen Submission Clip has no owner")
            stem = re.escape(f".clip_{clip.submission_annotation_id}_revsub{submission_id}.")
        else:
            raise VideoDeleteIntegrityError("A frozen Clip has no owner")
        for root_kind, extension in (("clips", "mp4"), ("thumbnails", "jpg")):
            clip_patterns[root_kind].append(
                (re.compile(stem + r"[0-9a-fA-F]{32}\." + extension + r"\.part"), "file")
            )
            clip_suspicious[root_kind].append(re.compile(r"^" + stem))
    scan("clips", settings.clips_dir, clip_patterns["clips"], clip_suspicious["clips"])
    scan("thumbnails", settings.thumbnails_dir,
         clip_patterns["thumbnails"], clip_suspicious["thumbnails"])
    return artifacts


def _reject_shared_paths(db: Session, owners: Mapping[tuple[str, str], set[tuple[str, int]]],
                         target_ids: Mapping[str, tuple[int, ...]], settings: Settings) -> None:
    """Fail closed when an ordinary non-target business row names a target entity."""
    checks = {
        "videos": ((Video, "storage_path", "videos"), (Submission, "source_storage_key", "submissions"),
                   (VideoImportBatch, "video_path", "video_import_batches")),
        "detection_imports": ((DetectionImport, "tracks_path", "detection_imports"),
                              (DetectionImport, "metadata_path", "detection_imports"),
                              (VideoImportBatch, "tracks_path", "video_import_batches"),
                              (VideoImportBatch, "metadata_path", "video_import_batches")),
        "clips": ((Clip, "clip_path", "clips"),),
        "thumbnails": ((Clip, "thumbnail_path", "clips"),),
        "exports": ((BackgroundJob, "result_path", "background_jobs"),),
        "display_proxies": ((Video, "display_path", "videos"),
                            (BackgroundJob, "result_path", "background_jobs")),
    }
    roots = {"videos": settings.videos_dir, "detection_imports": settings.detection_imports_dir,
             "clips": settings.clips_dir, "thumbnails": settings.thumbnails_dir,
             "exports": settings.exports_dir,
             "display_proxies": settings.display_proxies_dir}
    wanted = set(owners)
    for root_kind, descriptors in checks.items():
        if not any(root == root_kind for root, _key in wanted):
            continue
        for model, field, table_name in descriptors:
            for row in db.query(model).filter(getattr(model, field).is_not(None)).all():
                if row.id in target_ids.get(table_name, ()):
                    continue
                if (root_kind == "display_proxies" and model is BackgroundJob
                        and row.job_type != "display_proxy"):
                    continue
                try:
                    key = _safe_key(getattr(row, field), roots[root_kind])
                except VideoDeleteIntegrityError:
                    continue  # An unrelated malformed path cannot alias a controlled key.
                if (root_kind, os.path.normcase(key)) in wanted:
                    raise VideoDeleteConflictError("A target file is shared by another business row")


def freeze_video_delete(db: Session, *, project_id: int, video_id: int,
                        actor_user_id: int, settings: Settings) -> FrozenVideoDelete:
    """Fresh-read and freeze the complete target graph without modifying any row."""
    if not all(_positive(value) for value in (project_id, video_id, actor_user_id)):
        raise ValueError("project_id, video_id and actor_user_id must be positive integers")
    return _collect(db, project_id=project_id, video_id=video_id,
                    actor_user_id=actor_user_id, settings=settings)


def _delete_ids(db: Session, table: str, ids: tuple[int, ...]) -> None:
    if not ids:
        return
    bind = ",".join(f":v{i}" for i in range(len(ids)))
    db.execute(text(f"DELETE FROM {table} WHERE id IN ({bind})"),
               {f"v{i}": value for i, value in enumerate(ids)})


def _delete_keys(db: Session, table: str, keys: tuple[tuple[int, ...], ...]) -> None:
    columns = {
        "detection_state_overrides": ("raw_detection_id",),
        "draft_detection_changes": ("edit_id", "raw_detection_id"),
        "suppression_detections": ("suppression_id", "raw_detection_id"),
        "detection_snapshot_states": ("snapshot_id", "raw_detection_id"),
    }[table]
    for key in keys:
        where = " AND ".join(f"{column}=:v{i}" for i, column in enumerate(columns))
        db.execute(text(f"DELETE FROM {table} WHERE {where}"),
                   {f"v{i}": value for i, value in enumerate(key)})


def _delete_frozen_video_core(db: Session, frozen: FrozenVideoDelete, *, settings: Settings,
                              fault_hook: Callable[[str], None] | None = None) -> None:
    """Revalidate and delete a frozen graph in the wrapper-owned transaction."""
    if db.get_bind().dialect.name != "sqlite":
        raise VideoDeleteIntegrityError("Hard video deletion requires SQLite")
    try:
        current = _collect(db, project_id=frozen.project_id, video_id=frozen.video_id,
                           actor_user_id=frozen.actor_user_id, settings=settings)
        if not _same_frozen_graph(current, frozen):
            raise VideoDeleteConflictError("The frozen video graph changed before final deletion")
        ids = dict(frozen.frozen_ids_by_table)
        keys = dict(frozen.frozen_keys_by_table)
        for name in _DROPPED_TRIGGERS:
            db.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
        if fault_hook:
            fault_hook("triggers_dropped")
        for table in _DELETE_ORDER:
            if table in keys:
                _delete_keys(db, table, keys[table])
            else:
                _delete_ids(db, table, ids.get(table, ()))
        for name in _DROPPED_TRIGGERS:
            db.execute(text(f"CREATE TRIGGER {name} {TRIGGERS[name]}"))
        trigger_count = db.execute(text(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' "
            "AND name IN ('trg_annotation_delete','trg_live_annotation_delete')"
        )).scalar_one()
        if trigger_count != len(_DROPPED_TRIGGERS):
            raise VideoDeleteIntegrityError("Authority triggers were not restored")
        for table, table_ids in ids.items():
            if table_ids and table in _TABLES_WITH_ID | {"background_jobs", "videos"}:
                bind = ",".join(f":v{i}" for i in range(len(table_ids)))
                count = db.execute(text(f"SELECT count(*) FROM {table} WHERE id IN ({bind})"),
                                   {f"v{i}": value for i, value in enumerate(table_ids)}).scalar_one()
                if count:
                    raise VideoDeleteIntegrityError("Target rows remain after deletion")
        for table, table_keys in keys.items():
            columns = {
                "detection_state_overrides": ("raw_detection_id",),
                "draft_detection_changes": ("edit_id", "raw_detection_id"),
                "suppression_detections": ("suppression_id", "raw_detection_id"),
                "detection_snapshot_states": ("snapshot_id", "raw_detection_id"),
            }[table]
            for key in table_keys:
                where = " AND ".join(f"{column}=:v{i}" for i, column in enumerate(columns))
                if db.execute(text(f"SELECT 1 FROM {table} WHERE {where}"),
                              {f"v{i}": value for i, value in enumerate(key)}).first():
                    raise VideoDeleteIntegrityError("Target rows remain after deletion")
        if db.execute(text("PRAGMA foreign_key_check")).first() is not None:
            raise VideoDeleteIntegrityError("Foreign-key integrity check failed")
    except VideoDeleteDBError:
        raise
    except SQLAlchemyError as exc:
        raise VideoDeleteIntegrityError("Database integrity prevented video deletion") from exc


def _worker_artifact_path(path: DeletePath) -> bool:
    name = PurePath(path.relative_key).name
    return bool(
        re.fullmatch(r"\.export(?:-\d+|_\d+_\d+)\.(?:staging|tmp\.zip)", name)
        or re.fullmatch(r"\.submission(?:-media-job-\d+-|_media_job_\d+_)[0-9a-fA-F]{32}\.staging", name)
        or re.fullmatch(r"\.clip_\d+_(?:rev\d+|revsub\d+)\.[0-9a-fA-F]{32}\.(?:mp4|jpg)\.part", name)
    )


def _same_frozen_graph(current: FrozenVideoDelete, frozen: FrozenVideoDelete) -> bool:
    """Compare DB authority exactly while allowing quarantined ephemeral paths to vanish."""
    if (current.project_id, current.video_id, current.actor_user_id,
        current.frozen_ids_by_table, current.frozen_keys_by_table, current.terminal_job_ids) != (
        frozen.project_id, frozen.video_id, frozen.actor_user_id,
        frozen.frozen_ids_by_table, frozen.frozen_keys_by_table, frozen.terminal_job_ids,
    ):
        return False
    current_regular = {path for path in current.paths if not _worker_artifact_path(path)}
    frozen_regular = {path for path in frozen.paths if not _worker_artifact_path(path)}
    current_ephemeral = {path for path in current.paths if _worker_artifact_path(path)}
    frozen_ephemeral = {path for path in frozen.paths if _worker_artifact_path(path)}
    return current_regular == frozen_regular and current_ephemeral <= frozen_ephemeral


def delete_frozen_video(session_source, frozen: FrozenVideoDelete, *, settings: Settings,
                        fault_hook: Callable[[str], None] | None = None) -> None:
    """Own an independent IMMEDIATE transaction and atomically delete ``frozen``.

    A live ``Session`` is deliberately not accepted: its autobegin/deferred state
    cannot prove that the delete lock was acquired before trigger or row changes.
    """
    if isinstance(session_source, Session):
        raise VideoDeleteIntegrityError("A session factory or engine is required")
    factory = sessionmaker(bind=session_source) if isinstance(session_source, Engine) else session_source
    if not callable(factory):
        raise VideoDeleteIntegrityError("A session factory or engine is required")
    db = factory()
    if not isinstance(db, Session):
        raise VideoDeleteIntegrityError("The session factory returned an invalid session")
    if db.in_transaction():
        # Do not rollback or close a possibly shared/scoped caller transaction.
        raise VideoDeleteIntegrityError("The deletion session already has a transaction")
    try:
        if db.get_bind().dialect.name != "sqlite":
            raise VideoDeleteIntegrityError("Hard video deletion requires SQLite")
        db.execute(text("BEGIN IMMEDIATE"))
        _delete_frozen_video_core(db, frozen, settings=settings, fault_hook=fault_hook)
        db.commit()
    except VideoDeleteDBError:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        raise VideoDeleteIntegrityError("Database integrity prevented video deletion") from exc
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
