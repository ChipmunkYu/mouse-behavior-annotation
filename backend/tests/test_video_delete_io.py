from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.config import Settings
from app.video_delete_io import DeletePath, VideoDeleteIO, VideoDeleteIOError


def _io(tmp_path: Path) -> VideoDeleteIO:
    settings = Settings(env="test", data_dir=tmp_path)
    for root in (
        settings.videos_dir, settings.exports_dir, settings.clips_dir,
        settings.thumbnails_dir, settings.import_batches_dir, settings.detection_imports_dir,
        settings.display_proxies_dir,
    ):
        root.mkdir(parents=True)
    return VideoDeleteIO(settings)


def test_rejects_escape_symlink_directory_and_unknown_root(tmp_path):
    io = _io(tmp_path)
    (tmp_path / "outside").write_bytes(b"x")
    (io.settings.videos_dir / "folder").mkdir()
    for item in (
        DeletePath("videos", "../outside"),
        DeletePath("videos", "folder"),
        DeletePath("unknown", "x"),
    ):
        with pytest.raises(VideoDeleteIOError):
            io.prepare(1, [item])
    link = io.settings.videos_dir / "link.mp4"
    try:
        link.symlink_to(tmp_path / "outside")
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(VideoDeleteIOError, match="symlink"):
        io.prepare(1, [DeletePath("videos", "link.mp4")])


def test_duplicate_missing_quarantine_restore_and_manifest_privacy(tmp_path):
    io = _io(tmp_path)
    source = io.settings.videos_dir / "a.mp4"
    source.write_bytes(b"video")
    manifest = io.prepare(7, [DeletePath("videos", "a.mp4"), ("videos", "a.mp4"), ("clips", "missing.mp4")], operation_id="op7")
    assert len(manifest.entries) == 2
    text = (io.quarantine_dir / "op7" / "manifest.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in text and "secret" not in text
    io.quarantine(manifest)
    assert not source.exists()
    io.restore("op7")
    assert source.read_bytes() == b"video"
    assert not (io.quarantine_dir / "op7").exists()


def test_display_proxy_root_deduplicates_and_round_trips(tmp_path):
    io = _io(tmp_path)
    proxy = io.settings.display_proxies_dir / "proxy.mp4"
    proxy.write_bytes(b"proxy")
    manifest = io.prepare(7, [
        DeletePath("display_proxies", proxy.name),
        DeletePath("display_proxies", proxy.name),
    ], operation_id="display-proxy")
    assert [(entry.root_kind, entry.relative_key) for entry in manifest.entries] == [
        ("display_proxies", proxy.name)
    ]
    io.quarantine(manifest)
    assert not proxy.exists()
    io.restore(manifest)
    assert proxy.read_bytes() == b"proxy"


def test_manifest_metadata_round_trip_and_id_validation(tmp_path):
    io = _io(tmp_path)
    manifest = io.prepare(
        7,
        [("videos", "missing.mp4")],
        operation_id="metadata",
        project_id=3,
        frozen_ids_by_table={"submissions": [9, 4, 9], "annotations": (8,)},
        terminal_job_ids=[12, 10, 12],
    )
    loaded = io.load("metadata")
    assert loaded.project_id == manifest.project_id == 3
    assert loaded.frozen_ids_by_table == (
        ("annotations", (8,)), ("submissions", (4, 9)),
    )
    assert loaded.terminal_job_ids == (10, 12)

    raw_text = (io.quarantine_dir / "metadata" / "manifest.json").read_text(encoding="utf-8")
    raw = json.loads(raw_text)
    assert raw["project_id"] == 3
    assert raw["frozen_ids_by_table"] == {"annotations": [8], "submissions": [4, 9]}
    assert raw["terminal_job_ids"] == [10, 12]
    assert str(tmp_path) not in raw_text

    for invalid in (True, False, 0, -1, "1", 1.0, None):
        with pytest.raises(VideoDeleteIOError, match="video-id-invalid"):
            io.prepare(invalid, [], operation_id=f"bad-{type(invalid).__name__}-{invalid}")


def test_partial_move_failure_restores_in_reverse(tmp_path, monkeypatch):
    io = _io(tmp_path)
    first = io.settings.videos_dir / "first.mp4"
    second = io.settings.videos_dir / "second.mp4"
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    manifest = io.prepare(1, [("videos", first.name), ("videos", second.name)], operation_id="partial")
    real_replace = os.replace
    calls = 0

    def fail_second(source, target):
        nonlocal calls
        if Path(source).name in {"first.mp4", "second.mp4"}:
            calls += 1
            if calls == 2:
                raise PermissionError
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_second)
    with pytest.raises(VideoDeleteIOError, match="quarantine-failed"):
        io.quarantine(manifest)
    assert first.read_bytes() == b"1" and second.read_bytes() == b"2"


def test_recovery_restores_or_purges_from_callback(tmp_path):
    io = _io(tmp_path)
    keep = io.settings.videos_dir / "keep.mp4"
    delete = io.settings.videos_dir / "delete.mp4"
    keep.write_bytes(b"k")
    delete.write_bytes(b"d")
    io.quarantine(io.prepare(10, [("videos", keep.name)], operation_id="keep"))
    io.quarantine(io.prepare(11, [("videos", delete.name)], operation_id="delete"))
    results = io.recover(lambda video_id: video_id == 10)
    assert all(result.ok for result in results)
    assert keep.read_bytes() == b"k" and not delete.exists()
    assert not (io.quarantine_dir / "delete").exists()


def test_purge_after_database_delete_and_already_missing(tmp_path):
    io = _io(tmp_path)
    source = io.settings.clips_dir / "clip.mp4"
    source.write_bytes(b"clip")
    manifest = io.prepare(3, [("clips", source.name), ("exports", "gone.zip")], operation_id="purge")
    io.quarantine(manifest)
    io.purge("purge")
    assert not source.exists() and not (io.quarantine_dir / "purge").exists()


def test_directory_quarantine_restore_and_startup_purge(tmp_path):
    io = _io(tmp_path)
    staging = io.settings.exports_dir / ".export-7.staging"
    nested = staging / "category" / "clip"
    nested.mkdir(parents=True)
    (nested / "metadata.json").write_text("{}", encoding="utf-8")
    manifest = io.prepare(
        7, [DeletePath("exports", staging.name, "directory")], operation_id="directory-restore",
    )
    io.quarantine(manifest)
    assert not staging.exists()
    if not io._descriptor_directory_operations_supported():
        with pytest.raises(VideoDeleteIOError, match="directory-descriptor-operations-unavailable"):
            io.restore(manifest)
        quarantine = io.quarantine_dir / "directory-restore" / "files" / "000000"
        assert (quarantine / "category" / "clip" / "metadata.json").read_text(encoding="utf-8") == "{}"
        return
    io.restore(manifest)
    assert (nested / "metadata.json").read_text(encoding="utf-8") == "{}"

    manifest = io.prepare(
        7, [DeletePath("exports", staging.name, "directory")], operation_id="directory-purge",
    )
    io.quarantine(manifest)
    results = io.recover(lambda _video_id: False)
    assert len(results) == 1
    assert results[0].ok and results[0].action == "purged"
    assert not staging.exists()


def test_directory_internal_symlink_and_kind_conflict_fail_closed(tmp_path):
    io = _io(tmp_path)
    staging = io.settings.exports_dir / ".export-8.staging"
    staging.mkdir()
    with pytest.raises(VideoDeleteIOError, match="path-kind-mismatch"):
        io.prepare(8, [DeletePath("exports", staging.name, "file")], operation_id="wrong-kind")

    outside = tmp_path / "outside-directory-file"
    outside.write_text("keep", encoding="utf-8")
    link = staging / "linked"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(VideoDeleteIOError, match="symlink"):
        io.prepare(8, [DeletePath("exports", staging.name, "directory")], operation_id="linked-dir")
    assert outside.read_text(encoding="utf-8") == "keep"


def test_partial_directory_purge_resumes_but_rejects_new_content(tmp_path, monkeypatch):
    io = _io(tmp_path)
    if not io._descriptor_directory_operations_supported():
        pytest.skip("descriptor-relative directory deletion unavailable")
    staging = io.settings.exports_dir / ".export-9.staging"
    staging.mkdir()
    (staging / "first").write_bytes(b"1")
    (staging / "second").write_bytes(b"2")
    manifest = io.quarantine(io.prepare(
        9, [DeletePath("exports", staging.name, "directory")], operation_id="partial-dir-purge",
    ))
    real_unlink = os.unlink
    failed = False

    def fail_second(path, *args, **kwargs):
        nonlocal failed
        if Path(path).name == "second" and not failed:
            failed = True
            raise PermissionError("injected")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", fail_second)
        with pytest.raises(PermissionError, match="injected"):
            io.purge(manifest)
    assert failed, "os.unlink fault hook was not exercised"
    results = io.recover(lambda _video_id: False)
    assert len(results) == 1 and results[0].ok and results[0].action == "purged"

    staging.mkdir()
    (staging / "owned").write_bytes(b"owned")
    io.quarantine(io.prepare(
        9, [DeletePath("exports", staging.name, "directory")], operation_id="changed-dir-purge",
    ))
    loaded = io.load("changed-dir-purge")
    loaded.entries[0].state = "purging"
    io._write_manifest(loaded)
    quarantine = io.quarantine_dir / "changed-dir-purge" / "files" / "000000"
    (quarantine / "injected").write_bytes(b"do-not-delete")
    results = io.recover(lambda _video_id: False)
    assert len(results) == 1 and not results[0].ok
    assert results[0].error == "identity-mismatch"
    assert (quarantine / "injected").read_bytes() == b"do-not-delete"


def test_directory_purge_fails_closed_when_descriptor_operations_are_unavailable(tmp_path, monkeypatch):
    io = _io(tmp_path)
    staging = io.settings.exports_dir / ".export-closed.staging"
    staging.mkdir()
    (staging / "owned").write_bytes(b"keep-in-quarantine")
    io.quarantine(io.prepare(
        10, [DeletePath("exports", staging.name, "directory")], operation_id="closed-dir-purge",
    ))
    monkeypatch.setattr(io, "_descriptor_directory_operations_supported", lambda: False)

    with pytest.raises(VideoDeleteIOError, match="directory-descriptor-operations-unavailable"):
        io.purge("closed-dir-purge")

    quarantine = io.quarantine_dir / "closed-dir-purge" / "files" / "000000"
    assert (quarantine / "owned").read_bytes() == b"keep-in-quarantine"


def test_descriptor_capability_is_frozen_across_fault_injection_monkeypatches(tmp_path, monkeypatch):
    io = _io(tmp_path)
    supported = io._descriptor_directory_operations_supported()

    monkeypatch.setattr(os, "unlink", lambda *args, **kwargs: None)
    monkeypatch.setattr(os, "rename", lambda *args, **kwargs: None)

    assert io._descriptor_directory_operations_supported() is supported


def test_descriptor_purge_rejects_nested_symlink_swap_after_stat(tmp_path, monkeypatch):
    io = _io(tmp_path)
    if not io._descriptor_directory_operations_supported():
        pytest.skip("descriptor-relative directory deletion unavailable")
    staging = io.settings.exports_dir / ".export-race.staging"
    nested = staging / "nested"
    nested.mkdir(parents=True)
    (nested / "owned").write_bytes(b"owned")
    outside = tmp_path / "outside-race"
    outside.mkdir()
    outside_file = outside / "must-survive"
    outside_file.write_bytes(b"outside")
    io.quarantine(io.prepare(
        11, [DeletePath("exports", staging.name, "directory")], operation_id="race-dir-purge",
    ))
    quarantine = io.quarantine_dir / "race-dir-purge" / "files" / "000000"
    swapped = False

    def swap_after_stat(relative_key):
        nonlocal swapped
        if relative_key == "nested" and not swapped:
            swapped = True
            (quarantine / "nested").rename(quarantine / "displaced")
            (quarantine / "nested").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(io, "_before_open_purge_child", swap_after_stat)
    with pytest.raises(VideoDeleteIOError, match="directory-open-failed|symlink-rejected|identity-mismatch"):
        io.purge("race-dir-purge")

    assert outside_file.read_bytes() == b"outside"
    assert (quarantine / "displaced" / "owned").read_bytes() == b"owned"


def test_descriptor_restore_rejects_nested_symlink_swap_after_stat(tmp_path, monkeypatch):
    io = _io(tmp_path)
    if not io._descriptor_directory_operations_supported():
        pytest.skip("descriptor-relative directory operations unavailable")
    staging = io.settings.exports_dir / ".export-restore-race.staging"
    nested = staging / "nested"
    nested.mkdir(parents=True)
    (nested / "owned").write_bytes(b"owned")
    outside = tmp_path / "outside-restore-race"
    outside.mkdir()
    outside_file = outside / "must-survive"
    outside_file.write_bytes(b"outside")
    manifest = io.quarantine(io.prepare(
        13, [DeletePath("exports", staging.name, "directory")], operation_id="race-dir-restore",
    ))
    quarantine = io.quarantine_dir / "race-dir-restore" / "files" / "000000"
    swapped = False

    def swap_after_stat(relative_key):
        nonlocal swapped
        if relative_key == "nested" and not swapped:
            swapped = True
            (quarantine / "nested").rename(quarantine / "displaced")
            (quarantine / "nested").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(io, "_before_open_purge_child", swap_after_stat)
    with pytest.raises(VideoDeleteIOError, match="directory-open-failed|symlink-rejected|identity-mismatch"):
        io.restore(manifest)

    assert not staging.exists()
    assert outside_file.read_bytes() == b"outside"
    assert (quarantine / "displaced" / "owned").read_bytes() == b"owned"


def test_descriptor_restore_rejects_quarantine_root_symlink_swap_before_rename(tmp_path, monkeypatch):
    io = _io(tmp_path)
    if not io._descriptor_directory_operations_supported():
        pytest.skip("descriptor-relative directory operations unavailable")
    staging = io.settings.exports_dir / ".export-root-restore-race.staging"
    staging.mkdir()
    (staging / "owned").write_bytes(b"owned")
    outside = tmp_path / "outside-root-restore-race"
    outside.mkdir()
    outside_file = outside / "must-survive"
    outside_file.write_bytes(b"outside")
    manifest = io.quarantine(io.prepare(
        14, [DeletePath("exports", staging.name, "directory")], operation_id="root-race-dir-restore",
    ))
    operation_dir = io.quarantine_dir / "root-race-dir-restore"
    quarantine = operation_dir / "files" / "000000"
    displaced = operation_dir / "files" / "displaced-root"

    def swap_root_before_rename(_entry):
        quarantine.rename(displaced)
        quarantine.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(io, "_before_restore_directory_rename", swap_root_before_rename)
    with pytest.raises(VideoDeleteIOError, match="identity-mismatch"):
        io.restore(manifest)

    assert outside_file.read_bytes() == b"outside"
    assert not staging.exists() and not staging.is_symlink()
    assert (displaced / "owned").read_bytes() == b"owned"
    raw = json.loads((operation_dir / "manifest.json").read_text(encoding="utf-8"))
    assert raw["phase"] == "quarantined"
    assert raw["entries"][0]["state"] == "quarantined"


def test_descriptor_restore_removes_root_symlink_swapped_during_rename(tmp_path, monkeypatch):
    io = _io(tmp_path)
    if not io._descriptor_directory_operations_supported():
        pytest.skip("descriptor-relative directory operations unavailable")
    staging = io.settings.exports_dir / ".export-root-rename-race.staging"
    staging.mkdir()
    (staging / "owned").write_bytes(b"owned")
    outside = tmp_path / "outside-root-rename-race"
    outside.mkdir()
    outside_file = outside / "must-survive"
    outside_file.write_bytes(b"outside")
    manifest = io.quarantine(io.prepare(
        16, [DeletePath("exports", staging.name, "directory")], operation_id="root-rename-race-restore",
    ))
    operation_dir = io.quarantine_dir / "root-rename-race-restore"
    quarantine = operation_dir / "files" / "000000"
    displaced = operation_dir / "files" / "displaced-root"
    real_rename = os.rename
    swapped = False

    def swap_during_rename(source, target, *args, **kwargs):
        nonlocal swapped
        if source == "000000" and target == staging.name and not swapped:
            swapped = True
            quarantine.rename(displaced)
            quarantine.symlink_to(outside, target_is_directory=True)
        return real_rename(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "rename", swap_during_rename)
    with pytest.raises(VideoDeleteIOError, match="identity-mismatch"):
        io.restore(manifest)
    assert swapped, "os.rename fault hook was not exercised"

    assert outside_file.read_bytes() == b"outside"
    assert not staging.exists() and not staging.is_symlink()
    assert (displaced / "owned").read_bytes() == b"owned"
    raw = json.loads((operation_dir / "manifest.json").read_text(encoding="utf-8"))
    assert raw["phase"] == "quarantined"
    assert raw["entries"][0]["state"] == "quarantined"


def test_descriptor_relative_directory_restore_succeeds_on_supported_platform(tmp_path):
    io = _io(tmp_path)
    if not io._descriptor_directory_operations_supported():
        pytest.skip("descriptor-relative directory operations unavailable")
    staging = io.settings.exports_dir / ".export-descriptor-restore.staging"
    (staging / "nested").mkdir(parents=True)
    (staging / "nested" / "owned").write_bytes(b"owned")
    manifest = io.quarantine(io.prepare(
        15, [DeletePath("exports", staging.name, "directory")], operation_id="descriptor-dir-restore",
    ))

    io.restore(manifest)

    assert (staging / "nested" / "owned").read_bytes() == b"owned"
    assert not (io.quarantine_dir / "descriptor-dir-restore").exists()


def test_descriptor_relative_directory_purge_succeeds_on_supported_platform(tmp_path):
    io = _io(tmp_path)
    if not io._descriptor_directory_operations_supported():
        pytest.skip("descriptor-relative directory deletion unavailable")
    staging = io.settings.exports_dir / ".export-descriptor.staging"
    (staging / "one" / "two").mkdir(parents=True)
    (staging / "one" / "two" / "owned").write_bytes(b"owned")
    io.quarantine(io.prepare(
        12, [DeletePath("exports", staging.name, "directory")], operation_id="descriptor-dir-purge",
    ))
    io.purge("descriptor-dir-purge")
    assert not (io.quarantine_dir / "descriptor-dir-purge").exists()


def test_mutations_fsync_affected_directories(tmp_path, monkeypatch):
    io = _io(tmp_path)
    source = io.settings.videos_dir / "durable.mp4"
    source.write_bytes(b"video")
    synced = []
    monkeypatch.setattr(io, "_fsync_directory", lambda path: synced.append(Path(path)))

    manifest = io.prepare(20, [("videos", source.name)], operation_id="durable")
    operation_dir = io.quarantine_dir / "durable"
    assert synced[:2] == [io.data_dir, io.quarantine_dir]

    synced.clear()
    files_parent_syncs = []

    def record_sync(path):
        path = Path(path)
        synced.append(path)
        if path == operation_dir:
            files_parent_syncs.append(((operation_dir / "files").exists(), source.exists()))

    monkeypatch.setattr(io, "_fsync_directory", record_sync)
    io.quarantine(manifest)
    quarantine_parent = operation_dir / "files"
    assert (True, True) in files_parent_syncs
    assert io.settings.videos_dir in synced
    assert quarantine_parent in synced

    synced.clear()
    io.restore("durable")
    assert quarantine_parent in synced
    assert io.settings.videos_dir in synced

    purge_source = io.settings.clips_dir / "purge-durable.mp4"
    purge_source.write_bytes(b"clip")
    purge_manifest = io.prepare(21, [("clips", purge_source.name)], operation_id="purge-durable")
    io.quarantine(purge_manifest)
    synced.clear()
    io.purge("purge-durable")
    assert io.quarantine_dir / "purge-durable" / "files" in synced


@pytest.mark.parametrize("temporary", [False, True])
def test_recovery_removes_only_safe_pre_manifest_orphan(tmp_path, monkeypatch, temporary):
    io = _io(tmp_path)

    def fail_manifest(_manifest):
        if temporary:
            (io.quarantine_dir / "orphan" / "manifest.json.tmp").write_text("partial")
        raise OSError("injected prepare crash")

    with monkeypatch.context() as patch:
        patch.setattr(io, "_write_manifest", fail_manifest)
        with pytest.raises(OSError, match="injected prepare crash"):
            io.prepare(30, [], operation_id="orphan")

    callbacks = []
    results = io.recover(lambda video_id: callbacks.append(video_id) or True)
    assert len(results) == 1
    assert results[0].ok and results[0].action == "orphan-removed"
    assert results[0].video_id == -1
    assert callbacks == []
    assert not (io.quarantine_dir / "orphan").exists()


@pytest.mark.parametrize("unsafe_kind", ["unknown-file", "files-content"])
def test_recovery_fails_closed_for_unsafe_manifestless_orphan(tmp_path, unsafe_kind):
    io = _io(tmp_path)
    operation_dir = io.quarantine_dir / "unsafe-orphan"
    operation_dir.mkdir(parents=True)
    if unsafe_kind == "unknown-file":
        (operation_dir / "unknown.bin").write_bytes(b"do not delete")
    else:
        files_dir = operation_dir / "files"
        files_dir.mkdir()
        (files_dir / "000000").write_bytes(b"quarantined")

    results = io.recover(lambda _video_id: True)
    assert len(results) == 1
    assert not results[0].ok and results[0].action == "stopped"
    assert results[0].error == "orphan-operation-unsafe"
    assert operation_dir.exists()
    assert any(operation_dir.rglob("*"))


def test_identity_replacement_and_both_locations_stop_without_overwrite(tmp_path):
    io = _io(tmp_path)
    source = io.settings.videos_dir / "replace.mp4"
    source.write_bytes(b"old")
    manifest = io.prepare(4, [("videos", source.name)], operation_id="replace")
    source.unlink()
    source.write_bytes(b"new-value")
    with pytest.raises(VideoDeleteIOError, match="identity-mismatch"):
        io.quarantine(manifest)
    assert source.read_bytes() == b"new-value"

    source.unlink()
    source.write_bytes(b"old")
    both = io.quarantine(io.prepare(5, [("videos", source.name)], operation_id="both"))
    source.write_bytes(b"do-not-overwrite")
    result = io.recover(lambda _video_id: True)
    failure = next(item for item in result if item.operation_id == "both")
    assert not failure.ok and failure.error == "both-locations-exist"
    assert source.read_bytes() == b"do-not-overwrite"


def test_manifest_is_versioned_and_atomic_replace_is_used(tmp_path, monkeypatch):
    io = _io(tmp_path)
    calls = []
    real_replace = os.replace

    def record(source, target):
        calls.append((Path(source).name, Path(target).name))
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", record)
    io.prepare(9, [("videos", "absent.mp4")], operation_id="atomic")
    raw = json.loads((io.quarantine_dir / "atomic" / "manifest.json").read_text(encoding="utf-8"))
    assert raw["version"] == 2
    assert ("manifest.json.tmp", "manifest.json") in calls


def _run_file_response(response, sent):
    import asyncio

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": "/stream", "raw_path": b"/stream",
        "query_string": b"", "headers": [], "client": ("test", 1), "server": ("test", 80),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(response(scope, receive, send))


def test_starlette_file_response_stat_start_open_order_and_pre_open_isolation(tmp_path, monkeypatch):
    """真实 ASGI 契约：1.5.1 simple response 先 stat、发头，再 open。"""
    import anyio
    import starlette.responses as responses

    source = tmp_path / "ordered.mp4"
    quarantine = tmp_path / "ordered.quarantine"
    source.write_bytes(b"response-body")
    events = []
    sent = []
    real_stat = os.stat
    real_open = anyio.open_file

    def record_stat(path, *args, **kwargs):
        if Path(path) == source:
            events.append("stat")
        return real_stat(path, *args, **kwargs)

    async def record_open(path, *args, **kwargs):
        if Path(path) == source:
            events.append("open")
        return await real_open(path, *args, **kwargs)

    async def send(message):
        if message["type"] == "http.response.start":
            events.append("start")
            os.replace(source, quarantine)
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": "/stream", "raw_path": b"/stream",
        "query_string": b"", "headers": [], "client": ("test", 1), "server": ("test", 80),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    monkeypatch.setattr(responses.os, "stat", record_stat)
    monkeypatch.setattr(responses.anyio, "open_file", record_open)
    with pytest.raises(FileNotFoundError):
        import asyncio
        asyncio.run(responses.FileResponse(source)(scope, receive, send))
    assert events == ["stat", "start", "open"]
    assert [item["type"] for item in sent] == ["http.response.start"]


@pytest.mark.skipif(os.name == "nt", reason="Windows open-handle sharing behavior is recorded separately")
def test_posix_opened_file_response_continues_after_same_filesystem_isolation(tmp_path, monkeypatch):
    import anyio
    import starlette.responses as responses

    source = tmp_path / "opened.mp4"
    quarantine = tmp_path / "opened.quarantine"
    content = b"opened-response-body"
    source.write_bytes(content)
    real_open = anyio.open_file
    opened = []

    async def open_then_isolate(path, *args, **kwargs):
        handle = await real_open(path, *args, **kwargs)
        opened.append(True)
        os.replace(source, quarantine)
        return handle

    sent = []
    monkeypatch.setattr(responses.anyio, "open_file", open_then_isolate)
    _run_file_response(responses.FileResponse(source), sent)
    body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    assert opened == [True]
    assert body == content
    assert not source.exists() and quarantine.exists()
