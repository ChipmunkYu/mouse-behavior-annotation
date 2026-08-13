"""Canonical SQLite-safe effective track ID domain and cursor helpers."""
from __future__ import annotations

from collections.abc import Iterable

TRACK_ID_UPPER_BOUND = 9223372036854775807


def is_valid_track_id(value: object) -> bool:
    """Track IDs are integers in [0, SQLite signed-int max)."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value < TRACK_ID_UPPER_BOUND
    )


def next_display_track_id(track_ids: Iterable[int]) -> int:
    """Return max+1 in the cursor domain; empty input starts at zero."""
    maximum = -1
    for track_id in track_ids:
        if not is_valid_track_id(track_id):
            raise ValueError(
                f"track_id must satisfy 0 <= id < {TRACK_ID_UPPER_BOUND}; got {track_id!r}"
            )
        maximum = max(maximum, track_id)
    # maximum is strictly below the bound, so the cursor may equal the bound.
    return maximum + 1
