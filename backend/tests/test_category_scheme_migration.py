import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from app import database as db_mod
from app.migration import current_revision, run_migrations, upgrade_to


def _execute_rejected(conn, statement):
    with pytest.raises(DBAPIError):
        conn.execute(text(statement))
    conn.rollback()


def _populate_0012(conn):
    """Build a complete legacy authority chain using only 0012 columns."""
    statements = [
        "INSERT INTO users(id,username,password_hash,created_at) "
        "VALUES (1,'owner','x',CURRENT_TIMESTAMP)",
        "INSERT INTO projects(id,name,status,created_by,created_at,updated_at,invite_code) "
        "VALUES (1,'legacy','active',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'legacy-invite')",
        "INSERT INTO project_memberships(id,project_id,user_id,role,can_review,status,created_at) "
        "VALUES (1,1,1,'owner',0,'active',CURRENT_TIMESTAMP)",
        "INSERT INTO behavior_categories(id,project_id,name,\"group\",sort_order,is_active,"
        "mouse_count_min,created_at) VALUES (1,1,'legacy category','legacy',0,1,1,CURRENT_TIMESTAMP)",
        "INSERT INTO videos(id,project_id,filename,status,workflow_status,annotation_revision,"
        "detection_import_revision,identity_revision,media_revision,created_at) "
        "VALUES (1,1,'legacy.mp4','metadata','draft',1,0,0,1,CURRENT_TIMESTAMP)",
        "INSERT INTO annotations(id,video_id,annotator_id,category_id,start_time,end_time,start_frame,"
        "end_frame,confidence,review_status,mouse_ids,mouse_id_status,detection_import_revision,"
        "identity_revision,created_at,updated_at) VALUES "
        "(1,1,1,1,0,1,0,25,'certain','pending','[]','needs_mouse_ids',0,0,"
        "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        "INSERT INTO detection_imports(id,video_id,revision,schema_version,status,active,created_by,"
        "created_at,edit_version,next_display_track_id) "
        "VALUES (1,1,1,'1.0','imported',1,1,CURRENT_TIMESTAMP,0,0)",
        "INSERT INTO detection_snapshots(id,detection_import_id,source_edit_version,raw_detection_count,"
        "override_count,schema_version,fps,width,height,frame_count,keypoint_names,skeleton_edges,created_at,"
        "raw_digest,state_digest,metadata_digest) VALUES "
        "(1,1,0,0,0,1,25,640,480,100,'[]','[]',CURRENT_TIMESTAMP,'" + "0" * 64 + "','" +
        "0" * 64 + "','" + "0" * 64 + "')",
        "INSERT INTO submissions(id,video_id,detection_snapshot_id,attempt_no,source_annotation_version,"
        "source_media_revision,source_video_filename,source_storage_key,source_video_sha256,status,"
        "submitted_by,submitted_at,legacy_backfill,source_file_size,source_mtime_ns,source_device,source_inode) "
        "VALUES (1,1,1,1,1,1,'legacy.mp4','legacy.mp4','" + "0" * 64 +
        "','submitted',1,CURRENT_TIMESTAMP,0,1,1,1,1)",
        "INSERT INTO submission_annotations(id,submission_id,source_annotation_id,category_id,category_name,"
        "start_time,end_time,start_frame,end_frame,confidence,crop_region,mouse_ids) "
        "VALUES (1,1,1,1,'legacy category',0,1,0,25,'certain',NULL,'[]')",
    ]
    for statement in statements:
        conn.execute(text(statement))


def test_populated_0012_to_0013_preserves_legacy_rows_and_installs_barriers(tmp_path):
    url = f"sqlite:///{(tmp_path / 'v0012-category-scheme.db').as_posix()}"
    upgrade_to(url, "0012")
    engine = create_engine(url)
    with engine.begin() as conn:
        _populate_0012(conn)

    # 0012 installs the legacy trigger and protects every then-existing frozen column.
    with engine.connect() as conn:
        trigger_names = set(conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )).scalars())
        assert {"trg_annotation_update", "trg_annotation_delete"} <= trigger_names
        _execute_rejected(
            conn, "UPDATE submission_annotations SET category_name='changed' WHERE id=1"
        )

    engine.dispose()
    run_migrations(url)
    assert current_revision(url) == "0014"
    db_mod.configure_engine(url)
    with db_mod.engine.connect() as conn:
        row = conn.execute(text(
            "SELECT category_name,category_group,category_participant_mode,role_definitions_snapshot,"
            "participant_roles_snapshot FROM submission_annotations WHERE id=1"
        )).one()
        assert tuple(row) == ("legacy category", None, "unordered", "[]", "{}")
        assert conn.execute(text("SELECT count(*) FROM annotations WHERE id=1")).scalar_one() == 1
        for statement in (
            "UPDATE submission_annotations SET category_group='legacy' WHERE id=1",
            "UPDATE submission_annotations SET category_participant_mode='role_based' WHERE id=1",
            "UPDATE submission_annotations SET role_definitions_snapshot='[1]' WHERE id=1",
            "UPDATE submission_annotations SET participant_roles_snapshot='{\"x\":[]}' WHERE id=1",
            "UPDATE submission_annotations SET category_name='changed' WHERE id=1",
            "DELETE FROM submission_annotations WHERE id=1",
            # This row predates locking; after 0013 it may neither change nor disappear while unlocked.
            "UPDATE annotations SET start_time=0.1 WHERE id=1",
            "DELETE FROM annotations WHERE id=1",
        ):
            _execute_rejected(conn, statement)

    columns = {column["name"] for column in inspect(db_mod.engine).get_columns("submission_annotations")}
    assert {"category_group", "category_participant_mode", "role_definitions_snapshot",
            "participant_roles_snapshot"} <= columns
