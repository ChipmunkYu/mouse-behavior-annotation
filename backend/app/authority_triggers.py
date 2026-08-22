"""SQLite authority barriers shared by Alembic and create_all."""
from sqlalchemy.exc import OperationalError
LEGACY_SUBMISSION_ANNOTATION_UPDATE = "BEFORE UPDATE ON submission_annotations WHEN NEW.submission_id IS NOT OLD.submission_id OR (NEW.source_annotation_id IS NOT OLD.source_annotation_id AND NOT (OLD.source_annotation_id IS NOT NULL AND NEW.source_annotation_id IS NULL)) OR NEW.category_id IS NOT OLD.category_id OR NEW.category_name IS NOT OLD.category_name OR NEW.start_time IS NOT OLD.start_time OR NEW.end_time IS NOT OLD.end_time OR NEW.start_frame IS NOT OLD.start_frame OR NEW.end_frame IS NOT OLD.end_frame OR NEW.confidence IS NOT OLD.confidence OR NEW.crop_region IS NOT OLD.crop_region OR NEW.mouse_ids IS NOT OLD.mouse_ids BEGIN SELECT RAISE(ABORT,'submission annotation immutable'); END"
TRIGGERS = {
"trg_snapshot_update": "BEFORE UPDATE ON detection_snapshots BEGIN SELECT RAISE(ABORT,'referenced snapshot immutable') WHERE EXISTS(SELECT 1 FROM submissions WHERE detection_snapshot_id=OLD.id); END",
"trg_snapshot_delete": "BEFORE DELETE ON detection_snapshots BEGIN SELECT RAISE(ABORT,'referenced snapshot immutable') WHERE EXISTS(SELECT 1 FROM submissions WHERE detection_snapshot_id=OLD.id); END",
"trg_state_insert": "BEFORE INSERT ON detection_snapshot_states BEGIN SELECT RAISE(ABORT,'referenced state immutable') WHERE EXISTS(SELECT 1 FROM submissions WHERE detection_snapshot_id=NEW.snapshot_id); END",
"trg_state_update": "BEFORE UPDATE ON detection_snapshot_states BEGIN SELECT RAISE(ABORT,'referenced state immutable') WHERE EXISTS(SELECT 1 FROM submissions WHERE detection_snapshot_id=OLD.snapshot_id); END",
"trg_state_delete": "BEFORE DELETE ON detection_snapshot_states BEGIN SELECT RAISE(ABORT,'referenced state immutable') WHERE EXISTS(SELECT 1 FROM submissions WHERE detection_snapshot_id=OLD.snapshot_id); END",
"trg_submission_frozen": "BEFORE UPDATE ON submissions WHEN NEW.video_id IS NOT OLD.video_id OR NEW.detection_snapshot_id IS NOT OLD.detection_snapshot_id OR NEW.attempt_no IS NOT OLD.attempt_no OR NEW.source_annotation_version IS NOT OLD.source_annotation_version OR NEW.source_media_revision IS NOT OLD.source_media_revision OR NEW.source_video_filename IS NOT OLD.source_video_filename OR NEW.source_storage_key IS NOT OLD.source_storage_key OR NEW.source_video_sha256 IS NOT OLD.source_video_sha256 OR NEW.source_file_size IS NOT OLD.source_file_size OR NEW.source_mtime_ns IS NOT OLD.source_mtime_ns OR NEW.source_device IS NOT OLD.source_device OR NEW.source_inode IS NOT OLD.source_inode OR (NEW.submitted_by IS NOT OLD.submitted_by AND NOT (OLD.submitted_by IS NOT NULL AND NEW.submitted_by IS NULL)) OR NEW.submitted_at IS NOT OLD.submitted_at OR NEW.legacy_backfill IS NOT OLD.legacy_backfill BEGIN SELECT RAISE(ABORT,'submission immutable'); END",
"trg_submission_lifecycle": "BEFORE UPDATE OF status,decided_at ON submissions WHEN NOT ((NEW.status=OLD.status AND NEW.decided_at IS OLD.decided_at) OR (OLD.status='submitted' AND NEW.status='withdrawn' AND NEW.decided_at IS OLD.decided_at) OR (OLD.status='submitted' AND NEW.status IN ('approved','rejected') AND NEW.decided_at IS NOT NULL) OR (OLD.status='approved' AND NEW.status='superseded' AND NEW.decided_at IS OLD.decided_at)) BEGIN SELECT RAISE(ABORT,'invalid submission lifecycle'); END",
"trg_annotation_update": "BEFORE UPDATE ON submission_annotations WHEN NEW.submission_id IS NOT OLD.submission_id OR (NEW.source_annotation_id IS NOT OLD.source_annotation_id AND NOT (OLD.source_annotation_id IS NOT NULL AND NEW.source_annotation_id IS NULL)) OR NEW.category_id IS NOT OLD.category_id OR NEW.category_name IS NOT OLD.category_name OR NEW.category_group IS NOT OLD.category_group OR NEW.category_participant_mode IS NOT OLD.category_participant_mode OR NEW.role_definitions_snapshot IS NOT OLD.role_definitions_snapshot OR NEW.participant_roles_snapshot IS NOT OLD.participant_roles_snapshot OR NEW.start_time IS NOT OLD.start_time OR NEW.end_time IS NOT OLD.end_time OR NEW.start_frame IS NOT OLD.start_frame OR NEW.end_frame IS NOT OLD.end_frame OR NEW.confidence IS NOT OLD.confidence OR NEW.crop_region IS NOT OLD.crop_region OR NEW.mouse_ids IS NOT OLD.mouse_ids BEGIN SELECT RAISE(ABORT,'submission annotation immutable'); END",
"trg_annotation_delete": "BEFORE DELETE ON submission_annotations BEGIN SELECT RAISE(ABORT,'submission annotation immutable'); END",
"trg_raw_delete": "BEFORE DELETE ON raw_detections BEGIN SELECT RAISE(ABORT,'snapshot raw immutable') WHERE EXISTS(SELECT 1 FROM detection_snapshots WHERE detection_import_id=OLD.detection_import_id); END",
"trg_raw_update": "BEFORE UPDATE OF detection_import_id,frame_index,frame_detection_index,raw_track_id,box,keypoints,detection_confidence,class_id ON raw_detections BEGIN SELECT RAISE(ABORT,'snapshot raw immutable') WHERE EXISTS(SELECT 1 FROM detection_snapshots WHERE detection_import_id=OLD.detection_import_id) OR EXISTS(SELECT 1 FROM detection_snapshots WHERE detection_import_id=NEW.detection_import_id); END",
"trg_raw_insert": "BEFORE INSERT ON raw_detections BEGIN SELECT RAISE(ABORT,'snapshot raw immutable') WHERE EXISTS(SELECT 1 FROM detection_snapshots WHERE detection_import_id=NEW.detection_import_id); END",
"trg_project_scheme_lock": "BEFORE UPDATE OF category_scheme_version,category_scheme_locked_at,category_scheme_locked_by ON projects BEGIN SELECT RAISE(ABORT,'category scheme lock is permanent') WHERE OLD.category_scheme_locked_at IS NOT NULL AND (NEW.category_scheme_version IS NOT OLD.category_scheme_version OR NEW.category_scheme_locked_at IS NOT OLD.category_scheme_locked_at OR NEW.category_scheme_locked_by IS NOT OLD.category_scheme_locked_by); SELECT RAISE(ABORT,'category scheme locker must be active owner') WHERE NEW.category_scheme_locked_by IS NOT NULL AND NOT EXISTS(SELECT 1 FROM project_memberships m WHERE m.project_id=OLD.id AND m.user_id=NEW.category_scheme_locked_by AND m.role='owner' AND m.status='active'); END",
"trg_category_locked_insert": "BEFORE INSERT ON behavior_categories BEGIN SELECT RAISE(ABORT,'category scheme is locked') WHERE EXISTS(SELECT 1 FROM projects p WHERE p.id=NEW.project_id AND p.category_scheme_locked_at IS NOT NULL); END",
"trg_category_locked_update": "BEFORE UPDATE ON behavior_categories BEGIN SELECT RAISE(ABORT,'category scheme is locked') WHERE EXISTS(SELECT 1 FROM projects p WHERE p.id IN (OLD.project_id,NEW.project_id) AND p.category_scheme_locked_at IS NOT NULL); END",
"trg_category_locked_delete": "BEFORE DELETE ON behavior_categories BEGIN SELECT RAISE(ABORT,'category scheme is locked') WHERE EXISTS(SELECT 1 FROM projects p WHERE p.id=OLD.project_id AND p.category_scheme_locked_at IS NOT NULL); END",
"trg_live_annotation_insert": "BEFORE INSERT ON annotations BEGIN SELECT RAISE(ABORT,'category scheme must be locked') WHERE NOT EXISTS(SELECT 1 FROM videos v JOIN projects p ON p.id=v.project_id WHERE v.id=NEW.video_id AND p.category_scheme_locked_at IS NOT NULL); SELECT RAISE(ABORT,'video and category projects differ') WHERE NOT EXISTS(SELECT 1 FROM videos v JOIN behavior_categories c ON c.id=NEW.category_id WHERE v.id=NEW.video_id AND v.project_id=c.project_id); END",
"trg_live_annotation_update": "BEFORE UPDATE OF video_id,category_id,start_time,end_time,start_frame,end_frame,confidence,crop_region,mouse_ids,mouse_id_status,detection_import_revision,identity_revision,participant_roles,participant_status ON annotations BEGIN SELECT RAISE(ABORT,'category scheme must be locked') WHERE NOT EXISTS(SELECT 1 FROM videos v JOIN projects p ON p.id=v.project_id WHERE v.id=NEW.video_id AND p.category_scheme_locked_at IS NOT NULL); SELECT RAISE(ABORT,'video and category projects differ') WHERE NOT EXISTS(SELECT 1 FROM videos v JOIN behavior_categories c ON c.id=NEW.category_id WHERE v.id=NEW.video_id AND v.project_id=c.project_id); END",
"trg_live_annotation_delete": "BEFORE DELETE ON annotations BEGIN SELECT RAISE(ABORT,'category scheme must be locked') WHERE NOT EXISTS(SELECT 1 FROM videos v JOIN projects p ON p.id=v.project_id WHERE v.id=OLD.video_id AND p.category_scheme_locked_at IS NOT NULL); END",
"trg_scheme_audit_update": "BEFORE UPDATE ON category_scheme_audits BEGIN SELECT RAISE(ABORT,'category scheme audit is append-only'); END",
"trg_scheme_audit_delete": "BEFORE DELETE ON category_scheme_audits BEGIN SELECT RAISE(ABORT,'category scheme audit is append-only'); END",
}
def install_sqlite_authority_triggers(connection):
    # Older Alembic revisions import this current module while walking to head.
    # Triggers introduced by a later revision are therefore skipped until their
    # target table/columns exist; 0013 performs a final complete reinstall.
    tables = {
        row[0] for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    project_columns = (
        {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(projects)")}
        if "projects" in tables else set()
    )
    annotation_columns = (
        {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(annotations)")}
        if "annotations" in tables else set()
    )
    submission_annotation_columns = (
        {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(submission_annotations)")}
        if "submission_annotations" in tables else set()
    )
    new_project_triggers = {
        "trg_project_scheme_lock", "trg_category_locked_insert",
        "trg_category_locked_update", "trg_category_locked_delete",
    }
    live_annotation_triggers = {
        "trg_live_annotation_insert", "trg_live_annotation_update",
        "trg_live_annotation_delete",
    }
    audit_triggers = {"trg_scheme_audit_update", "trg_scheme_audit_delete"}
    for name, body in TRIGGERS.items():
        if name in new_project_triggers and "category_scheme_locked_at" not in project_columns:
            continue
        if name in live_annotation_triggers and "participant_status" not in annotation_columns:
            continue
        if name in audit_triggers and "category_scheme_audits" not in tables:
            continue
        if name == "trg_annotation_update" and "category_group" not in submission_annotation_columns:
            body = LEGACY_SUBMISSION_ANNOTATION_UPDATE
        try:
            connection.exec_driver_sql(f"CREATE TRIGGER IF NOT EXISTS {name} {body}")
        except OperationalError as exc:
            if "no such table" not in str(exc).lower() and "no such column" not in str(exc).lower():
                raise
def drop_sqlite_authority_triggers(connection):
    for name in TRIGGERS: connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
