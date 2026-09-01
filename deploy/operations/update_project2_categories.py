#!/usr/bin/env python3
"""Fail-closed, offline-prepared project 2 category maintenance operation."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from datetime import datetime, timezone

PROJECT_ID = 2
RELEASE = "50be725743254c0fa55ae3b21de646d457211417"
SCHEMA = "0016"
PRODUCTION_DB = Path("/data/mouse-annotation/data/annotation.db")
PRODUCTION_BACKEND = Path(f"/opt/mouse-annotation/releases/{RELEASE}/backend")
TARGET_NAMES = ("Following", "Group locomotion", "Social clustering", "Dispersal")
FOLLOWER_KEY = "role_fa688d903bd22a493398b22bfd7d65cd"
LEADER_KEY = "role_005fe72c3604718350b2a1beade6eaf4"
CATEGORY_COLUMNS = (
    "id", "project_id", "name", "group", "color", "sort_order", "is_active",
    "mouse_count_min", "mouse_count_max", "participant_mode", "role_definitions", "created_at",
)
SNAPSHOT_COLUMNS = CATEGORY_COLUMNS[:-1]
TEMPORARILY_DROPPED = ("trg_category_locked_insert", "trg_project_scheme_lock")


class Stop(RuntimeError):
    pass


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def emit(status: str, **values: object) -> None:
    print(canonical({"status": status, **values}))


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="microseconds")


def iso_datetime(value: object) -> str | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value)).isoformat()


def normalize_sql(value: str) -> str:
    return " ".join(value.split())


def normalize_trigger_sql(value: str) -> str:
    normalized = normalize_sql(value)
    return re.sub(r"^CREATE TRIGGER IF NOT EXISTS ", "CREATE TRIGGER ", normalized,
                  count=1, flags=re.IGNORECASE)


def connect_ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise Stop(f"database is not a regular file: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def load_triggers(backend: Path) -> dict[str, str]:
    backend = backend.resolve()
    trigger_groups = []
    for module_name in ("authority_triggers", "assignee_triggers"):
        module_path = backend / "app" / f"{module_name}.py"
        if not module_path.is_file():
            raise Stop(f"trigger module missing: {module_path}")
        spec = importlib.util.spec_from_file_location(f"project2_fixed_release_{module_name}", module_path)
        if spec is None or spec.loader is None:
            raise Stop(f"cannot load trigger module: {module_name}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded_file = getattr(module, "__file__", None)
        if loaded_file is None:
            raise Stop(f"trigger module has no __file__: {module_name}")
        loaded_path = Path(loaded_file).resolve()
        if loaded_path != module_path.resolve() or loaded_path.parent.parent != backend:
            raise Stop(f"trigger module did not load from the exact requested release backend: {module_name}")
        triggers = getattr(module, "TRIGGERS", None)
        if not isinstance(triggers, dict) or not triggers or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in triggers.items()
        ):
            raise Stop(f"release TRIGGERS definition is invalid: {module_name}")
        trigger_groups.append(triggers)
    conflicts = set(trigger_groups[0]) & set(trigger_groups[1])
    if conflicts:
        raise Stop(f"trigger names conflict between release modules: {sorted(conflicts)}")
    triggers = trigger_groups[0] | trigger_groups[1]
    for name in TEMPORARILY_DROPPED:
        if name not in triggers:
            raise Stop(f"required trigger absent from release definitions: {name}")
    return triggers


def expected_trigger_sql(name: str, body: str) -> str:
    return f"CREATE TRIGGER {name} {body}"


def trigger_rows(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        row["name"]: row["sql"]
        for row in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
        )
    }


def check_triggers(connection: sqlite3.Connection, triggers: dict[str, str]) -> dict[str, str]:
    actual = trigger_rows(connection)
    missing = set(triggers) - set(actual)
    unknown = set(actual) - set(triggers)
    if missing or unknown:
        raise Stop(f"trigger name set mismatch: missing={sorted(missing)}, unknown={sorted(unknown)}")
    for name, body in triggers.items():
        if normalize_trigger_sql(actual[name]) != normalize_trigger_sql(expected_trigger_sql(name, body)):
            raise Stop(f"trigger definition mismatch: {name}")
    return {name: normalize_trigger_sql(actual[name]) for name in sorted(triggers)}


def integrity(connection: sqlite3.Connection) -> None:
    quick = [row[0] for row in connection.execute("PRAGMA quick_check")]
    if quick != ["ok"]:
        raise Stop(f"quick_check failed: {quick}")
    foreign = list(connection.execute("PRAGMA foreign_key_check"))
    if foreign:
        raise Stop(f"foreign_key_check failed with {len(foreign)} row(s)")


def role_definitions(row: sqlite3.Row) -> list[dict]:
    try:
        value = json.loads(row["role_definitions"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise Stop(f"invalid role_definitions JSON for category id={row['id']}") from exc
    if not isinstance(value, list):
        raise Stop(f"role_definitions is not a list for category id={row['id']}")
    return value


def category_dict(row: sqlite3.Row, *, snapshot: bool = False) -> dict:
    columns = SNAPSHOT_COLUMNS if snapshot else CATEGORY_COLUMNS
    result = {column: row[column] for column in columns}
    result["is_active"] = bool(result["is_active"])
    result["role_definitions"] = role_definitions(row)
    return result


def target_specs(first_sort: int) -> list[dict]:
    following_roles = [
        {"key": FOLLOWER_KEY, "name": "Follower", "min_count": 1, "max_count": 1, "role_sort_order": 0},
        {"key": LEADER_KEY, "name": "Leader", "min_count": 1, "max_count": 1, "role_sort_order": 1},
    ]
    raw = [
        ("Following", "社交行为", "#C34B8F", "role_based", 2, 2, following_roles),
        ("Group locomotion", "群体行为", "#058ACC", "unordered", 3, None, []),
        ("Social clustering", "群体行为", "#817931", "unordered", 3, None, []),
        ("Dispersal", "群体行为", "#FF2605", "unordered", 3, None, []),
    ]
    return [
        {"project_id": PROJECT_ID, "name": name, "group": group, "color": color,
         "sort_order": first_sort + index, "is_active": True,
         "mouse_count_min": minimum, "mouse_count_max": maximum,
         "participant_mode": mode, "role_definitions": roles}
        for index, (name, group, color, mode, minimum, maximum, roles) in enumerate(raw)
    ]


def table_fingerprint(connection: sqlite3.Connection, table: str) -> dict:
    columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
    if not columns:
        raise Stop(f"required table missing: {table}")
    quoted = ",".join(f'"{column}"' for column in columns)
    rows = [list(row) for row in connection.execute(f"SELECT {quoted} FROM {table} ORDER BY rowid")]
    return {"count": len(rows), "sha256": sha({"columns": columns, "rows": rows})}


def snapshot(project: sqlite3.Row, categories: list[sqlite3.Row], version: int | None = None) -> dict:
    return {
        "project_id": PROJECT_ID,
        "category_scheme_version": project["category_scheme_version"] if version is None else version,
        "category_scheme_locked_at": iso_datetime(project["category_scheme_locked_at"]),
        "category_scheme_locked_by": project["category_scheme_locked_by"],
        "categories": [category_dict(row, snapshot=True) for row in categories],
    }


def inspect(connection: sqlite3.Connection, triggers: dict[str, str], *, require_present: bool = False) -> dict:
    integrity(connection)
    versions = list(connection.execute("SELECT version_num FROM alembic_version"))
    if len(versions) != 1 or versions[0][0] != SCHEMA:
        raise Stop(f"expected exactly one alembic version {SCHEMA}")
    projects = list(connection.execute("SELECT * FROM projects WHERE id=?", (PROJECT_ID,)))
    if len(projects) != 1:
        raise Stop("project 2 must exist exactly once")
    project = projects[0]
    if project["category_scheme_locked_at"] is None or project["category_scheme_locked_by"] is None:
        raise Stop("project 2 category scheme must be locked")
    owners = list(connection.execute(
        "SELECT m.user_id FROM project_memberships m JOIN users u ON u.id=m.user_id "
        "WHERE m.project_id=? AND m.role='owner' AND m.status='active'", (PROJECT_ID,)
    ))
    if len(owners) != 1:
        raise Stop("project 2 must have exactly one active owner with an existing user")
    owner = owners[0][0]
    normalized_triggers = check_triggers(connection, triggers)
    categories = list(connection.execute(
        "SELECT * FROM behavior_categories WHERE project_id=? ORDER BY sort_order,id", (PROJECT_ID,)
    ))
    folded = [row["name"].casefold() for row in categories]
    if len(folded) != len(set(folded)):
        raise Stop("project 2 category names are not casefold-unique")
    target_folded = {name.casefold() for name in TARGET_NAMES}
    old = [row for row in categories if row["name"].casefold() not in target_folded]
    sorts = [row["sort_order"] for row in old]
    if sorts != list(range(len(old))):
        raise Stop(f"existing non-target sort_order must be exactly 0..N-1: {sorts}")
    specs = target_specs(len(old))
    present = [row for row in categories if row["name"].casefold() in target_folded]
    role_keys: list[str] = []
    for row in categories:
        for role in role_definitions(row):
            key = role.get("key") if isinstance(role, dict) else None
            if not isinstance(key, str):
                raise Stop(f"invalid role key in category id={row['id']}")
            role_keys.append(key)
    if len(role_keys) != len(set(role_keys)):
        raise Stop("role keys conflict within project 2")
    if not present:
        state = "absent"
    elif len(present) == 4:
        actual_by_name = {row["name"]: category_dict(row, snapshot=True) for row in present}
        created_values = []
        for spec in specs:
            actual = actual_by_name.get(spec["name"])
            expected = dict(spec)
            if actual is None or any(actual[key] != value for key, value in expected.items()):
                raise Stop(f"target category differs from fixed specification: {spec['name']}")
            row = next(row for row in present if row["name"] == spec["name"])
            if row["created_at"] is None:
                raise Stop(f"target category has no explicit created_at: {spec['name']}")
            created_values.append(iso_datetime(row["created_at"]))
        if len(set(created_values)) != 1:
            raise Stop("target categories must share one atomic UTC created_at")
        state = "present"
    else:
        raise Stop(f"target categories are partially present: {len(present)}/4")
    if require_present and state != "present":
        raise Stop("verification requires all four fixed categories")
    fp_payload = {
        "schema": SCHEMA, "release": RELEASE,
        "project": {"id": PROJECT_ID, "version": project["category_scheme_version"],
                    "locked_at": iso_datetime(project["category_scheme_locked_at"]),
                    "locked_by": project["category_scheme_locked_by"], "active_owner": owner},
        "categories": [category_dict(row) for row in categories],
        "triggers": normalized_triggers,
    }
    return {"state": state, "fingerprint": sha(fp_payload), "project": project,
            "owner": owner, "categories": categories, "old": old, "specs": specs,
            "snapshot": snapshot(project, categories), "trigger_snapshot": normalized_triggers}


def verify_audit(connection: sqlite3.Connection, info: dict) -> None:
    rows = list(connection.execute(
        "SELECT * FROM category_scheme_audits WHERE project_id=? AND action='replace' "
        "ORDER BY created_at DESC,id DESC LIMIT 1", (PROJECT_ID,)
    ))
    if len(rows) != 1:
        raise Stop("latest replace audit is missing")
    audit = rows[0]
    current = info["snapshot"]
    try:
        after = json.loads(audit["after_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise Stop("latest replace audit after_json is invalid") from exc
    if audit["scheme_version"] != current["category_scheme_version"]:
        raise Stop("latest replace audit version differs from current project")
    if after != current or audit["scheme_hash"] != sha(current):
        raise Stop("latest replace audit after/hash differs from current snapshot")


def dry_run(db: Path, backend: Path, *, require_present: bool = False) -> dict:
    triggers = load_triggers(backend)
    with closing(connect_ro(db)) as connection:
        info = inspect(connection, triggers, require_present=require_present)
        if require_present:
            verify_audit(connection, info)
        return info


def make_backup(db: Path, destination: Path, backend: Path) -> None:
    dry_run(db, backend)
    if destination.exists():
        raise Stop(f"backup target already exists: {destination}")
    if not destination.parent.is_dir():
        raise Stop(f"backup parent does not exist: {destination.parent}")
    source = connect_ro(db)
    target = None
    try:
        target = sqlite3.connect(destination)
        source.backup(target)
        target.close()
        target = None
        with closing(connect_ro(destination)) as backup_connection:
            integrity(backup_connection)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        emit("backup-created", path=str(destination), sha256=digest)
    except Exception:
        if target is not None:
            target.close()
        if destination.exists():
            destination.unlink()
        raise
    finally:
        source.close()


def verify_backup_evidence(db: Path, backup: Path, expected_sha256: str,
                           backend: Path, expected_fingerprint: str) -> None:
    if not backup.is_file() or backup.samefile(db):
        raise Stop(f"verified backup must be a separate regular file: {backup}")
    actual_sha256 = hashlib.sha256(backup.read_bytes()).hexdigest()
    if expected_sha256.lower() != actual_sha256:
        raise Stop("verified backup SHA256 does not match --confirm-backup-sha256")
    backup_info = dry_run(backup, backend)
    if backup_info["fingerprint"] != expected_fingerprint:
        raise Stop("verified backup fingerprint does not match --expect-fingerprint")


def apply_update(db: Path, backend: Path, expected_fingerprint: str,
                 backup: Path, backup_sha256: str) -> str:
    triggers = load_triggers(backend)
    baseline = dry_run(db, backend)
    if baseline["fingerprint"] != expected_fingerprint:
        raise Stop("pre-transaction fingerprint does not match --expect-fingerprint")
    verify_backup_evidence(db, backup, backup_sha256, backend, expected_fingerprint)
    baseline_triggers = baseline["trigger_snapshot"]
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    committed = False
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        current = inspect(connection, triggers)
        if current["fingerprint"] != expected_fingerprint:
            raise Stop("transaction fingerprint changed since dry-run")
        if current["state"] == "present":
            verify_audit(connection, current)
            connection.commit()
            committed = True
            dry_run(db, backend, require_present=True)
            return "no-op"
        old_rows = [category_dict(row) for row in current["old"]]
        protected_before = {
            table: table_fingerprint(connection, table)
            for table in ("annotations", "submission_annotations")
        }
        before = current["snapshot"]
        locked_at = current["project"]["category_scheme_locked_at"]
        locked_by = current["project"]["category_scheme_locked_by"]
        old_version = current["project"]["category_scheme_version"]
        for name in TEMPORARILY_DROPPED:
            connection.execute(f'DROP TRIGGER "{name}"')
        created_at = utc_now()
        inserted = 0
        for spec in current["specs"]:
            cursor = connection.execute(
                "INSERT INTO behavior_categories "
                "(project_id,name,\"group\",color,sort_order,is_active,mouse_count_min,mouse_count_max,"
                "participant_mode,role_definitions,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (spec["project_id"], spec["name"], spec["group"], spec["color"], spec["sort_order"],
                 1, spec["mouse_count_min"], spec["mouse_count_max"], spec["participant_mode"],
                 canonical(spec["role_definitions"]), created_at),
            )
            inserted += cursor.rowcount
        version_cursor = connection.execute(
            "UPDATE projects SET category_scheme_version=category_scheme_version+1 WHERE id=? "
            "AND category_scheme_version=? AND category_scheme_locked_at IS ? AND category_scheme_locked_by=?",
            (PROJECT_ID, old_version, locked_at, locked_by),
        )
        for name in TEMPORARILY_DROPPED:
            connection.execute(expected_trigger_sql(name, triggers[name]))
        if check_triggers(connection, triggers) != baseline_triggers:
            raise Stop("trigger snapshot changed while restoring temporary triggers")
        project_after = connection.execute("SELECT * FROM projects WHERE id=?", (PROJECT_ID,)).fetchone()
        categories_after = list(connection.execute(
            "SELECT * FROM behavior_categories WHERE project_id=? ORDER BY sort_order,id", (PROJECT_ID,)
        ))
        after = snapshot(project_after, categories_after)
        audit_cursor = connection.execute(
            "INSERT INTO category_scheme_audits "
            "(project_id,actor_id,action,scheme_version,before_json,after_json,scheme_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (PROJECT_ID, current["owner"], "replace", old_version + 1, canonical(before),
             canonical(after), sha(after), created_at),
        )
        if (inserted, version_cursor.rowcount, audit_cursor.rowcount) != (4, 1, 1):
            raise Stop("unexpected rowcount for categories/version/audit")
        if project_after["category_scheme_version"] != old_version + 1:
            raise Stop("scheme version did not increase exactly once")
        if (project_after["category_scheme_locked_at"], project_after["category_scheme_locked_by"]) != (locked_at, locked_by):
            raise Stop("project lock fields changed")
        if [category_dict(row) for row in categories_after[:len(old_rows)]] != old_rows:
            raise Stop("an existing category changed")
        if any(table_fingerprint(connection, table) != value for table, value in protected_before.items()):
            raise Stop("annotation content changed")
        final = inspect(connection, triggers, require_present=True)
        verify_audit(connection, final)
        connection.commit()
        committed = True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
        if not committed:
            try:
                with closing(connect_ro(db)) as recovery:
                    if check_triggers(recovery, triggers) != baseline_triggers:
                        raise Stop("full trigger snapshot differs from pre-transaction baseline")
            except Exception as recovery_error:
                emit("KEEP_SERVICE_STOPPED", reason=f"rollback trigger verification failed: {recovery_error}")
    post = dry_run(db, backend, require_present=True)
    return post["fingerprint"]


def create_fixture(root: Path) -> tuple[Path, Path]:
    backend = root / "release" / "backend"
    app = backend / "app"
    app.mkdir(parents=True)
    authority_trigger_bodies = {
        "trg_project_scheme_lock": "BEFORE UPDATE OF category_scheme_version,category_scheme_locked_at,category_scheme_locked_by ON projects BEGIN SELECT RAISE(ABORT,'category scheme lock is permanent') WHERE OLD.category_scheme_locked_at IS NOT NULL AND (NEW.category_scheme_version IS NOT OLD.category_scheme_version OR NEW.category_scheme_locked_at IS NOT OLD.category_scheme_locked_at OR NEW.category_scheme_locked_by IS NOT OLD.category_scheme_locked_by); END",
        "trg_category_locked_insert": "BEFORE INSERT ON behavior_categories BEGIN SELECT RAISE(ABORT,'category scheme is locked') WHERE EXISTS(SELECT 1 FROM projects p WHERE p.id=NEW.project_id AND p.category_scheme_locked_at IS NOT NULL); END",
    }
    assignee_trigger_bodies = {
        "trg_annotation_assignee_insert": "AFTER INSERT ON annotations BEGIN SELECT NEW.id; END",
    }
    (app / "authority_triggers.py").write_text("TRIGGERS = " + repr(authority_trigger_bodies), encoding="utf-8")
    (app / "assignee_triggers.py").write_text("TRIGGERS = " + repr(assignee_trigger_bodies), encoding="utf-8")
    db = root / "fixture.db"
    connection = sqlite3.connect(db)
    fixture_statements = (
        "PRAGMA foreign_keys=ON",
        "CREATE TABLE alembic_version(version_num TEXT NOT NULL)",
        "INSERT INTO alembic_version VALUES('0016')",
        "CREATE TABLE users(id INTEGER PRIMARY KEY)",
        "INSERT INTO users VALUES(1)",
        "CREATE TABLE projects(id INTEGER PRIMARY KEY, category_scheme_version INTEGER NOT NULL, "
        "category_scheme_locked_at TEXT, category_scheme_locked_by INTEGER REFERENCES users(id))",
        "INSERT INTO projects VALUES(2,1,'2026-09-01 00:00:00',1)",
        "CREATE TABLE project_memberships(id INTEGER PRIMARY KEY,project_id INTEGER,user_id INTEGER "
        "REFERENCES users(id),role TEXT,status TEXT)",
        "INSERT INTO project_memberships VALUES(1,2,1,'owner','active')",
        "CREATE TABLE behavior_categories(id INTEGER PRIMARY KEY,project_id INTEGER REFERENCES projects(id),"
        "name TEXT,\"group\" TEXT,color TEXT,sort_order INTEGER,is_active INTEGER,mouse_count_min INTEGER,"
        "mouse_count_max INTEGER,participant_mode TEXT,role_definitions TEXT,created_at TEXT)",
        "INSERT INTO behavior_categories VALUES(1,2,'Existing','个体行为','#000000',0,1,1,1,"
        "'unordered','[]','2026-09-01 00:00:00')",
        "CREATE TABLE annotations(id INTEGER PRIMARY KEY,note TEXT)",
        "CREATE TABLE submission_annotations(id INTEGER PRIMARY KEY,note TEXT)",
        "CREATE TABLE category_scheme_audits(id INTEGER PRIMARY KEY,project_id INTEGER REFERENCES "
        "projects(id),actor_id INTEGER REFERENCES users(id),action TEXT,scheme_version INTEGER,"
        "before_json TEXT,after_json TEXT,scheme_hash TEXT,created_at TEXT)",
    )
    for statement in fixture_statements:
        connection.execute(statement)
    for name, body in (authority_trigger_bodies | assignee_trigger_bodies).items():
        connection.execute(expected_trigger_sql(name, body).replace("CREATE TRIGGER ",
                                                                     "CREATE TRIGGER IF NOT EXISTS ", 1))
    connection.commit()
    connection.close()
    return db, backend


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="project2-categories-") as temporary:
        db, backend = create_fixture(Path(temporary))
        first = dry_run(db, backend)
        assert first["state"] == "absent"
        rejected = Path(temporary) / "rejected.db"
        with closing(connect_ro(db)) as source, closing(sqlite3.connect(rejected)) as target:
            source.backup(target)
        with closing(sqlite3.connect(rejected)) as connection:
            connection.execute("DROP TRIGGER trg_category_locked_insert")
        try:
            dry_run(rejected, backend)
            raise AssertionError("missing release trigger was accepted")
        except Stop as exc:
            assert "trigger name set mismatch" in str(exc)
        drifted = Path(temporary) / "drifted.db"
        with closing(connect_ro(db)) as source, closing(sqlite3.connect(drifted)) as target:
            source.backup(target)
        with closing(sqlite3.connect(drifted)) as connection:
            connection.execute("DROP TRIGGER trg_annotation_assignee_insert")
            connection.execute("CREATE TRIGGER trg_annotation_assignee_insert AFTER INSERT ON annotations BEGIN SELECT NEW.note; END")
        try:
            dry_run(drifted, backend)
            raise AssertionError("drifted assignment trigger was accepted")
        except Stop as exc:
            assert "trigger definition mismatch" in str(exc)
        unknown = Path(temporary) / "unknown.db"
        with closing(connect_ro(db)) as source, closing(sqlite3.connect(unknown)) as target:
            source.backup(target)
        with closing(sqlite3.connect(unknown)) as connection:
            connection.execute("CREATE TRIGGER trg_unknown AFTER INSERT ON annotations BEGIN SELECT NEW.id; END")
        try:
            dry_run(unknown, backend)
            raise AssertionError("unknown trigger was accepted")
        except Stop as exc:
            assert "trigger name set mismatch" in str(exc) and "trg_unknown" in str(exc)
        partial = Path(temporary) / "partial.db"
        with closing(connect_ro(db)) as source, closing(sqlite3.connect(partial)) as target:
            source.backup(target)
        with closing(sqlite3.connect(partial)) as connection:
            connection.execute("DROP TRIGGER trg_category_locked_insert")
            spec = target_specs(1)[0]
            connection.execute(
                "INSERT INTO behavior_categories VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?)",
                (2, spec["name"], spec["group"], spec["color"], spec["sort_order"], 1, 2, 2,
                 spec["participant_mode"], canonical(spec["role_definitions"]), utc_now()),
            )
            triggers = load_triggers(backend)
            connection.execute(expected_trigger_sql("trg_category_locked_insert", triggers["trg_category_locked_insert"]))
            connection.commit()
        try:
            dry_run(partial, backend)
            raise AssertionError("partial target set was accepted")
        except Stop as exc:
            assert "partially present" in str(exc)
        backup = Path(temporary) / "verified-backup.db"
        with closing(connect_ro(db)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        backup_sha256 = hashlib.sha256(backup.read_bytes()).hexdigest()
        try:
            apply_update(db, backend, first["fingerprint"], backup, "0" * 64)
            raise AssertionError("incorrect backup evidence was accepted")
        except Stop as exc:
            assert "backup SHA256" in str(exc)
        result = apply_update(db, backend, first["fingerprint"], backup, backup_sha256)
        assert len(result) == 64
        verified = dry_run(db, backend, require_present=True)
        assert verified["state"] == "present"
        assert verified["trigger_snapshot"] == first["trigger_snapshot"]
        with closing(connect_ro(db)) as connection:
            assert connection.execute("SELECT category_scheme_version FROM projects WHERE id=2").fetchone()[0] == 2
            assert connection.execute("SELECT count(*) FROM category_scheme_audits").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM behavior_categories WHERE project_id=2").fetchone()[0] == 5
        post_backup = Path(temporary) / "verified-post-backup.db"
        with closing(connect_ro(db)) as source, closing(sqlite3.connect(post_backup)) as target:
            source.backup(target)
        post_backup_sha256 = hashlib.sha256(post_backup.read_bytes()).hexdigest()
        assert apply_update(db, backend, verified["fingerprint"], post_backup,
                            post_backup_sha256) == "no-op"
        with closing(connect_ro(db)) as connection:
            assert connection.execute("SELECT category_scheme_version FROM projects WHERE id=2").fetchone()[0] == 2
            assert connection.execute("SELECT count(*) FROM category_scheme_audits").fetchone()[0] == 1
    emit("self-test-passed", cases=8)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=PRODUCTION_DB)
    parser.add_argument("--release-backend", type=Path, default=PRODUCTION_BACKEND)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--backup", type=Path)
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--verify", action="store_true")
    modes.add_argument("--self-test", action="store_true")
    parser.add_argument("--expect-fingerprint")
    parser.add_argument("--confirm-production-db")
    parser.add_argument("--confirm-release")
    parser.add_argument("--confirm-backup", type=Path)
    parser.add_argument("--confirm-backup-sha256")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.self_test:
            self_test()
            return 0
        if args.apply:
            if not args.expect_fingerprint or len(args.expect_fingerprint) != 64:
                raise Stop("--apply requires --expect-fingerprint <sha256>")
            if (args.confirm_backup is None or not args.confirm_backup_sha256 or
                    len(args.confirm_backup_sha256) != 64):
                raise Stop("--apply requires --confirm-backup <path> and --confirm-backup-sha256 <sha256>")
            if (args.db != PRODUCTION_DB or args.release_backend != PRODUCTION_BACKEND or
                    args.confirm_production_db != str(PRODUCTION_DB) or args.confirm_release != RELEASE):
                raise Stop("apply requires exact production paths plus --confirm-production-db and --confirm-release")
            result = apply_update(args.db, args.release_backend, args.expect_fingerprint,
                                  args.confirm_backup, args.confirm_backup_sha256)
            if result == "no-op":
                result = dry_run(args.db, args.release_backend, require_present=True)["fingerprint"]
                emit("no-op", fingerprint=result)
            else:
                emit("apply-complete", fingerprint=result)
        elif args.backup:
            make_backup(args.db, args.backup, args.release_backend)
        elif args.verify:
            info = dry_run(args.db, args.release_backend, require_present=True)
            emit("verify-passed", state=info["state"], fingerprint=info["fingerprint"])
        else:
            info = dry_run(args.db, args.release_backend)
            emit("dry-run", readonly=True, state=info["state"], fingerprint=info["fingerprint"])
        return 0
    except Exception as exc:
        status = "KEEP_SERVICE_STOPPED" if args.apply else "failed"
        emit(status, error=type(exc).__name__, reason=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
