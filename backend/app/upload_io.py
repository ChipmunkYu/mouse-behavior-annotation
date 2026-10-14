"""Shared cancellation-safe, non-blocking upload file I/O."""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import anyio
from fastapi import UploadFile


def _setup(directory: Path, temp_path: Path, check_space: Callable[[int], None]):
    directory.mkdir(parents=True, exist_ok=True)
    check_space(0)
    return open(temp_path, "wb")


def _write(file_obj, chunk: bytes, check_space: Callable[[int], None]) -> None:
    check_space(len(chunk))
    file_obj.write(chunk)


def _publish(file_obj, temp_path: Path, final_path: Path, written: int, empty_error: Callable[[], BaseException]) -> None:
    file_obj.flush()
    os.fsync(file_obj.fileno())
    file_obj.close()
    if written == 0:
        raise empty_error()
    os.replace(temp_path, final_path)


def _cleanup(file_obj, paths: tuple[Path | None, ...]) -> None:
    if file_obj is not None and not file_obj.closed:
        try:
            file_obj.close()
        except OSError:
            pass
    for path in paths:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


async def cleanup_upload(*paths: Path | None, file_obj=None) -> None:
    """Finish cleanup even when the request task has already been cancelled."""
    with anyio.CancelScope(shield=True):
        await anyio.to_thread.run_sync(_cleanup, file_obj, paths)


async def atomic_save_upload(
    upload: UploadFile,
    directory: Path,
    final_name: str,
    chunk_size: int,
    check_space: Callable[[int], None],
    empty_error: Callable[[], BaseException],
) -> tuple[Path, int]:
    """Stream reads asynchronously and perform every filesystem operation in a worker."""
    final_path = directory / final_name
    temp_path = directory / f"{Path(final_name).stem}.part"
    file_obj = None
    written = 0
    try:
        file_obj = await anyio.to_thread.run_sync(_setup, directory, temp_path, check_space)
        while True:
            chunk = await upload.read(max(1, chunk_size))
            if not chunk:
                break
            await anyio.to_thread.run_sync(_write, file_obj, chunk, check_space)
            written += len(chunk)
        await anyio.to_thread.run_sync(
            _publish, file_obj, temp_path, final_path, written, empty_error
        )
        file_obj = None
        return final_path, written
    except BaseException:
        await cleanup_upload(temp_path, final_path, file_obj=file_obj)
        raise
