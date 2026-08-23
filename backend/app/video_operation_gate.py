"""In-process, non-blocking coordination for per-video operations.

Create one coordinator for each application instance and pass it to routes or
services that must not operate on the same video concurrently.  The module is
pure Python and deliberately does not create a process-wide coordinator.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock


class VideoOperationBusyError(RuntimeError):
    """Raised when any requested video is already being operated on."""

    def __init__(self, video_id: int, requested_video_ids: tuple[int, ...]) -> None:
        self.video_id = video_id
        self.requested_video_ids = requested_video_ids
        super().__init__(
            f"Video operation already in progress for video_id={video_id}; "
            "retry after the current operation finishes"
        )


@dataclass
class _LockRecord:
    lock: Lock = field(default_factory=Lock)
    users: int = 0


class VideoOperationGateCoordinator:
    """Thread-safe, app-scoped coordinator keyed by integer video IDs.

    Acquisition is non-blocking and all-or-nothing.  Multi-video requests are
    deduplicated and acquired in ascending ID order.
    """

    def __init__(self) -> None:
        self._registry_lock = Lock()
        self._records: dict[int, _LockRecord] = {}

    @property
    def record_count(self) -> int:
        """Return the current lock-record count, primarily for diagnostics."""
        with self._registry_lock:
            return len(self._records)

    @staticmethod
    def _normalize_video_ids(video_ids: Iterable[int]) -> tuple[int, ...]:
        normalized: set[int] = set()
        for video_id in video_ids:
            if isinstance(video_id, bool) or not isinstance(video_id, int):
                raise TypeError("video_id values must be integers")
            if video_id <= 0:
                raise ValueError("video_id values must be positive integers")
            normalized.add(video_id)
        if not normalized:
            raise ValueError("At least one video_id is required")
        return tuple(sorted(normalized))

    def _reserve(self, video_ids: tuple[int, ...]) -> list[_LockRecord]:
        records: list[_LockRecord] = []
        with self._registry_lock:
            for video_id in video_ids:
                record = self._records.get(video_id)
                if record is None:
                    record = _LockRecord()
                    self._records[video_id] = record
                record.users += 1
                records.append(record)
        return records

    def _discard_reservations(
        self, video_ids: tuple[int, ...], records: list[_LockRecord]
    ) -> None:
        with self._registry_lock:
            for video_id, record in zip(video_ids, records, strict=True):
                record.users -= 1
                if record.users == 0:
                    # A record with no users cannot have a legitimate lock owner:
                    # every contender reserves its record before acquisition.
                    assert not record.lock.locked()
                    if self._records.get(video_id) is record:
                        del self._records[video_id]

    @contextmanager
    def acquire(self, video_id: int) -> Iterator[int]:
        """Acquire one video's gate or immediately raise if it is busy."""
        with self.acquire_many((video_id,)) as video_ids:
            yield video_ids[0]

    @contextmanager
    def acquire_many(self, video_ids: Iterable[int]) -> Iterator[tuple[int, ...]]:
        """Acquire all requested gates in ascending order, without waiting."""
        ordered_ids = self._normalize_video_ids(video_ids)
        records = self._reserve(ordered_ids)
        acquired: list[_LockRecord] = []
        try:
            for video_id, record in zip(ordered_ids, records, strict=True):
                if not record.lock.acquire(blocking=False):
                    raise VideoOperationBusyError(video_id, ordered_ids)
                acquired.append(record)
            yield ordered_ids
        finally:
            for record in reversed(acquired):
                record.lock.release()
            self._discard_reservations(ordered_ids, records)


# Shorter name for service code that does not need the implementation detail in
# its dependency name.
VideoOperationCoordinator = VideoOperationGateCoordinator


__all__ = [
    "VideoOperationBusyError",
    "VideoOperationCoordinator",
    "VideoOperationGateCoordinator",
]
