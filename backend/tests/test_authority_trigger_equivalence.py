import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.assignee_triggers import install_sqlite_assignee_triggers
from app.authority_triggers import TRIGGERS, install_sqlite_authority_triggers
from app.database import Base
from app.migration import run_migrations


def _trigger_names(engine):
    with engine.connect() as conn:
        return set(conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )).scalars())


def _reject(conn, sql):
    with pytest.raises(DBAPIError):
        conn.execute(text(sql))
    conn.rollback()


def _seed_matrix(conn):
    conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    for sql in (
        "INSERT INTO users(id,username,password_hash,created_at) VALUES "
        "(1,'owner','x',CURRENT_TIMESTAMP),(2,'outsider','x',CURRENT_TIMESTAMP)",
        "INSERT INTO projects(id,name,status,created_by,created_at,updated_at,invite_code) VALUES "
        "(1,'locked','active',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'p1'),"
        "(2,'unlocked','active',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'p2')",
        "INSERT INTO project_memberships(id,project_id,user_id,role,can_review,status,created_at) VALUES "
        "(1,1,1,'owner',0,'active',CURRENT_TIMESTAMP),(2,2,1,'owner',0,'active',CURRENT_TIMESTAMP)",
        "INSERT INTO behavior_categories(id,project_id,name,\"group\",sort_order,is_active,mouse_count_min,"
        "participant_mode,role_definitions,created_at) VALUES "
        "(1,1,'locked category','test',0,1,1,'unordered','[]',CURRENT_TIMESTAMP),"
        "(2,2,'unlocked category','test',0,1,1,'unordered','[]',CURRENT_TIMESTAMP)",
        "INSERT INTO videos(id,project_id,filename,status,workflow_status,annotation_revision,"
        "detection_import_revision,identity_revision,media_revision,created_at) VALUES "
        "(1,1,'locked.mp4','metadata','draft',1,0,0,1,CURRENT_TIMESTAMP),"
        "(2,2,'unlocked.mp4','metadata','draft',1,0,0,1,CURRENT_TIMESTAMP)",
        "INSERT INTO detection_imports(id,video_id,revision,schema_version,status,active,created_by,created_at,"
        "edit_version,next_display_track_id) VALUES (1,1,1,'1.0','imported',1,1,CURRENT_TIMESTAMP,0,0)",
        "INSERT INTO detection_snapshots(id,detection_import_id,source_edit_version,raw_detection_count,"
        "override_count,schema_version,fps,width,height,frame_count,keypoint_names,skeleton_edges,created_at,"
        "raw_digest,state_digest,metadata_digest) VALUES "
        "(1,1,0,0,0,1,25,640,480,100,'[]','[]',CURRENT_TIMESTAMP,'" + "0" * 64 + "','" +
        "0" * 64 + "','" + "0" * 64 + "')",
        "INSERT INTO submissions(id,video_id,detection_snapshot_id,attempt_no,source_annotation_version,"
        "source_media_revision,source_video_filename,source_storage_key,source_video_sha256,status,submitted_by,"
        "submitted_at,legacy_backfill,source_file_size,source_mtime_ns,source_device,source_inode) VALUES "
        "(1,1,1,1,1,1,'locked.mp4','locked.mp4','" + "0" * 64 +
        "','submitted',1,CURRENT_TIMESTAMP,0,1,1,1,1)",
        # SubmissionAnnotation INSERT is intentionally valid; only subsequent mutation is forbidden.
        "INSERT INTO submission_annotations(id,submission_id,category_id,category_name,category_group,"
        "category_participant_mode,role_definitions_snapshot,participant_roles_snapshot,start_time,end_time,"
        "start_frame,end_frame,confidence,mouse_ids) VALUES "
        "(1,1,1,'locked category','test','unordered','[]','{}',0,1,0,25,'certain','[]')",
        "UPDATE projects SET category_scheme_version=1,category_scheme_locked_at=CURRENT_TIMESTAMP,"
        "category_scheme_locked_by=1 WHERE id=1",
        "INSERT INTO category_scheme_audits(id,project_id,actor_id,action,scheme_version,after_json,scheme_hash,"
        "created_at) VALUES (1,1,1,'lock',1,'{}','" + "0" * 64 + "',CURRENT_TIMESTAMP)",
    ):
        conn.execute(text(sql))


def _exercise_matrix(engine):
    with engine.begin() as conn:
        _seed_matrix(conn)
    with engine.connect() as conn:
        for sql in (
            "INSERT INTO behavior_categories(project_id,name,\"group\",sort_order,is_active,mouse_count_min,"
            "participant_mode,role_definitions,created_at) VALUES "
            "(1,'blocked','test',1,1,1,'unordered','[]',CURRENT_TIMESTAMP)",
            "UPDATE behavior_categories SET name='blocked' WHERE id=1",
            "DELETE FROM behavior_categories WHERE id=1",
            "UPDATE projects SET category_scheme_locked_at=NULL,category_scheme_locked_by=NULL WHERE id=1",
            "UPDATE projects SET category_scheme_version=2 WHERE id=1",
            "UPDATE projects SET category_scheme_locked_at='2000-01-01',category_scheme_locked_by=2 WHERE id=1",
            "UPDATE projects SET category_scheme_locked_at=CURRENT_TIMESTAMP,category_scheme_locked_by=2 WHERE id=2",
            "INSERT INTO annotations(video_id,annotator_id,category_id,start_time,end_time,start_frame,end_frame,"
            "confidence,review_status,mouse_ids,mouse_id_status,detection_import_revision,identity_revision,"
            "participant_roles,participant_status,created_at,updated_at) VALUES "
            "(2,1,2,0,1,0,1,'certain','pending','[]','needs_mouse_ids',0,0,'{}','valid',"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            "INSERT INTO annotations(video_id,annotator_id,category_id,start_time,end_time,start_frame,end_frame,"
            "confidence,review_status,mouse_ids,mouse_id_status,detection_import_revision,identity_revision,"
            "participant_roles,participant_status,created_at,updated_at) VALUES "
            "(1,1,2,0,1,0,1,'certain','pending','[]','needs_mouse_ids',0,0,'{}','valid',"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            "UPDATE category_scheme_audits SET scheme_hash='bad' WHERE id=1",
            "DELETE FROM category_scheme_audits WHERE id=1",
            "UPDATE submission_annotations SET category_name='bad' WHERE id=1",
            "UPDATE submission_annotations SET category_group='bad' WHERE id=1",
            "DELETE FROM submission_annotations WHERE id=1",
        ):
            _reject(conn, sql)

        # Locked same-project Annotation writes remain normal business writes.
        conn.execute(text(
            "INSERT INTO annotations(id,video_id,annotator_id,category_id,start_time,end_time,start_frame,"
            "end_frame,confidence,review_status,mouse_ids,mouse_id_status,detection_import_revision,"
            "identity_revision,participant_roles,participant_status,created_at,updated_at) VALUES "
            "(10,1,1,1,0,1,0,1,'certain','pending','[]','needs_mouse_ids',0,0,'{}','valid',"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        ))
        conn.commit()
        conn.execute(text("UPDATE annotations SET end_time=2,end_frame=2 WHERE id=10"))
        conn.commit()
        _reject(conn, "UPDATE annotations SET category_id=2 WHERE id=10")
        conn.execute(text("DELETE FROM annotations WHERE id=10"))
        conn.commit()
        assert conn.execute(text("SELECT count(*) FROM annotations WHERE id=10")).scalar_one() == 0


def test_fresh_alembic_and_create_all_trigger_sets_and_behavior_are_equivalent(tmp_path):
    alembic_url = f"sqlite:///{(tmp_path / 'alembic.db').as_posix()}"
    run_migrations(alembic_url)
    alembic_engine = create_engine(alembic_url)

    create_all_engine = create_engine(f"sqlite:///{(tmp_path / 'create-all.db').as_posix()}")
    Base.metadata.create_all(create_all_engine)
    with create_all_engine.begin() as conn:
        install_sqlite_authority_triggers(conn)
        install_sqlite_assignee_triggers(conn)

    alembic_names = _trigger_names(alembic_engine)
    create_all_names = _trigger_names(create_all_engine)
    assert alembic_names == create_all_names
    assert set(TRIGGERS) <= alembic_names
    _exercise_matrix(alembic_engine)
    _exercise_matrix(create_all_engine)
    alembic_engine.dispose()
    create_all_engine.dispose()
