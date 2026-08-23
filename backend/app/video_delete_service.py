"""Application service for the synchronous, recoverable hard-video-delete flow."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .models import Annotation, Submission, SubmissionAnnotation, Video
from .related_video_jobs import identify_related_video_jobs
from .video_delete_db import (
    FrozenVideoDelete, VideoDeleteConflictError, VideoDeleteDBError,
    delete_frozen_video, freeze_video_delete,
)
from .video_delete_io import DeleteManifest, VideoDeleteIO, VideoDeleteIOError
from .video_operation_gate import VideoOperationBusyError, VideoOperationGateCoordinator

logger = logging.getLogger(__name__)


class VideoDeleteServiceError(RuntimeError):
    """A filesystem/protocol failure with a stable API-safe phase."""

    def __init__(self, message: str, *, database_deleted: bool = False) -> None:
        self.safe_message = message
        self.database_deleted = database_deleted
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class VideoDeleteResult:
    project_id: int
    video_id: int
    terminal_job_ids: tuple[int, ...]


class VideoDeleteService:
    """Coordinate the app-scoped gate, DB freeze/delete, and file quarantine."""

    def __init__(self, *, session_factory: sessionmaker, settings: Settings,
                 gate: VideoOperationGateCoordinator) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.gate = gate
        self.io = VideoDeleteIO(settings)

    def delete(self, *, project_id: int, video_id: int,
               actor_user_id: int) -> VideoDeleteResult:
        logger.info(
            "video_delete_started",
            extra={"event": "video_delete_started", "project_id": project_id,
                   "video_id": video_id, "actor_user_id": actor_user_id},
        )
        try:
            with self.gate.acquire(video_id):
                return self._delete_locked(
                    project_id=project_id, video_id=video_id,
                    actor_user_id=actor_user_id,
                )
        except VideoOperationBusyError:
            self._log_failure(project_id, video_id, "operation_gate", "video operation is busy")
            raise

    def _delete_locked(self, *, project_id: int, video_id: int,
                       actor_user_id: int) -> VideoDeleteResult:
        phase = "freeze"
        try:
            frozen = self._freeze(project_id, video_id, actor_user_id)
            manifest: DeleteManifest | None = None
            committed = False
            try:
                phase = "prepare"
                manifest = self.io.prepare(
                    video_id, frozen.paths, project_id=project_id,
                    frozen_ids_by_table=dict(frozen.frozen_ids_by_table),
                    terminal_job_ids=frozen.terminal_job_ids,
                )
                phase = "quarantine"
                self.io.quarantine(manifest)
                phase = "database_delete"
                delete_frozen_video(self.session_factory, frozen, settings=self.settings)
                committed = True
                phase = "purge"
                self.io.purge(manifest)
            except VideoDeleteIOError as exc:
                if (manifest is not None and not committed
                        and (self.io.quarantine_dir / manifest.operation_id).exists()):
                    try:
                        self.io.restore(manifest)
                    except VideoDeleteIOError:
                        logger.exception("Video delete rollback restore failed for video_id=%s", video_id)
                        self._log_failure(project_id, video_id, "restore", "administrator recovery required")
                        raise VideoDeleteServiceError(
                            "Video was not deleted, but file restoration requires administrator recovery"
                        ) from exc
                if committed:
                    self._log_failure(project_id, video_id, "purge", "cleanup pending recovery")
                    raise VideoDeleteServiceError(
                        "Video data was deleted, but file cleanup remains pending startup recovery",
                        database_deleted=True,
                    ) from exc
                self._log_failure(project_id, video_id, phase, "file isolation failed")
                raise VideoDeleteServiceError("Video was not deleted because file isolation failed") from exc
            except BaseException:
                if (manifest is not None and not committed
                        and (self.io.quarantine_dir / manifest.operation_id).exists()):
                    try:
                        self.io.restore(manifest)
                    except VideoDeleteIOError as restore_exc:
                        logger.exception("Video delete rollback restore failed for video_id=%s", video_id)
                        self._log_failure(project_id, video_id, "restore", "administrator recovery required")
                        raise VideoDeleteServiceError(
                            "Video was not deleted, but file restoration requires administrator recovery"
                        ) from restore_exc
                raise
            row_counts = {table: len(ids) for table, ids in frozen.frozen_ids_by_table}
            logger.info(
                "video_delete_finished",
                extra={"event": "video_delete_finished", "project_id": project_id,
                       "video_id": video_id, "actor_user_id": actor_user_id,
                       "final_phase": "purged", "row_counts": row_counts,
                       "terminal_job_count": len(frozen.terminal_job_ids),
                       "terminal_job_ids": list(frozen.terminal_job_ids)},
            )
            return VideoDeleteResult(project_id, video_id, frozen.terminal_job_ids)
        except VideoDeleteConflictError as exc:
            self._log_blocking_jobs(project_id, video_id)
            self._log_failure(project_id, video_id, phase, exc.safe_message)
            raise
        except VideoDeleteDBError as exc:
            self._log_failure(project_id, video_id, phase, exc.safe_message)
            raise
        except VideoDeleteServiceError:
            raise
        except BaseException:
            self._log_failure(project_id, video_id, phase, "persistent delete operation failed")
            raise

    @staticmethod
    def _log_failure(project_id: int, video_id: int, phase: str, summary: str) -> None:
        logger.error(
            "video_delete_failed",
            extra={"event": "video_delete_failed", "project_id": project_id,
                   "video_id": video_id, "failure_phase": phase,
                   "final_phase": "failed", "failure_summary": summary},
        )

    def _log_blocking_jobs(self, project_id: int, video_id: int) -> None:
        """Log safe identifiers for related jobs that prevented the freeze."""
        try:
            with self.session_factory() as db:
                annotation_ids = tuple(row[0] for row in db.query(Annotation.id).filter_by(video_id=video_id))
                submission_ids = tuple(row[0] for row in db.query(Submission.id).filter_by(video_id=video_id))
                submission_annotation_ids = tuple(
                    row[0] for row in db.query(SubmissionAnnotation.id).filter(
                        SubmissionAnnotation.submission_id.in_(submission_ids)
                    )
                ) if submission_ids else ()
                jobs = identify_related_video_jobs(
                    db, project_id=project_id, video_id=video_id,
                    annotation_ids=annotation_ids, submission_ids=submission_ids,
                    submission_annotation_ids=submission_annotation_ids,
                )
                for job in (*jobs.active, *jobs.unknown):
                    logger.warning(
                        "video_delete_blocked_job",
                        extra={"event": "video_delete_blocked_job", "project_id": project_id,
                               "video_id": video_id, "blocking_job_id": job.id,
                               "blocking_job_status": job.status},
                    )
        except Exception:
            logger.warning(
                "video_delete_blocked_job_observation_failed",
                extra={"event": "video_delete_blocked_job_observation_failed",
                       "project_id": project_id, "video_id": video_id},
            )

    def _freeze(self, project_id: int, video_id: int,
                actor_user_id: int) -> FrozenVideoDelete:
        with self.session_factory() as db:
            return freeze_video_delete(
                db, project_id=project_id, video_id=video_id,
                actor_user_id=actor_user_id, settings=self.settings,
            )

    def recover(self):
        """Restore pre-commit operations and purge post-commit operations at startup."""
        def video_exists(video_id: int) -> bool:
            with self.session_factory() as db:
                return db.query(Video.id).filter(Video.id == video_id).first() is not None

        results = self.io.recover(video_exists)
        for result in results:
            if result.ok:
                logger.info("Recovered video delete operation=%s video=%s action=%s",
                            result.operation_id, result.video_id, result.action)
            else:
                logger.error("Video delete recovery stopped operation=%s video=%s error=%s",
                             result.operation_id, result.video_id, result.error)
        return results


__all__ = [
    "VideoDeleteResult", "VideoDeleteService", "VideoDeleteServiceError",
    "VideoOperationBusyError",
]
