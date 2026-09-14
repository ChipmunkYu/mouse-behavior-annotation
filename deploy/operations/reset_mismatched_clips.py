#!/usr/bin/env python3
"""One-off repair: reset ready clips whose decoded frame count != expected.

The renderer is fixed separately; this only flips already-stored bad rows back
to ``pending``. Legacy clips and media files are never touched.
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = "/data/mouse-annotation/data/annotation.db"
DEFAULT_CLIPS_DIR = "/data/mouse-annotation/data/clips"
DEFAULT_BACKUPS_DIR = "/data/mouse-annotation/backups"
CHUNK = 500
JOINS = ("FROM clips c JOIN submission_annotations sa ON sa.id = c.submission_annotation_id "
         "JOIN submissions s ON s.id = sa.submission_id JOIN videos v ON v.id = s.video_id")
SCOPE_SQL = ("SELECT c.id AS clip_id, c.submission_annotation_id AS annotation_id, v.filename AS filename, "
             "(sa.end_frame - sa.start_frame + 1) AS expected, c.clip_path AS clip_path " + JOINS +
             " WHERE c.status = 'ready' AND c.clip_path IS NOT NULL AND c.submission_annotation_id IS NOT NULL")
COUNT_SQL = ("SELECT c.status AS status, count(*) AS n " + JOINS +
             " WHERE c.submission_annotation_id IS NOT NULL")
UPDATE_SQL = ("UPDATE clips SET status='pending', clip_path=NULL, thumbnail_path=NULL, error=NULL, "
              "generated_at=NULL, updated_at=? WHERE id IN ({marks})")

def die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(2)

def connect_ro(path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection

def probe_frame_count(ffprobe: str, path: str) -> int | None:
    """Return the decoded frame count, or None when the file is missing/unreadable.

    Primary probe uses ``-count_frames`` + ``nb_read_frames``, matching the export
    contract's ``probe_clip`` gate; falls back to the fast container ``nb_frames``.
    """
    if not Path(path).is_file():
        return None
    probes = (
        (["-count_frames"], "stream=nb_read_frames"),
        ([], "stream=nb_frames"),
    )
    for prefix, entry in probes:
        command = [ffprobe, "-v", "error", *prefix, "-select_streams", "v:0",
                   "-show_entries", entry, "-of", "csv=p=0", str(path)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        value = result.stdout.strip()
        if value.isdigit():
            return int(value)
    return None

def fetch_rows(db_path, project_id: int | None) -> list[dict]:
    sql, params = SCOPE_SQL, []
    if project_id is not None:
        sql += " AND c.project_id = ?"
        params.append(project_id)
    connection = connect_ro(db_path)
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()

def classify(rows, ffprobe: str, clips_dir, probe=probe_frame_count):
    mismatched, unreadable = [], []
    for row in rows:
        row["actual"] = probe(ffprobe, str(Path(clips_dir) / row["clip_path"]))
        if row["actual"] is None:
            unreadable.append(row)
        elif row["actual"] != row["expected"]:
            mismatched.append(row)
    return mismatched, unreadable

def print_examples(mismatched: list[dict], limit: int) -> None:
    for row in mismatched[:limit]:
        print(f"{row['clip_id']} | {row['annotation_id']} | {row['filename']} | "
              f"{row['expected']} | {row['actual']}")

def run_apply(db_path, backups_dir, mismatched: list[dict]) -> None:
    if not mismatched:
        print("updated=0 (nothing to reset)")
        return
    backups = Path(backups_dir)
    if not backups.is_dir():
        die(f"backups directory does not exist: {backups}")
    backup = backups / f"pre-clip-frame-reset-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.db"
    if backup.exists():
        die(f"backup target already exists: {backup}")
    source = sqlite3.connect(str(db_path))
    try:
        target = sqlite3.connect(str(backup))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    check = connect_ro(backup)
    try:
        quick = [tuple(row) for row in check.execute("PRAGMA quick_check")]
    finally:
        check.close()
    if quick != [("ok",)]:
        die(f"backup quick_check failed, aborting: {quick}")
    ids = [row["clip_id"] for row in mismatched]
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = 0
        for start in range(0, len(ids), CHUNK):
            chunk = ids[start:start + CHUNK]
            marks = ",".join("?" * len(chunk))
            cursor = connection.execute(UPDATE_SQL.format(marks=marks), [now, *chunk])
            updated += cursor.rowcount
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    print(f"backup={backup}")
    print(f"updated={updated}")

def run_verify(db_path, project_id, mismatched: list[dict], unreadable: list[dict]) -> int:
    sql, params = COUNT_SQL, []
    if project_id is not None:
        sql += " AND c.project_id = ?"
        params.append(project_id)
    connection = connect_ro(db_path)
    try:
        counts = {row["status"]: row["n"] for row in connection.execute(sql + " GROUP BY c.status", params)}
    finally:
        connection.close()
    for status in ("ready", "pending", "processing", "failed"):
        print(f"{status}={counts.get(status, 0)}")
    print(f"mismatched={len(mismatched)} unreadable={len(unreadable)}")
    if mismatched:
        print_examples(mismatched, 20)
        return 1
    return 0

FIXTURE = (
    "CREATE TABLE videos(id INTEGER PRIMARY KEY, filename TEXT);"
    "CREATE TABLE submissions(id INTEGER PRIMARY KEY, video_id INTEGER);"
    "CREATE TABLE submission_annotations(id INTEGER PRIMARY KEY, submission_id INTEGER,"
    " start_frame INTEGER, end_frame INTEGER);"
    "CREATE TABLE clips(id INTEGER PRIMARY KEY, project_id INTEGER, submission_annotation_id INTEGER,"
    " status TEXT, clip_path TEXT, thumbnail_path TEXT, error TEXT, generated_at TEXT, updated_at TEXT);"
    "INSERT INTO videos VALUES(1,'v1.mp4');INSERT INTO submissions VALUES(1,1);"
    "INSERT INTO submission_annotations VALUES(1,1,0,4);"      # expected 5
    "INSERT INTO submission_annotations VALUES(2,1,10,16);"    # expected 7
    "INSERT INTO submission_annotations VALUES(3,1,100,102);"  # expected 3
    "INSERT INTO clips VALUES(1,1,1,'ready','c1.mp4','c1.jpg',NULL,NULL,NULL);"
    "INSERT INTO clips VALUES(2,1,2,'ready','c2.mp4','c2.jpg',NULL,NULL,NULL);"
    "INSERT INTO clips VALUES(3,1,3,'ready','c3.mp4','c3.jpg',NULL,NULL,NULL);"
    "INSERT INTO clips VALUES(4,1,NULL,'ready','legacy.mp4','legacy.jpg',NULL,NULL,NULL);"
)

def self_check() -> None:
    with tempfile.TemporaryDirectory(prefix="clip-reset-selfcheck-") as temporary:
        root = Path(temporary)
        clips_dir, backups = root / "clips", root / "backups"
        clips_dir.mkdir()
        backups.mkdir()
        for name in ("c1.mp4", "c2.mp4", "c3.mp4"):
            (clips_dir / name).write_bytes(b"x")
        db = root / "test.db"
        connection = sqlite3.connect(str(db))
        connection.executescript(FIXTURE)
        connection.close()
        frames = {"c1.mp4": 5, "c2.mp4": 6, "c3.mp4": 3}  # c2 is the one bad clip

        def stub_probe(_ffprobe: str, path: str) -> int | None:
            return frames.get(Path(path).name)

        rows = fetch_rows(db, None)
        assert len(rows) == 3, f"legacy clip must be excluded: {rows}"
        mismatched, unreadable = classify(rows, "ffprobe", clips_dir, probe=stub_probe)
        assert [row["clip_id"] for row in mismatched] == [2], mismatched
        assert unreadable == []
        print_examples(mismatched, 20)
        run_apply(db, backups, mismatched)
        connection = connect_ro(db)
        try:
            state = {row["id"]: dict(row) for row in connection.execute("SELECT * FROM clips")}
        finally:
            connection.close()
        assert state[2]["status"] == "pending", state[2]
        assert state[2]["clip_path"] is None and state[2]["thumbnail_path"] is None, state[2]
        assert state[1]["status"] == "ready" and state[1]["clip_path"] == "c1.mp4", state[1]
        assert state[4]["status"] == "ready" and state[4]["clip_path"] == "legacy.mp4", state[4]
        assert len(list(backups.glob("pre-clip-frame-reset-*.db"))) == 1
    print("SELF-CHECK OK")

def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--clips-dir", default=DEFAULT_CLIPS_DIR)
    parser.add_argument("--backups-dir", default=DEFAULT_BACKUPS_DIR)
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--project-id", type=int)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--self-check", action="store_true")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--verify", action="store_true")
    return parser.parse_args(argv)

def main(argv=None) -> int:
    args = parse_args(argv)
    if args.self_check:
        self_check()
        return 0
    if not Path(args.db).is_file():
        die(f"database not found: {args.db}")
    rows = fetch_rows(args.db, args.project_id)
    mismatched, unreadable = classify(rows, args.ffprobe, args.clips_dir)
    if args.verify:
        return run_verify(args.db, args.project_id, mismatched, unreadable)
    if args.apply:
        run_apply(args.db, args.backups_dir, mismatched)
        return 0
    print(f"total={len(rows)} mismatched={len(mismatched)} unreadable={len(unreadable)}")
    print_examples(mismatched, args.limit)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
