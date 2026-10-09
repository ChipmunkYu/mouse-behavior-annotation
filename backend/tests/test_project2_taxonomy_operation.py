from __future__ import annotations

from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from app.migration import downgrade_to, inspect_state, upgrade_to
from app import database as db_mod
from app.behavior_review import approved_snapshots, locked_annotation_ids, rejected_blockers
from app.models import BehaviorCategory, Project, Submission
from app.category_scheme_service import scheme_snapshot as orm_scheme_snapshot


BACKEND = Path(__file__).parents[1]
SCRIPT = BACKEND.parent / "deploy" / "operations" / "update_project2_categories.py"
SPEC = importlib.util.spec_from_file_location("project2_taxonomy_operation", SCRIPT)
op = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(op)


def fixture(tmp_path):
    """Create the fixture through the real 0019 Alembic chain and real trigger modules."""
    db = tmp_path / "source.db"
    upgrade_to(f"sqlite:///{db.as_posix()}", "0019")
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    stamp = "2026-01-01 00:00:00"
    con.execute("INSERT INTO users(id,username,password_hash,created_at) VALUES(1,'owner','x',?)", (stamp,))
    con.execute("INSERT INTO projects(id,name,status,created_by,created_at,updated_at,invite_code,category_scheme_version) VALUES(2,'p2','active',1,?,?, 'p2',7)", (stamp, stamp))
    con.execute("INSERT INTO project_memberships(id,project_id,user_id,role,status,created_at,can_review) VALUES(1,2,1,'owner','active',?,1)", (stamp,))
    ids = [13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28]
    names = dict(zip(ids, (
        "Running", "Walking", "Static", "Together", "Approach", "Chasing",
        "Avoiding", "Attack", "Snout-head_contact", "Snout-rear_contact",
        "Huddling", "Isolation", "Following", "Group locomotion",
        "Social clustering", "Dispersal",
    )))
    for order, category_id in enumerate(ids):
        con.execute('INSERT INTO behavior_categories(id,project_id,name,"group",color,sort_order,is_active,created_at,mouse_count_min,mouse_count_max,participant_mode,role_definitions) VALUES(?,?,?,\'个体行为\',?,?,1,?,1,1,\'unordered\',\'[]\')',
                    (category_id, 2, names.get(category_id, f"C{category_id}"), f"#{category_id:06x}", order, stamp))
    con.execute("UPDATE projects SET category_scheme_locked_at=?,category_scheme_locked_by=1 WHERE id=2", (stamp,))
    con.row_factory = sqlite3.Row
    project = dict(con.execute("SELECT * FROM projects WHERE id=2").fetchone())
    categories = [dict(row) for row in con.execute("SELECT * FROM behavior_categories WHERE project_id=2 ORDER BY sort_order,id")]
    snapshot = op.scheme_snapshot(project, categories)
    con.execute("INSERT INTO category_scheme_audits(project_id,actor_id,action,scheme_version,before_json,after_json,scheme_hash,created_at) VALUES(2,1,'replace',7,?,?,?,?)",
                (op.canonical(snapshot), op.canonical(snapshot), op.sha(snapshot), stamp))
    con.execute("INSERT INTO videos(id,project_id,filename,status,uploaded_by,created_at,workflow_status,annotation_revision,submitted_at,approved_at,approved_by) VALUES(1,2,'v.mp4','ready',1,?,'approved',3,?,?,1)", (stamp, stamp, stamp))
    con.execute("INSERT INTO detection_imports(id,video_id,revision,schema_version,status,active,created_by,created_at) VALUES(1,1,1,'1','ready',1,1,?)", (stamp,))
    con.execute("INSERT INTO detection_snapshots(id,detection_import_id,source_edit_version,raw_detection_count,override_count,schema_version,fps,width,height,frame_count,keypoint_names,skeleton_edges,created_at,raw_digest,state_digest,metadata_digest) VALUES(1,1,0,0,0,'1',25,100,100,100,'[]','[]',?,'r','s','m')", (stamp,))
    for annotation_id, category_id in enumerate((13,14,19,25,15), 1):
        con.execute("INSERT INTO annotations(id,video_id,annotator_id,category_id,reviewer_id,start_time,end_time,start_frame,end_frame,confidence,review_status,created_at,updated_at,mouse_ids,mouse_id_status,detection_import_revision,identity_revision,participant_roles,participant_status,material_revision) VALUES(?,1,1,?,1,0,1,0,2,'certain','approved',?,?,'[1]','valid',1,1,'{}','valid',1)", (annotation_id, category_id, stamp, stamp))
    attempts = ((1, "approved"), (2, "rejected"), (3, "withdrawn"), (4, "rejected"))
    snapshot_id = 0
    for attempt_no, status in attempts:
        con.execute("INSERT INTO submissions(id,video_id,detection_snapshot_id,attempt_no,source_annotation_version,source_media_revision,source_video_filename,source_storage_key,source_video_sha256,status,submitted_by,submitted_at,decided_at,decision_revision) VALUES(?,1,1,?,3,1,'v.mp4','v.mp4',?, ?,1,?,?,10)",
                    (attempt_no, attempt_no, "a" * 64, status, stamp, stamp if status != "withdrawn" else None))
        source_ids = range(1, 6) if attempt_no == 1 else (attempt_no - 1,)
        for source_id in source_ids:
            snapshot_id += 1
            category_id = (13,14,19,25,15)[source_id - 1]
            con.execute("INSERT INTO submission_annotations(id,submission_id,source_annotation_id,category_id,category_name,start_time,end_time,start_frame,end_frame,confidence,mouse_ids,source_annotation_key,source_material_revision,material_digest) VALUES(?,?,?,?,?,0,1,0,2,'certain','[1]',?,1,'legacy')",
                        (snapshot_id, attempt_no, source_id, category_id, names.get(category_id, f"C{category_id}"), source_id))
            decision = "approved" if (attempt_no == 1 or attempt_no == 3) else "rejected"
            con.execute("INSERT INTO behavior_review_decisions(submission_annotation_id,status,feedback,sequence,reviewer_id,decided_at,origin) VALUES(?,?,?,10,1,?,'manual')",
                        (snapshot_id, decision, "old rejection" if decision == "rejected" else None, stamp))
    con.execute("INSERT INTO reviews(project_id,video_id,reviewer_id,result,annotation_revision,created_at,submission_id) VALUES(2,1,1,'approved',3,?,1)", (stamp,))
    for clip_id, annotation_id in enumerate((1, 3, 4), 1):
        con.execute("INSERT INTO clips(id,project_id,annotation_id,source_revision,media_revision,status,clip_path,thumbnail_path,created_at,updated_at) VALUES(?,2,?,3,1,'ready',?,?,?,?)",
                    (clip_id, annotation_id, f"clip-{clip_id}.mp4", f"clip-{clip_id}.jpg", stamp, stamp))
    con.commit(); con.close()
    return db


def make_backup(db, tmp_path, name="backup.db"):
    target = tmp_path / name
    with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(target)) as copied:
        source.backup(copied)
    return target, hashlib.sha256(target.read_bytes()).hexdigest()


def lifecycle_fixture(tmp_path):
    """Normal multi-video lifecycle matrix; fixture() remains the hostile legacy case."""
    db = fixture(tmp_path); stamp = "2026-01-01 00:00:00"
    with sqlite3.connect(db) as con:
        for video_id, workflow in ((2, "approved"), (3, "rejected"), (4, "submitted")):
            con.execute("INSERT INTO videos(id,project_id,filename,status,uploaded_by,created_at,workflow_status,annotation_revision,submitted_at,approved_at,approved_by) VALUES(?,2,?,'ready',1,?,?,1,?,?,?)",
                        (video_id, f"v{video_id}.mp4", stamp, workflow,
                         stamp if workflow != "draft" else None,
                         stamp if workflow == "approved" else None,
                         1 if workflow == "approved" else None))
        for sid, video_id, status in ((10,2,"approved"),(11,3,"rejected")):
            con.execute("INSERT INTO submissions(id,video_id,detection_snapshot_id,attempt_no,source_annotation_version,source_media_revision,source_video_filename,source_storage_key,source_video_sha256,status,submitted_by,submitted_at,decided_at,decision_revision) VALUES(?,?,1,1,1,1,?,?,?, ?,1,?,?,1)",
                        (sid, video_id, f"v{video_id}.mp4", f"v{video_id}.mp4", "b"*64, status,
                         stamp, stamp if status in ("approved","rejected") else None))
        con.execute("INSERT INTO submissions(id,video_id,detection_snapshot_id,attempt_no,source_annotation_version,source_media_revision,source_video_filename,source_storage_key,source_video_sha256,status,submitted_by,submitted_at,decision_revision) VALUES(13,4,1,1,1,1,'v4.mp4','v4.mp4',?,'submitted',1,?,1)",
                    ("d"*64, stamp))
        con.execute("INSERT INTO reviews(project_id,video_id,reviewer_id,result,annotation_revision,created_at,submission_id) VALUES(2,2,1,'approved',1,?,10)", (stamp,))
        con.execute("INSERT INTO submissions(id,video_id,detection_snapshot_id,attempt_no,source_annotation_version,source_media_revision,source_video_filename,source_storage_key,source_video_sha256,status,submitted_by,submitted_at,decided_at,decision_revision) VALUES(12,2,1,2,1,1,'v2.mp4','v2.mp4',?,'superseded',1,?,?,1)",
                    ("c"*64, stamp, stamp))
        con.execute("INSERT INTO reviews(project_id,video_id,reviewer_id,result,annotation_revision,created_at,submission_id) VALUES(2,2,1,'approved',1,?,12)", (stamp,))
    return db


def test_plan_backup_apply_verify_and_repeat_are_safe(tmp_path):
    db = fixture(tmp_path)
    with sqlite3.connect(db) as con:
        # IDs are global across projects. Another project's higher category ID
        # must not make Project 2's deterministic target IDs jump from 29/30.
        stamp = "2026-01-01 00:00:00"
        con.execute("INSERT INTO projects(id,name,status,created_by,created_at,updated_at,invite_code,category_scheme_version) VALUES(3,'other','active',1,?,?, 'other',0)", (stamp, stamp))
        con.execute("INSERT INTO behavior_categories(id,project_id,name,\"group\",color,sort_order,is_active,created_at,mouse_count_min,mouse_count_max,participant_mode,role_definitions) VALUES(100,3,'Other','个体行为','#ffffff',0,1,?,1,1,'unordered','[]')", (stamp,))
    before = db.read_bytes(); info = op.plan(db, BACKEND)
    assert db.read_bytes() == before and info["plan"]["annotation_counts"] == {13:1,14:1,19:1,25:1}
    assert info["plan"]["actions"]["add"] == [{"id": 101, "name": "Grooming"}, {"id": 102, "name": "Rearing"}]
    backup = tmp_path / "api-backup.db"; evidence = op.backup(db, backup, BACKEND)
    immutable_before = None
    with closing(op.connect_ro(db)) as con:
        immutable_before = ([tuple(r) for r in con.execute("SELECT * FROM submission_annotations")],
                            [tuple(r) for r in con.execute("SELECT * FROM reviews")],
                            [tuple(r) for r in con.execute("SELECT * FROM behavior_review_decisions")])
    result = op.apply_offline(db, BACKEND, info["fingerprint"], info["plan_hash"], backup, evidence["sha256"])
    verified = op.verify_applied(db, BACKEND, production_counts=False)
    assert result == verified["fingerprint"]
    with closing(op.connect_ro(db)) as con:
        categories = list(con.execute("SELECT id,name,is_active,sort_order FROM behavior_categories WHERE project_id=2 ORDER BY sort_order,id"))
        assert len(categories) == 18 and [r[3] for r in categories] == list(range(18))
        assert [(r[0], r[1]) for r in categories[-3:]] == [(14,"Walking"),(19,"Avoiding"),(25,"Following")]
        assert con.execute("SELECT id FROM behavior_categories WHERE name='Grooming'").fetchone()[0] == 101
        assert con.execute("SELECT id FROM behavior_categories WHERE name='Rearing'").fetchone()[0] == 102
        assert con.execute("SELECT count(*) FROM annotations WHERE category_id=13").fetchone()[0] == 2
        assert con.execute("SELECT count(*) FROM annotations WHERE category_id IN (14,19,25)").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM annotations WHERE review_status='pending' AND reviewer_id IS NULL AND material_revision=2 AND material_digest IS NOT NULL").fetchone()[0] == 3
        clips = list(con.execute("SELECT annotation_id,clip_path,thumbnail_path,retired_at,retired_reason FROM clips ORDER BY id"))
        assert [tuple(row[:3]) for row in clips] == [(1,"clip-1.mp4","clip-1.jpg"),(None,"clip-2.mp4","clip-2.jpg"),(None,"clip-3.mp4","clip-3.jpg")]
        assert all(row[3] is not None and row[4] == op.RETIRE_REASON for row in clips)
        assert con.execute("SELECT workflow_status FROM videos").fetchone()[0] == "rejected"
        assert con.execute("SELECT status FROM submissions WHERE id=1").fetchone()[0] == "superseded"
        latest = list(con.execute("SELECT d.status,d.feedback,d.reviewer_id,d.carried_from_decision_id FROM behavior_review_decisions d JOIN (SELECT submission_annotation_id,max(sequence) sequence FROM behavior_review_decisions GROUP BY submission_annotation_id) x ON x.submission_annotation_id=d.submission_annotation_id AND x.sequence=d.sequence"))
        assert latest and all(tuple(r) == ("pending", None, None, None) for r in latest)
        # Deleted live annotations intentionally NULL source_annotation_id via real FK SET NULL.
        assert op.immutable_snapshot_hash(con) == op.immutable_snapshot_hash(sqlite3.connect(backup))
        assert [tuple(r) for r in con.execute("SELECT * FROM reviews")] == immutable_before[1]
        assert [tuple(r) for r in con.execute("SELECT * FROM behavior_review_decisions WHERE id<=8")] == immutable_before[2]
        audit = con.execute("SELECT after_json,scheme_hash FROM category_scheme_audits ORDER BY id DESC LIMIT 1").fetchone()
        assert audit[1] == op.sha(json.loads(audit[0]))
    post_backup, post_sha = make_backup(db, tmp_path, "post.db")
    assert op.apply_offline(db, BACKEND, verified["fingerprint"], verified["plan_hash"], post_backup, post_sha) == "no-op"
    db_mod.configure_engine(f"sqlite:///{db.as_posix()}")
    with db_mod.SessionLocal() as session:
        assert locked_annotation_ids(session, 1) == []
        assert rejected_blockers(session, 1, 1) == []
        assert all(approved_snapshots(session, row) == [] for row in session.query(Submission).all())


def test_normal_multi_video_lifecycles_reset_and_reopen(tmp_path):
    db = lifecycle_fixture(tmp_path); info = op.plan(db, BACKEND)
    backup, digest = make_backup(db, tmp_path)
    op.apply_offline(db, BACKEND, info["fingerprint"], info["plan_hash"], backup, digest)
    op.verify_applied(db, BACKEND, production_counts=False)
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT status FROM submissions WHERE id=10").fetchone()[0] == "superseded"
        assert con.execute("SELECT count(*) FROM behavior_review_reopens WHERE submission_id=10").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM behavior_review_reopens WHERE submission_id=12").fetchone()[0] == 1
        assert con.execute("SELECT status FROM submissions WHERE id=13").fetchone()[0] == "withdrawn"
        assert con.execute("SELECT count(*) FROM videos WHERE project_id=2 AND workflow_status='draft'").fetchone()[0] == 3
        assert con.execute("SELECT workflow_status FROM videos WHERE id=1").fetchone()[0] == "rejected"


def test_exact_baseline_rejected_and_transaction_rollback(tmp_path):
    db = fixture(tmp_path)
    with sqlite3.connect(db) as con:
        con.execute("DROP TRIGGER trg_category_locked_update")
        con.execute("UPDATE behavior_categories SET name='Wrong' WHERE id=13")
        con.execute(f"CREATE TRIGGER trg_category_locked_update {op.load_triggers(BACKEND)['trg_category_locked_update']}")
    with pytest.raises(op.Stop, match="neither the exact initial"):
        op.plan(db, BACKEND)
    with sqlite3.connect(db) as con:
        con.execute("DROP TRIGGER trg_category_locked_update")
        con.execute("UPDATE behavior_categories SET name='Running' WHERE id=13")
        con.execute(f"CREATE TRIGGER trg_category_locked_update {op.load_triggers(BACKEND)['trg_category_locked_update']}")
    info = op.plan(db, BACKEND); backup, digest = make_backup(db, tmp_path); before_fp = info["fingerprint"]
    with pytest.raises(op.Stop, match="backup SHA256"): op.apply_offline(db, BACKEND, info["fingerprint"], info["plan_hash"], backup, "0"*64)
    with pytest.raises(RuntimeError, match="fault"):
        op.apply_offline(db, BACKEND, info["fingerprint"], info["plan_hash"], backup, digest,
                         fault=lambda: (_ for _ in ()).throw(RuntimeError("fault")))
    assert op.plan(db, BACKEND)["fingerprint"] == before_fp


def test_0019_adds_clip_retirement_fields(tmp_path):
    db = tmp_path / "migration.db"
    upgrade_to(f"sqlite:///{db.as_posix()}", "0018")
    with sqlite3.connect(db) as con:
        assert "retired_at" not in {row[1] for row in con.execute("PRAGMA table_info(clips)")}
    upgrade_to(f"sqlite:///{db.as_posix()}", "0019")
    with sqlite3.connect(db) as con:
        assert {"retired_at", "retired_reason"} <= {row[1] for row in con.execute("PRAGMA table_info(clips)")}
    assert inspect_state(f"sqlite:///{db.as_posix()}") == "versioned"


def test_plan_and_backup_on_0018_remain_valid_after_0019_upgrade(tmp_path):
    db = fixture(tmp_path); url = f"sqlite:///{db.as_posix()}"
    downgrade_to(url, "0018")
    info = op.plan(db, BACKEND)
    evidence = op.backup(db, tmp_path / "pre-0019.db", BACKEND)
    upgrade_to(url, "0019")
    op.apply_offline(db, BACKEND, info["fingerprint"], info["plan_hash"],
                     tmp_path / "pre-0019.db", evidence["sha256"])
    op.verify_applied(db, BACKEND, production_counts=False)
