from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3

import pytest

from app.migration import upgrade_to
from tests.test_project2_taxonomy_operation import BACKEND, fixture, make_backup, op

SCRIPT = Path(__file__).parents[2] / "deploy" / "operations" / "reject_project2_retired_clip_videos.py"
SPEC = importlib.util.spec_from_file_location("project2_video_compensation", SCRIPT)
comp = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(comp)


def migrated_db(tmp_path, monkeypatch):
    db = fixture(tmp_path); info = op.plan(db, BACKEND); backup, digest = make_backup(db, tmp_path)
    op.apply_offline(db, BACKEND, info["fingerprint"], info["plan_hash"], backup, digest)
    upgrade_to(f"sqlite:///{db.as_posix()}", "0020")
    with sqlite3.connect(db) as con:
        con.execute("UPDATE videos SET workflow_status='draft' WHERE id=1")
    monkeypatch.setattr(comp, "EXPECTED_CLIPS", 3)
    monkeypatch.setattr(comp, "TARGET_ANNOTATIONS", 3)
    monkeypatch.setattr(comp, "TARGET_MOVING", 2)
    return db


def test_plan_apply_and_repeat_are_scoped_and_safe(tmp_path, monkeypatch):
    db = migrated_db(tmp_path, monkeypatch)
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO projects(id,name,status,created_by,created_at,updated_at,invite_code,category_scheme_version) VALUES(3,'other','active',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'other2',0)")
        con.execute("INSERT INTO videos(id,project_id,filename,status,uploaded_by,created_at,workflow_status,annotation_revision) VALUES(99,3,'other.mp4','ready',1,CURRENT_TIMESTAMP,'draft',1)")
    before = db.read_bytes(); planned = comp.plan(db)
    assert db.read_bytes() == before and planned["draft"] == 1 and planned["retired_clips"] == 3
    applied = comp.apply(db)
    assert applied["draft"] == 0 and applied["rejected"] == 1
    assert comp.apply(db)["rejected"] == 1
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT workflow_status FROM videos WHERE id=1").fetchone()[0] == "rejected"
        assert con.execute("SELECT workflow_status FROM videos WHERE id=99").fetchone()[0] == "draft"


def test_rejects_abnormal_target_state(tmp_path, monkeypatch):
    db = migrated_db(tmp_path, monkeypatch)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE videos SET workflow_status='approved' WHERE id=1")
    with pytest.raises(comp.Stop, match="draft or rejected"):
        comp.plan(db)
