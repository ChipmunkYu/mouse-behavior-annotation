#!/usr/bin/env python3
"""One-time Project 2 compensation: reject videos owning taxonomy-retired Clips."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

PROJECT_ID = 2
SCHEMA = "0020"
EXPECTED_CLIPS = 1400
TARGET_ANNOTATIONS = 1643
TARGET_MOVING = 283
RETIRE_REASON = "Project 2 taxonomy migration"
PRODUCTION_DB = Path("/data/mouse-annotation/data/annotation.db")


class Stop(RuntimeError):
    pass


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_rows(db: sqlite3.Connection, sql: str, params=()) -> str:
    digest = hashlib.sha256()
    for row in db.execute(sql, params):
        digest.update(canonical(list(row)).encode()); digest.update(b"\n")
    return digest.hexdigest()


def connect(path: Path, readonly=False) -> sqlite3.Connection:
    if not path.is_file():
        raise Stop(f"database is not a regular file: {path}")
    db = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True) if readonly else sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def target_video_ids(db: sqlite3.Connection) -> list[int]:
    mappings = list(db.execute(
        "SELECT c.id,a.video_id annotation_video_id,s.video_id submission_video_id,coalesce(a.video_id,s.video_id) video_id "
        "FROM clips c LEFT JOIN annotations a ON a.id=c.annotation_id "
        "LEFT JOIN submission_annotations sa ON sa.id=c.submission_annotation_id "
        "LEFT JOIN submissions s ON s.id=sa.submission_id "
        "WHERE c.project_id=? AND c.retired_reason=? ORDER BY c.id",
        (PROJECT_ID, RETIRE_REASON)))
    if len(mappings) != EXPECTED_CLIPS:
        raise Stop(f"expected exactly {EXPECTED_CLIPS} taxonomy-retired Clips")
    if any(row["annotation_video_id"] is not None and row["submission_video_id"] is not None and
           row["annotation_video_id"] != row["submission_video_id"] for row in mappings):
        raise Stop("retired Clip has conflicting video associations")
    ids = sorted({row["video_id"] for row in mappings if row["video_id"] is not None})
    if not ids:
        raise Stop("taxonomy-retired Clips do not identify any target video")
    return ids


def inspect(db: sqlite3.Connection) -> dict:
    if [row[0] for row in db.execute("PRAGMA quick_check")] != ["ok"] or list(db.execute("PRAGMA foreign_key_check")):
        raise Stop("database integrity check failed")
    if [row[0] for row in db.execute("SELECT version_num FROM alembic_version")] != [SCHEMA]:
        raise Stop(f"expected schema {SCHEMA}")
    if db.execute("SELECT count(*) FROM projects WHERE id=?", (PROJECT_ID,)).fetchone()[0] != 1:
        raise Stop("project 2 must exist exactly once")
    ids = target_video_ids(db)
    placeholders = ",".join("?" for _ in ids)
    videos = list(db.execute(
        f"SELECT id,workflow_status,submitted_at,approved_at,approved_by FROM videos WHERE id IN ({placeholders}) ORDER BY id", ids))
    if len(videos) != len(ids) or any(row["workflow_status"] not in ("draft", "rejected") for row in videos):
        raise Stop("target videos must be draft or rejected")
    if any(row["submitted_at"] is not None or row["approved_at"] is not None or row["approved_by"] is not None for row in videos):
        raise Stop("target video review timestamps must already be NULL")
    categories = list(db.execute("SELECT id,name,is_active,sort_order FROM behavior_categories WHERE project_id=? ORDER BY sort_order,id", (PROJECT_ID,)))
    names = {row["name"]: row for row in categories}
    if len(categories) != 18 or len([row for row in categories if row["is_active"]]) != 15 or any(
            name not in names for name in ("Moving", "Grooming", "Rearing")) or any(
            names[name]["is_active"] for name in ("Walking", "Avoiding", "Following")):
        raise Stop("Project 2 taxonomy baseline differs")
    counts = db.execute("SELECT count(*),sum(category_id=13),sum(category_id IN (14,19,25)),sum(review_status!='pending' OR reviewer_id IS NOT NULL) "
                        "FROM annotations WHERE video_id IN (SELECT id FROM videos WHERE project_id=?)", (PROJECT_ID,)).fetchone()
    if tuple(counts) != (TARGET_ANNOTATIONS, TARGET_MOVING, 0, 0):
        raise Stop(f"Project 2 annotation baseline differs: {tuple(counts)}")
    clip_count = db.execute("SELECT count(*) FROM clips WHERE project_id=? AND retired_at IS NOT NULL AND retired_reason=?",
                            (PROJECT_ID, RETIRE_REASON)).fetchone()[0]
    total_clips = db.execute("SELECT count(*) FROM clips WHERE project_id=?", (PROJECT_ID,)).fetchone()[0]
    if clip_count != EXPECTED_CLIPS or total_clips != EXPECTED_CLIPS:
        raise Stop(f"expected exactly {EXPECTED_CLIPS} retired Clips")
    state = {
        "distance": digest_rows(db, "SELECT id,distance_calibration FROM videos WHERE project_id=? ORDER BY id", (PROJECT_ID,)),
        "submissions": digest_rows(db, "SELECT s.* FROM submissions s JOIN videos v ON v.id=s.video_id WHERE v.project_id=? ORDER BY s.id", (PROJECT_ID,)),
        "submission_annotations": digest_rows(db, "SELECT sa.* FROM submission_annotations sa JOIN submissions s ON s.id=sa.submission_id JOIN videos v ON v.id=s.video_id WHERE v.project_id=? ORDER BY sa.id", (PROJECT_ID,)),
        "reviews": digest_rows(db, "SELECT r.* FROM reviews r WHERE r.project_id=? ORDER BY r.id", (PROJECT_ID,)),
        "decisions": digest_rows(db, "SELECT d.* FROM behavior_review_decisions d JOIN submission_annotations sa ON sa.id=d.submission_annotation_id JOIN submissions s ON s.id=sa.submission_id JOIN videos v ON v.id=s.video_id WHERE v.project_id=? ORDER BY d.id", (PROJECT_ID,)),
        "reopens": digest_rows(db, "SELECT r.* FROM behavior_review_reopens r JOIN submissions s ON s.id=r.submission_id JOIN videos v ON v.id=s.video_id WHERE v.project_id=? ORDER BY r.id", (PROJECT_ID,)),
        "taxonomy_annotations": digest_rows(db, "SELECT a.* FROM annotations a JOIN videos v ON v.id=a.video_id WHERE v.project_id=? ORDER BY a.id", (PROJECT_ID,)),
    }
    token = hashlib.sha256(canonical({"ids": ids, "videos": [list(row) for row in videos], **state}).encode()).hexdigest()
    return {"target_video_ids": ids, "target_videos": len(ids), "draft": sum(row["workflow_status"] == "draft" for row in videos),
            "rejected": sum(row["workflow_status"] == "rejected" for row in videos), "retired_clips": clip_count,
            "digests": state, "plan_hash": token}


def plan(path: Path) -> dict:
    with closing(connect(path, readonly=True)) as db:
        return inspect(db)


def apply(path: Path) -> dict:
    baseline = plan(path)
    db = connect(path)
    try:
        db.execute("PRAGMA foreign_keys=ON"); db.execute("BEGIN IMMEDIATE")
        locked = inspect(db)
        if locked["plan_hash"] != baseline["plan_hash"]:
            raise Stop("database changed before transaction lock")
        ids = locked["target_video_ids"]
        db.execute(f"UPDATE videos SET workflow_status='rejected' WHERE workflow_status='draft' AND id IN ({','.join('?' for _ in ids)})", ids)
        verified = inspect(db)
        if verified["draft"] or verified["digests"] != baseline["digests"]:
            raise Stop("postcondition or protected data verification failed")
        db.commit()
    except Exception:
        db.rollback(); raise
    finally:
        db.close()
    fresh = plan(path)
    if fresh["draft"] or fresh["digests"] != baseline["digests"]:
        raise Stop("fresh verification failed")
    return fresh


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=PRODUCTION_DB)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = apply(args.db) if args.apply else plan(args.db)
        print(canonical({"status": "applied" if args.apply else "plan", "readonly": not args.apply, **result}))
        return 0
    except Exception as exc:
        print(canonical({"status": "failed", "error": type(exc).__name__, "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
