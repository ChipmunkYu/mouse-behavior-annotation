"""Threaded tests for the app-scoped video operation coordinator."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from app.video_operation_gate import (
    VideoOperationBusyError,
    VideoOperationGateCoordinator,
)


def test_same_video_contention_fails_without_waiting() -> None:
    coordinator = VideoOperationGateCoordinator()
    holder_entered = Event()
    release_holder = Event()

    def hold_video() -> None:
        with coordinator.acquire(7):
            holder_entered.set()
            assert release_holder.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(hold_video)
        assert holder_entered.wait(timeout=5)
        with pytest.raises(VideoOperationBusyError, match=r"video_id=7") as exc_info:
            with coordinator.acquire(7):
                pytest.fail("busy acquisition unexpectedly entered")
        assert exc_info.value.video_id == 7
        release_holder.set()
        future.result(timeout=5)


def test_different_videos_can_run_in_parallel() -> None:
    coordinator = VideoOperationGateCoordinator()
    both_entered = Barrier(3)
    release_workers = Event()

    def hold_video(video_id: int) -> None:
        with coordinator.acquire(video_id):
            both_entered.wait(timeout=5)
            assert release_workers.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(hold_video, video_id) for video_id in (1, 2)]
        both_entered.wait(timeout=5)
        release_workers.set()
        for future in futures:
            future.result(timeout=5)


def test_context_body_exception_releases_gate() -> None:
    coordinator = VideoOperationGateCoordinator()

    with pytest.raises(LookupError, match="body failed"):
        with coordinator.acquire(11):
            raise LookupError("body failed")

    with coordinator.acquire(11) as video_id:
        assert video_id == 11


def test_multi_video_partial_acquisition_rolls_back() -> None:
    coordinator = VideoOperationGateCoordinator()

    with coordinator.acquire(2):
        with pytest.raises(VideoOperationBusyError) as exc_info:
            with coordinator.acquire_many([3, 2, 1]):
                pytest.fail("partially busy acquisition unexpectedly entered")
        assert exc_info.value.video_id == 2
        assert exc_info.value.requested_video_ids == (1, 2, 3)

        # ID 1 was acquired before the busy ID and must have been rolled back.
        with coordinator.acquire(1):
            pass


def test_multi_video_order_is_stable_and_duplicate_ids_are_acquired_once() -> None:
    coordinator = VideoOperationGateCoordinator()

    with coordinator.acquire_many([9, 3, 5, 3]) as video_ids:
        assert video_ids == (3, 5, 9)
        with pytest.raises(VideoOperationBusyError):
            with coordinator.acquire(5):
                pass


@pytest.mark.parametrize("video_id", [0, -1])
def test_non_positive_video_ids_are_rejected(video_id: int) -> None:
    coordinator = VideoOperationGateCoordinator()

    with pytest.raises(ValueError, match="positive integers"):
        with coordinator.acquire(video_id):
            pytest.fail("invalid acquisition unexpectedly entered")


def test_bool_video_id_is_rejected() -> None:
    coordinator = VideoOperationGateCoordinator()

    with pytest.raises(TypeError, match="must be integers"):
        with coordinator.acquire(True):
            pytest.fail("invalid acquisition unexpectedly entered")


def test_unused_lock_records_are_reclaimed_after_success_and_failure() -> None:
    coordinator = VideoOperationGateCoordinator()

    with coordinator.acquire_many([4, 6]):
        assert coordinator.record_count == 2
        with pytest.raises(VideoOperationBusyError):
            with coordinator.acquire_many([5, 6, 7]):
                pass
        # Failed-only IDs 5 and 7 are reclaimed; held IDs remain registered.
        assert coordinator.record_count == 2

    assert coordinator.record_count == 0
