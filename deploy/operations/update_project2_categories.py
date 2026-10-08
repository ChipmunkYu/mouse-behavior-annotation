#!/usr/bin/env python3
"""One-shot, fail-closed Project 2 taxonomy/review reset (phase 1).

Production apply is deliberately disabled until phase 2 retires affected Clips and
filters exports.  The mutation function remains testable against offline copies.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import subprocess

PROJECT_ID = 2
SCHEMA = "0019"
PRODUCTION_DB = Path("/data/mouse-annotation/data/annotation.db")
PRODUCTION_BACKEND = Path("/opt/mouse-annotation/current/backend")
RETIRE_REASON = "Project 2 taxonomy migration"
DROP_TRIGGERS = ("trg_project_scheme_lock", "trg_category_locked_insert", "trg_category_locked_update")
BASELINE_COUNTS = {13: 123, 14: 160, 19: 41, 25: 50}
BASELINE_TOTAL, TARGET_TOTAL, TARGET_MOVING = 1734, 1643, 283
FIXED = {
    13: ("Running", "Moving"), 14: ("Walking", "Walking"),
    19: ("Avoiding", "Avoiding"), 25: ("Following", "Following"),
}
NEW_CATEGORIES = (
    ("Grooming", "个体行为", "#2A9D8F"),
    ("Rearing", "个体行为", "#E76F51"),
)
CATEGORY_FIELDS = ("id", "project_id", "name", "group", "color", "sort_order", "is_active",
                   "mouse_count_min", "mouse_count_max", "participant_mode", "role_definitions")


def category_spec(categories: list[dict]) -> list[dict]:
    result = []
    for category in categories:
        item = {field: category[field] for field in CATEGORY_FIELDS}
        item["is_active"] = bool(item["is_active"])
        if isinstance(item["role_definitions"], str):
            item["role_definitions"] = json.loads(item["role_definitions"])
        result.append(item)
    return result


def target_spec_from(baseline: list[dict]) -> list[dict]:
    target = json.loads(json.dumps(baseline, ensure_ascii=False))
    by_id = {row["id"]: row for row in target}
    by_id[13]["name"] = "Moving"
    for category_id in (14, 19, 25): by_id[category_id]["is_active"] = False
    active = [row for row in target if row["is_active"]]
    for order, row in enumerate(active): row["sort_order"] = order
    next_id = max(by_id) + 1
    for offset, (name, group, color) in enumerate(NEW_CATEGORIES):
        target.append({"id": next_id + offset, "project_id": PROJECT_ID, "name": name,
                       "group": group, "color": color, "sort_order": len(active) + offset,
                       "is_active": True, "mouse_count_min": 1, "mouse_count_max": 1,
                       "participant_mode": "unordered", "role_definitions": []})
    for order, category_id in enumerate((14, 19, 25), 15): by_id[category_id]["sort_order"] = order
    return sorted(target, key=lambda row: (row["sort_order"], row["id"]))


class Stop(RuntimeError):
    pass


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def emit(status: str, **values: object) -> None:
    print(canonical({"status": status, **values}))


def now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="microseconds")


def connect_ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise Stop(f"database is not a regular file: {path}")
    db = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def release_id(backend: Path) -> str:
    resolved = backend.resolve()
    release = resolved.parent.name
    if resolved.name != "backend" or resolved.parent.parent != Path("/opt/mouse-annotation/releases") or not re.fullmatch(r"[0-9a-f]{40}", release):
        raise Stop("release backend must resolve below /opt/mouse-annotation/releases/<commit>/backend")
    head = subprocess.run(["git", "-C", str(resolved.parent), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    if head != release:
        raise Stop("release directory and git HEAD differ")
    return release


def integrity(db: sqlite3.Connection) -> None:
    if [r[0] for r in db.execute("PRAGMA quick_check")] != ["ok"]:
        raise Stop("quick_check is not ok")
    errors = list(db.execute("PRAGMA foreign_key_check"))
    if errors:
        raise Stop(f"foreign_key_check returned {len(errors)} row(s)")


def normalize_sql(sql: str) -> str:
    return re.sub(r"^CREATE TRIGGER IF NOT EXISTS ", "CREATE TRIGGER ", " ".join(sql.split()),
                  count=1, flags=re.I)


def load_triggers(backend: Path) -> dict[str, str]:
    combined = {}
    for kind in ("authority", "assignee"):
        path = backend.resolve() / "app" / f"{kind}_triggers.py"
        if not path.is_file(): raise Stop(f"{kind} trigger module missing: {path}")
        spec = importlib.util.spec_from_file_location(f"fixed_project2_{kind}_triggers", path)
        if spec is None or spec.loader is None: raise Stop(f"cannot load {kind} trigger module")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        triggers = getattr(module, "TRIGGERS", None)
        if Path(module.__file__).resolve() != path or not isinstance(triggers, dict):
            raise Stop(f"invalid {kind} trigger definitions")
        if set(combined) & set(triggers): raise Stop("duplicate trigger names")
        combined.update(triggers)
    return combined


def check_triggers(db: sqlite3.Connection, expected: dict[str, str]) -> dict[str, str]:
    actual = {r["name"]: r["sql"] for r in db.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")}
    if set(actual) != set(expected):
        raise Stop(f"trigger set mismatch: missing={sorted(set(expected)-set(actual))}, "
                   f"unknown={sorted(set(actual)-set(expected))}")
    for name, body in expected.items():
        if normalize_sql(actual[name]) != normalize_sql(f"CREATE TRIGGER {name} {body}"):
            raise Stop(f"trigger definition mismatch: {name}")
    return {k: normalize_sql(actual[k]) for k in sorted(actual)}


def rows(db: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in db.execute(sql, params)]


def table_hash(db: sqlite3.Connection, table: str) -> str:
    columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
    primary_key = [r[1] for r in sorted(db.execute(f'PRAGMA table_info("{table}")'), key=lambda r: r[5]) if r[5]]
    order = primary_key or columns
    quoted = ",".join('"' + c.replace('"', '""') + '"' for c in columns)
    order_by = ",".join('"' + c.replace('"', '""') + '"' for c in order)
    digest = hashlib.sha256()
    digest.update(canonical({"columns": columns}).encode())
    for row in db.execute(f'SELECT {quoted} FROM "{table}" ORDER BY {order_by}'):
        encoded = [{"blob": bytes(value).hex()} if isinstance(value, (bytes, bytearray)) else value for value in row]
        digest.update(b"\n"); digest.update(canonical(encoded).encode())
    return digest.hexdigest()


def immutable_snapshot_hash(db: sqlite3.Connection) -> str:
    """Hash immutable snapshot payload; source_annotation_id may become NULL by FK design."""
    columns = [r[1] for r in db.execute("PRAGMA table_info(submission_annotations)")
               if r[1] != "source_annotation_id"]
    return sha([list(r) for r in db.execute(
        f'SELECT {",".join(chr(34)+c+chr(34) for c in columns)} FROM submission_annotations ORDER BY id')])


def material_state(annotation: dict) -> dict:
    def decoded(name: str, fallback):
        value = annotation[name]
        return fallback if value is None else json.loads(value)
    return {"category_id": annotation["category_id"], "start_frame": annotation["start_frame"],
            "end_frame": annotation["end_frame"], "confidence": annotation["confidence"],
            "crop_region": decoded("crop_region", None), "mouse_ids": decoded("mouse_ids", []),
            "participant_roles": decoded("participant_roles", {})}


def scheme_snapshot(project: dict, categories: list[dict]) -> dict:
    fields = ("id", "project_id", "name", "group", "color", "sort_order", "is_active",
              "mouse_count_min", "mouse_count_max", "participant_mode", "role_definitions")
    snapshots = [{field: category[field] for field in fields} for category in categories]
    for snapshot in snapshots:
        snapshot["is_active"] = bool(snapshot["is_active"])
        if isinstance(snapshot["role_definitions"], str):
            snapshot["role_definitions"] = json.loads(snapshot["role_definitions"])
    return {"project_id": project["id"], "category_scheme_version": project["category_scheme_version"],
            "category_scheme_locked_at": datetime.fromisoformat(
                str(project["category_scheme_locked_at"])).isoformat(),
            "category_scheme_locked_by": project["category_scheme_locked_by"],
            "categories": snapshots}


def inspect(db: sqlite3.Connection, backend: Path, *, expect_applied: bool = False, production_counts: bool = False,
            baseline_spec: list[dict] | None = None, include_fingerprint: bool = True) -> dict:
    version = [r[0] for r in db.execute("SELECT version_num FROM alembic_version")]
    allowed_versions = [SCHEMA] if expect_applied else ["0018", SCHEMA]
    if len(version) != 1 or version[0] not in allowed_versions:
        raise Stop(f"expected schema {' or '.join(allowed_versions)}")
    triggers = load_triggers(backend)
    trigger_snapshot = check_triggers(db, triggers)
    project = rows(db, "SELECT * FROM projects WHERE id=?", (PROJECT_ID,))
    if len(project) != 1 or not project[0]["category_scheme_locked_at"]:
        raise Stop("project 2 must exist exactly once with a locked taxonomy")
    owners = rows(db, "SELECT m.user_id FROM project_memberships m JOIN users u ON u.id=m.user_id "
                       "WHERE m.project_id=? AND m.role='owner' AND m.status='active'", (PROJECT_ID,))
    if len(owners) != 1:
        raise Stop("project 2 must have exactly one active owner")
    categories = rows(db, "SELECT * FROM behavior_categories WHERE project_id=? ORDER BY sort_order,id",
                      (PROJECT_ID,))
    by_id = {r["id"]: r for r in categories}
    actual_spec = category_spec(categories)
    if len(categories) == 16:
        canonical_baseline = actual_spec
    elif len(categories) == 18:
        previous = rows(db, "SELECT before_json FROM category_scheme_audits WHERE project_id=? AND action='replace' "
                            "AND scheme_version=? ORDER BY id DESC LIMIT 1",
                        (PROJECT_ID, project[0]["category_scheme_version"]))
        if not previous:
            raise Stop("production baseline gate: migration baseline audit is missing")
        canonical_baseline = category_spec(json.loads(previous[0]["before_json"])["categories"])
    if baseline_spec is not None and canonical_baseline != baseline_spec:
        raise Stop("production baseline gate: canonical baseline changed")
    expected_target = target_spec_from(canonical_baseline)
    initial = len(canonical_baseline) == 16 and actual_spec == canonical_baseline and all(
        i in by_id and by_id[i]["name"] == names[0] for i, names in FIXED.items())
    applied = actual_spec == expected_target
    if initial == applied:
        raise Stop("taxonomy is neither the exact initial nor exact applied state")
    if initial:
        if len(categories) != 16 or not all(c["is_active"] for c in categories) or [c["sort_order"] for c in categories] != list(range(16)):
            raise Stop("initial taxonomy must be exactly 16 active categories ordered 0..15")
        participant_fields = ("group", "mouse_count_min", "mouse_count_max", "participant_mode", "role_definitions")
        if any(by_id[13][field] != by_id[14][field] for field in participant_fields):
            raise Stop("Walking participants are incompatible with Running/Moving")
    if expect_applied and not applied:
        raise Stop("verification requires applied state")
    active = [c for c in categories if c["is_active"]]
    folded_names = [c["name"].strip().casefold() for c in categories]
    if len(folded_names) != len(set(folded_names)):
        raise Stop("category names must be unique ignoring case")
    if applied and (len(categories) != 18 or len(active) != 15 or
                    [c["sort_order"] for c in categories] != list(range(18)) or
                    [c["sort_order"] for c in active] != list(range(15))):
        raise Stop("applied taxonomy must order active 0..14 and inactive 15..17")
    if applied:
        audits = rows(db, "SELECT * FROM category_scheme_audits WHERE project_id=? AND action='replace' "
                          "ORDER BY id DESC LIMIT 1", (PROJECT_ID,))
        if len(audits) != 1:
            raise Stop("applied taxonomy audit is missing")
        audit = audits[0]
        before = json.loads(audit["before_json"])
        after = json.loads(audit["after_json"])
        expected_before = {**after, "category_scheme_version": after["category_scheme_version"] - 1,
                           "categories": canonical_baseline}
        if (audit["scheme_version"] != project[0]["category_scheme_version"] or
                audit["scheme_version"] != before["category_scheme_version"] + 1 or
                before != expected_before or
                audit["scheme_hash"] != sha(after) or after != scheme_snapshot(project[0], categories)):
            raise Stop(f"applied taxonomy audit/version/hash does not match current categories: {after!r} != {scheme_snapshot(project[0], categories)!r}")
    submitted = rows(db, "SELECT s.id FROM submissions s JOIN videos v ON v.id=s.video_id "
                         "WHERE v.project_id=? AND s.status='submitted'", (PROJECT_ID,))
    if submitted:
        raise Stop(f"active submitted attempts are unsafe: {[r['id'] for r in submitted]}")
    bad_workflow = rows(db, "SELECT id,workflow_status FROM videos WHERE project_id=? "
                           "AND workflow_status NOT IN ('draft','approved','rejected')", (PROJECT_ID,))
    if bad_workflow:
        raise Stop(f"unsupported video workflow state: {bad_workflow}")
    annotations = rows(db, "SELECT a.* FROM annotations a JOIN videos v ON v.id=a.video_id "
                           "WHERE v.project_id=? ORDER BY a.id", (PROJECT_ID,))
    counts = {i: sum(a["category_id"] == i for a in annotations) for i in FIXED}
    if initial and production_counts and (len(annotations) != BASELINE_TOTAL or counts != BASELINE_COUNTS):
        raise Stop(f"initial annotation counts differ from confirmed baseline: total={len(annotations)}, counts={counts}")
    if initial:
        colors = {c["color"].casefold() for c in categories}
        if any(color.casefold() in colors for _, _, color in NEW_CATEGORIES):
            raise Stop("fixed new category color conflicts with existing taxonomy")
    if applied and any(a["category_id"] in (14, 19, 25) for a in annotations):
        raise Stop("removed/merged category still has live annotations")
    retirement_columns = "retired_at,retired_reason" if version[0] == SCHEMA else "NULL retired_at,NULL retired_reason"
    clips = rows(db, "SELECT id,project_id,annotation_id,submission_annotation_id,clip_path,thumbnail_path," +
                     retirement_columns + " FROM clips WHERE project_id=? ORDER BY id", (PROJECT_ID,))
    fp = sha({"schema": SCHEMA, "project": project[0], "categories": categories,
              "annotation_ids": [(a["id"], a["category_id"]) for a in annotations],
              "clips": clips}) if include_fingerprint else None
    plan = {"project_id": PROJECT_ID, "from": "initial" if initial else "applied",
            "annotation_counts": counts, "actions": {"rename": "13 Running -> Moving",
            "merge": f"{counts[14]} live Walking -> 13", "delete": {"19": counts[19], "25": counts[25]},
            "add": [n for n, _, _ in NEW_CATEGORIES], "review_reset": len(annotations)},
            "retire_clips": len(clips)}
    return {"state": plan["from"], "fingerprint": fp, "plan": plan, "plan_hash": sha(plan),
            "project": project[0], "owner": owners[0]["user_id"], "categories": categories,
            "annotations": annotations, "triggers": triggers, "trigger_snapshot": trigger_snapshot,
            "baseline_spec": canonical_baseline, "target_spec": expected_target}


def plan(db_path: Path, backend: Path, *, expect_applied=False, production_counts=False) -> dict:
    with closing(connect_ro(db_path)) as db:
        production = production_counts or db_path.resolve() == PRODUCTION_DB.resolve()
        return inspect(db, backend, expect_applied=expect_applied, production_counts=production)


def verify_connection(db: sqlite3.Connection, backend: Path, *, production_counts=True,
                      include_fingerprint=False) -> dict:
    """Run all strict postconditions on the supplied connection."""
    info = inspect(db, backend, expect_applied=True, include_fingerprint=include_fingerprint)
    annotations = rows(db, "SELECT a.* FROM annotations a JOIN videos v ON v.id=a.video_id WHERE v.project_id=?", (PROJECT_ID,))
    if production_counts and (len(annotations) != TARGET_TOTAL or sum(a["category_id"] == 13 for a in annotations) != TARGET_MOVING):
        raise Stop("applied production annotation counts are not exact")
    if any(a["category_id"] in (14, 19, 25) or a["review_status"] != "pending" or a["reviewer_id"] is not None or
           a["material_state"] is None or json.loads(a["material_state"]) != material_state(a) or
           a["material_digest"] != sha(material_state(a)) for a in annotations):
        raise Stop("annotation reset/material verification failed")
    if db.execute("SELECT count(*) FROM videos WHERE project_id=? AND (workflow_status!='draft' OR submitted_at IS NOT NULL OR approved_at IS NOT NULL OR approved_by IS NOT NULL)", (PROJECT_ID,)).fetchone()[0]:
        raise Stop("video draft reset verification failed")
    if rows(db, "SELECT d.id FROM behavior_review_decisions d JOIN submission_annotations a ON a.id=d.submission_annotation_id JOIN submissions s ON s.id=a.submission_id JOIN videos v ON v.id=s.video_id WHERE v.project_id=? AND d.sequence=(SELECT max(x.sequence) FROM behavior_review_decisions x WHERE x.submission_annotation_id=d.submission_annotation_id) AND (d.status!='pending' OR d.feedback IS NOT NULL OR d.reviewer_id IS NOT NULL OR d.carried_from_decision_id IS NOT NULL)", (PROJECT_ID,)):
        raise Stop("latest decision authority was not reset")
    if rows(db, "SELECT s.id FROM submissions s JOIN videos v ON v.id=s.video_id WHERE v.project_id=? AND s.status IN ('submitted','approved')", (PROJECT_ID,)):
        raise Stop("submitted/approved submission remains")
    if db.execute("SELECT count(*) FROM clips WHERE project_id=? AND (retired_at IS NULL OR retired_reason!=?)",
                  (PROJECT_ID, RETIRE_REASON)).fetchone()[0]:
        raise Stop("project clips were not retired")
    if rows(db, "SELECT s.id FROM submissions s JOIN reviews r ON r.submission_id=s.id "
                "JOIN videos v ON v.id=s.video_id WHERE v.project_id=? AND r.result='approved' "
                "AND (s.status!='superseded' OR NOT EXISTS (SELECT 1 FROM behavior_review_reopens x WHERE x.submission_id=s.id))", (PROJECT_ID,)):
        raise Stop("approved review lacks superseded submission/reopen evidence")
    audit = rows(db, "SELECT * FROM category_scheme_audits WHERE project_id=? AND action='replace' ORDER BY id DESC LIMIT 1", (PROJECT_ID,))[0]
    before = json.loads(audit["before_json"]); after = json.loads(audit["after_json"])
    if audit["scheme_version"] != before["category_scheme_version"] + 1 or after["category_scheme_version"] != audit["scheme_version"]:
        raise Stop("audit/version/hash must advance exactly once")
    return info


def verify_applied(db_path: Path, backend: Path, *, production_counts=True) -> dict:
    with closing(connect_ro(db_path)) as db:
        return verify_connection(db, backend, production_counts=production_counts, include_fingerprint=True)


def backup(db_path: Path, destination: Path, backend: Path) -> dict:
    info = plan(db_path, backend)
    if destination.exists() or not destination.parent.is_dir():
        raise Stop("backup target must not exist and its parent must exist")
    source, target = connect_ro(db_path), None
    try:
        target = sqlite3.connect(destination)
        source.backup(target)
        target.close(); target = None
        with closing(connect_ro(destination)) as copied:
            integrity(copied)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        return {"path": str(destination), "sha256": digest, "fingerprint": info["fingerprint"],
                "plan_hash": info["plan_hash"]}
    except Exception:
        if target is not None: target.close()
        if destination.exists(): destination.unlink()
        raise
    finally:
        source.close()


def validate_backup(db_path: Path, backup_path: Path, digest: str, backend: Path,
                    fingerprint: str, plan_hash: str) -> None:
    if not backup_path.is_file() or backup_path.resolve() == db_path.resolve():
        raise Stop("backup must be a separate regular file")
    if hashlib.sha256(backup_path.read_bytes()).hexdigest() != digest.lower():
        raise Stop("backup SHA256 mismatch")
    info = plan(backup_path, backend)
    if (info["fingerprint"], info["plan_hash"]) != (fingerprint, plan_hash):
        raise Stop("backup fingerprint or plan hash mismatch")


def apply_offline(db_path: Path, backend: Path, fingerprint: str, plan_hash: str,
                  backup_path: Path, backup_sha: str, *, fault=None) -> str:
    if db_path.resolve() == PRODUCTION_DB.resolve():
        release_id(backend)
    baseline = plan(db_path, backend)
    if baseline["state"] == "applied":
        if (baseline["fingerprint"], baseline["plan_hash"]) != (fingerprint, plan_hash):
            raise Stop("repeat execution confirmations do not match")
        return "no-op"
    if (baseline["fingerprint"], baseline["plan_hash"]) != (fingerprint, plan_hash):
        raise Stop("live fingerprint or plan hash mismatch")
    validate_backup(db_path, backup_path, backup_sha, backend, fingerprint, plan_hash)
    protected = {"submission_annotations": None, "reviews": None}
    db = sqlite3.connect(db_path); db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON"); db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT version_num FROM alembic_version").fetchone()[0] != SCHEMA:
            raise Stop(f"apply requires schema {SCHEMA}")
        current = inspect(db, backend)
        if current["fingerprint"] != fingerprint or current["plan_hash"] != plan_hash:
            raise Stop("database changed before transaction lock")
        protected["submission_annotations"] = immutable_snapshot_hash(db)
        protected["reviews"] = table_hash(db, "reviews")
        old_history = {
            table: rows(db, f'SELECT * FROM "{table}" ORDER BY id')
            for table in ("behavior_review_decisions", "behavior_review_reopens")
        }
        before = scheme_snapshot(current["project"], current["categories"])
        clip_rows = rows(db, "SELECT id,clip_path,thumbnail_path FROM clips WHERE project_id=? ORDER BY id", (PROJECT_ID,))
        for name in DROP_TRIGGERS: db.execute(f'DROP TRIGGER "{name}"')
        stamp = now()
        db.execute("UPDATE clips SET retired_at=?,retired_reason=? WHERE project_id=? AND retired_at IS NULL",
                   (stamp, RETIRE_REASON, PROJECT_ID))
        db.execute("UPDATE clips SET annotation_id=NULL WHERE project_id=? AND annotation_id IN "
                   "(SELECT a.id FROM annotations a JOIN videos v ON v.id=a.video_id "
                   "WHERE v.project_id=? AND a.category_id IN (19,25))", (PROJECT_ID, PROJECT_ID))
        # Preserve immutable snapshots; only live rows move/delete.
        db.execute("UPDATE annotations SET category_id=13 WHERE category_id=14 AND video_id IN "
                   "(SELECT id FROM videos WHERE project_id=?)", (PROJECT_ID,))
        db.execute("DELETE FROM annotations WHERE category_id IN (19,25) AND video_id IN "
                   "(SELECT id FROM videos WHERE project_id=?)", (PROJECT_ID,))
        db.execute("UPDATE behavior_categories SET name='Moving' WHERE id=13 AND project_id=?", (PROJECT_ID,))
        db.execute("UPDATE behavior_categories SET is_active=0 WHERE project_id=? AND id IN (14,19,25)", (PROJECT_ID,))
        active = rows(db, "SELECT id FROM behavior_categories WHERE project_id=? AND is_active=1 ORDER BY sort_order,id", (PROJECT_ID,))
        for order, category in enumerate(active):
            db.execute("UPDATE behavior_categories SET sort_order=? WHERE id=?", (order, category["id"]))
        for offset, (name_, group, color) in enumerate(NEW_CATEGORIES, len(active)):
            db.execute("INSERT INTO behavior_categories(project_id,name,\"group\",color,sort_order,is_active,"
                       "mouse_count_min,mouse_count_max,participant_mode,role_definitions,created_at) "
                       "VALUES(?,?,?,?,?,1,1,1,'unordered','[]',?)", (PROJECT_ID, name_, group, color, offset, stamp))
        for offset, category_id in enumerate((14, 19, 25), 15):
            db.execute("UPDATE behavior_categories SET sort_order=? WHERE id=?", (offset, category_id))
        # Administratively neutralize every attempt while preserving append-only history.
        attempts = rows(db, "SELECT s.* FROM submissions s JOIN videos v ON v.id=s.video_id "
                            "WHERE v.project_id=? ORDER BY s.id", (PROJECT_ID,))
        for submission in attempts:
            sid = submission["id"]
            approved_review = db.execute(
                "SELECT 1 FROM reviews WHERE submission_id=? AND result='approved' LIMIT 1", (sid,)).fetchone()
            has_reopen = db.execute(
                "SELECT 1 FROM behavior_review_reopens WHERE submission_id=? LIMIT 1", (sid,)).fetchone()
            if submission["status"] == "approved":
                db.execute("UPDATE submissions SET status='superseded' WHERE id=?", (sid,))
            if (submission["status"] == "approved" or
                    (submission["status"] == "superseded" and approved_review)) and not has_reopen:
                db.execute("INSERT INTO behavior_review_reopens(submission_id,actor_id,reason,created_at) VALUES(?,?,?,?)",
                           (sid, current["owner"], "Project 2 taxonomy migration phase 1", stamp))
            latest = rows(db, "SELECT d.* FROM behavior_review_decisions d JOIN (SELECT submission_annotation_id,max(sequence) seq "
                              "FROM behavior_review_decisions GROUP BY submission_annotation_id) x "
                              "ON x.submission_annotation_id=d.submission_annotation_id AND x.seq=d.sequence "
                              "JOIN submission_annotations a ON a.id=d.submission_annotation_id WHERE a.submission_id=?", (sid,))
            revision = max([submission["decision_revision"], *(d["sequence"] for d in latest)])
            for decision in latest:
                if decision["status"] != "pending":
                    revision += 1
                    db.execute("INSERT INTO behavior_review_decisions(submission_annotation_id,status,feedback,sequence,reviewer_id,decided_at,origin) "
                               "VALUES(?,'pending',NULL,?,NULL,?,'reopen')",
                               (decision["submission_annotation_id"], revision, stamp))
            db.execute("UPDATE submissions SET decision_revision=? WHERE id=?", (revision, sid))
        # Every surviving live annotation gets fresh material authority and pending review.
        surviving = rows(db, "SELECT a.* FROM annotations a JOIN videos v ON v.id=a.video_id WHERE v.project_id=?", (PROJECT_ID,))
        for annotation in surviving:
            state = material_state(annotation); digest = sha(state)
            db.execute("UPDATE annotations SET review_status='pending',reviewer_id=NULL,material_revision=material_revision+1,"
                       "material_state=?,material_digest=?,updated_at=? WHERE id=?",
                       (canonical(state), digest, stamp, annotation["id"]))
        db.execute("UPDATE videos SET workflow_status='draft',annotation_revision=annotation_revision+1,"
                   "submitted_at=NULL,approved_at=NULL,approved_by=NULL WHERE project_id=?", (PROJECT_ID,))
        project = current["project"]
        db.execute("UPDATE projects SET category_scheme_version=category_scheme_version+1 WHERE id=?", (PROJECT_ID,))
        for name in DROP_TRIGGERS:
            db.execute(f"CREATE TRIGGER {name} {current['triggers'][name]}")
        if check_triggers(db, current["triggers"]) != current["trigger_snapshot"]:
            raise Stop("authority triggers were not restored exactly")
        after_categories = rows(db, "SELECT * FROM behavior_categories WHERE project_id=? ORDER BY sort_order,id", (PROJECT_ID,))
        project_after = rows(db, "SELECT * FROM projects WHERE id=?", (PROJECT_ID,))[0]
        after = scheme_snapshot(project_after, after_categories)
        db.execute("INSERT INTO category_scheme_audits(project_id,actor_id,action,scheme_version,before_json,after_json,scheme_hash,created_at) "
                   "VALUES(?,?, 'replace',?,?,?,?,?)", (PROJECT_ID, current["owner"], project["category_scheme_version"] + 1,
                   canonical(before), canonical(after), sha(after), stamp))
        if fault: fault()
        if (immutable_snapshot_hash(db) != protected["submission_annotations"] or
                table_hash(db, "reviews") != protected["reviews"]):
            raise Stop("immutable review history changed")
        for table, original in old_history.items():
            unchanged = (not original or rows(
                db, f'SELECT * FROM "{table}" WHERE id IN ({",".join("?" for _ in original)}) ORDER BY id',
                tuple(r["id"] for r in original)) == original)
            if not unchanged:
                raise Stop(f"existing append-only history changed: {table}")
        if rows(db, "SELECT id,clip_path,thumbnail_path FROM clips WHERE project_id=? ORDER BY id",
                (PROJECT_ID,)) != clip_rows:
            raise Stop("clip rows or media paths changed")
        verify_connection(db, backend, production_counts=False, include_fingerprint=False)
        db.commit()
    except Exception:
        db.rollback(); raise
    finally:
        db.close()
    return verify_applied(db_path, backend, production_counts=False)["fingerprint"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=("plan", "backup", "apply", "verify"))
    p.add_argument("--db", type=Path, default=PRODUCTION_DB)
    p.add_argument("--release-backend", type=Path, default=PRODUCTION_BACKEND)
    p.add_argument("--destination", type=Path)
    p.add_argument("--expect-fingerprint"); p.add_argument("--expect-plan-hash")
    p.add_argument("--confirm-release"); p.add_argument("--confirm-schema"); p.add_argument("--confirm-project", type=int)
    p.add_argument("--confirm-backup", type=Path); p.add_argument("--confirm-backup-sha256")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "plan":
            info = plan(args.db, args.release_backend)
            emit("plan", readonly=True, fingerprint=info["fingerprint"], plan_hash=info["plan_hash"], plan=info["plan"])
        elif args.command == "backup":
            if args.destination is None: raise Stop("backup requires --destination")
            emit("backup-created", **backup(args.db, args.destination, args.release_backend))
        elif args.command == "verify":
            info = verify_applied(args.db, args.release_backend)
            emit("verify-passed", fresh_connection=True, fingerprint=info["fingerprint"], plan_hash=info["plan_hash"])
        else:
            required = (args.expect_fingerprint, args.expect_plan_hash, args.confirm_backup,
                        args.confirm_backup_sha256)
            runtime_release = release_id(args.release_backend)
            if not all(required) or (args.confirm_release, args.confirm_schema, args.confirm_project) != (runtime_release, SCHEMA, PROJECT_ID):
                raise Stop("apply requires exact release/schema/project/fingerprint/plan hash and backup evidence")
            result = apply_offline(args.db, args.release_backend, args.expect_fingerprint, args.expect_plan_hash,
                                   args.confirm_backup, args.confirm_backup_sha256)
            emit("apply-complete" if result != "no-op" else "no-op", fingerprint=result)
        return 0
    except Exception as exc:
        emit("KEEP_SERVICE_STOPPED" if args.command == "apply" else "failed",
             error=type(exc).__name__, reason=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
