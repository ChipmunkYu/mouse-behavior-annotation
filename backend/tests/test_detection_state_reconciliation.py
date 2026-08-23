"""Gate 1 remediation: strict 0008 preflight and maintenance reconciliation."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app import database as db_mod
from app.config import Settings
from app.detection_state_reconciliation import reconcile_detection_state
from app.migration import current_revision, downgrade_to, run_migrations, upgrade_to
from app.models import (
    CorrectedDetectionAssignment,
    CorrectedTrack,
    DetectionImport,
    DetectionSnapshot,
    DetectionSnapshotState,
    DetectionStateOverride,
    Project,
    RawDetection,
    User,
    Video,
)
from app.track_ids import TRACK_ID_UPPER_BOUND


def _url(tmp_path: Path, name: str) -> str:
    settings = Settings(
        env="test", data_dir=tmp_path, database_url=f"sqlite:///{(tmp_path / name).as_posix()}"
    )
    return settings.resolved_database_url


def _seed_0007_current(url: str, anomaly: str | None = None) -> None:
    upgrade_to(url, "0007")
    db_mod.configure_engine(url)
    now = datetime.utcnow()
    with db_mod.engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, username, password_hash, created_at) VALUES (1,'u','h',:n)"),
            {"n": now},
        )
        conn.execute(
            text(
                "INSERT INTO projects (id,name,status,created_by,created_at,updated_at) "
                "VALUES (1,'p','active',1,:n,:n)"
            ),
            {"n": now},
        )
        conn.execute(
            text(
                "INSERT INTO project_memberships "
                "(id,project_id,user_id,role,status,created_at) "
                "VALUES (1,1,1,'owner','active',:n)"
            ),
            {"n": now},
        )
        conn.execute(
            text(
                "INSERT INTO videos (id,project_id,filename,status,uploaded_by,created_at,"
                "workflow_status,annotation_revision,detection_import_revision,identity_revision,"
                "media_revision) VALUES (1,1,'v.mp4','ready',1,:n,'draft',1,1,3,1)"
            ),
            {"n": now},
        )
        conn.execute(
            text(
                "INSERT INTO detection_imports "
                "(id,video_id,revision,schema_version,status,active,created_by,created_at) "
                "VALUES (1,1,1,'1.0','imported',1,1,:n)"
            ),
            {"n": now},
        )
        if anomaly == "cross_import_track":
            conn.execute(
                text(
                    "INSERT INTO detection_imports "
                    "(id,video_id,revision,schema_version,status,active,created_by,created_at) "
                    "VALUES (2,1,2,'1.0','imported',0,1,:n)"
                ),
                {"n": now},
            )
        raw_track_id = -1 if anomaly == "negative_raw" else TRACK_ID_UPPER_BOUND if anomaly == "upper_raw" else 1
        conn.execute(
            text(
                "INSERT INTO raw_detections "
                "(id,detection_import_id,frame_index,frame_detection_index,raw_track_id) "
                "VALUES (1,1,0,0,:track_id)"
            ),
            {"track_id": raw_track_id},
        )
        if anomaly != "missing_cda" and anomaly != "missing_track":
            track_import = 2 if anomaly == "cross_import_track" else 1
            active = 0 if anomaly == "inactive_track" else 1
            display = TRACK_ID_UPPER_BOUND if anomaly == "upper_display" else 10
            conn.execute(
                text(
                    "INSERT INTO corrected_tracks "
                    "(id,detection_import_id,display_track_id,effective_detection_count,"
                    "created_identity_revision,active) VALUES (1,:imp,:display,1,3,:active)"
                ),
                {"imp": track_import, "display": display, "active": active},
            )
            conn.execute(
                text(
                    "INSERT INTO corrected_detection_assignments "
                    "(id,raw_detection_id,corrected_track_id,identity_revision) VALUES (1,1,1,3)"
                )
            )

    if anomaly == "missing_track":
        # Construct the only otherwise-unrepresentable legacy corruption.
        with db_mod.engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            conn.execute(
                text(
                    "INSERT INTO corrected_detection_assignments "
                    "(id,raw_detection_id,corrected_track_id,identity_revision) VALUES (1,1,999,3)"
                )
            )
            conn.commit()
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")


@pytest.mark.parametrize(
    ("anomaly", "category"),
    [
        ("missing_cda", "missing_cda"),
        ("missing_track", "missing_track"),
        ("cross_import_track", "cross_import_track"),
        ("inactive_track", "inactive_track"),
    ],
)
def test_0008_strict_current_preflight_fails_before_schema(tmp_path, anomaly, category):
    url = _url(tmp_path, f"{anomaly}.db")
    _seed_0007_current(url, anomaly)

    with pytest.raises(RuntimeError) as exc_info:
        run_migrations(url)
    message = str(exc_info.value)
    assert all(token in message for token in ("import=1", "video=1", "revision=3", category, "count=1"))
    assert current_revision(url) == "0007"
    assert "edit_version" not in {c["name"] for c in inspect(db_mod.engine).get_columns("detection_imports")}
    assert "detection_state_overrides" not in inspect(db_mod.engine).get_table_names()


@pytest.mark.parametrize("anomaly", ["negative_raw", "upper_raw", "upper_display"])
def test_0008_track_domain_preflight_fails_before_schema(tmp_path, anomaly):
    url = _url(tmp_path, f"{anomaly}.db")
    _seed_0007_current(url, anomaly)
    with pytest.raises(ValueError) as exc_info:
        run_migrations(url)
    assert "track ID domain" in str(exc_info.value)
    assert "import=1" in str(exc_info.value)
    assert current_revision(url) == "0007"


def test_0008_downgrade_reupgrade_preserves_legacy_and_rebackfills(tmp_path):
    url = _url(tmp_path, "cycle.db")
    _seed_0007_current(url)
    now = datetime.utcnow()
    with db_mod.engine.begin() as conn:
        conn.execute(
            text(
                'INSERT INTO behavior_categories '
                '(id,project_id,name,"group",sort_order,is_active,mouse_count_min,created_at) '
                "VALUES (1,1,'行走','个体行为',0,1,1,:n)"
            ),
            {"n": now},
        )
        conn.execute(
            text(
                "INSERT INTO annotations "
                "(id,video_id,annotator_id,category_id,start_time,end_time,start_frame,end_frame,"
                "confidence,review_status,crop_region,mouse_ids,mouse_id_status,"
                "detection_import_revision,identity_revision,created_at,updated_at) "
                "VALUES (1,1,1,1,0,1,0,25,'certain','pending',NULL,'[1]','valid',1,3,:n,:n)"
            ),
            {"n": now},
        )
        conn.execute(
            text(
                "INSERT INTO reviews "
                "(id,project_id,video_id,reviewer_id,result,annotation_revision,"
                "detection_import_revision,identity_revision,created_at) "
                "VALUES (1,1,1,1,'approved',1,1,3,:n)"
            ),
            {"n": now},
        )
        conn.execute(
            text(
                "INSERT INTO clips "
                "(id,project_id,annotation_id,source_revision,media_revision,status,created_at,updated_at) "
                "VALUES (1,1,1,1,1,'ready',:n,:n)"
            ),
            {"n": now},
        )

    run_migrations(url)
    with db_mod.engine.connect() as conn:
        assert conn.execute(text("SELECT display_track_id FROM detection_state_overrides")).scalar() == 10

    downgrade_to(url, "0007")
    assert current_revision(url) == "0007"
    inspector = inspect(db_mod.engine)
    assert "detection_state_overrides" not in inspector.get_table_names()
    assert "submission_id" not in {c["name"] for c in inspector.get_columns("reviews")}
    assert "submission_annotation_id" not in {c["name"] for c in inspector.get_columns("clips")}
    assert "ix_reviews_video_revision" in {i["name"] for i in inspector.get_indexes("reviews")}
    assert "uq_clip_annotation_revision" in {u["name"] for u in inspector.get_unique_constraints("clips")}
    with db_mod.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM raw_detections")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM reviews")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM clips")).scalar() == 1

    run_migrations(url)
    assert current_revision(url) == "0014"
    inspector = inspect(db_mod.engine)
    assert {"participant_mode", "role_definitions"} <= {
        column["name"] for column in inspector.get_columns("behavior_categories")
    }
    assert {"participant_roles", "participant_status"} <= {
        column["name"] for column in inspector.get_columns("annotations")
    }
    assert {"category_group", "category_participant_mode", "role_definitions_snapshot",
            "participant_roles_snapshot"} <= {
        column["name"] for column in inspector.get_columns("submission_annotations")
    }
    with db_mod.engine.connect() as conn:
        assert conn.execute(text("SELECT display_track_id FROM detection_state_overrides")).scalar() == 10
        assert conn.execute(text("SELECT submission_id FROM reviews")).scalar() is None
        assert conn.execute(text("SELECT submission_annotation_id FROM clips")).scalar() is None
        assert conn.execute(text("SELECT participant_mode FROM behavior_categories WHERE id=1")).scalar() == "unordered"
        assert conn.execute(text("SELECT participant_roles FROM annotations WHERE id=1")).scalar() == "{}"
    # 0013 deliberately leaves pre-existing schemes unlocked and its authority trigger
    # rejects all live annotation writes until an active owner explicitly locks one.
    with db_mod.engine.begin() as conn:
        conn.execute(text(
            "UPDATE projects SET category_scheme_locked_at=CURRENT_TIMESTAMP, "
            "category_scheme_locked_by=1 WHERE id=1"
        ))
    summary = reconcile_detection_state(url, legacy_writer_stopped=True)
    assert summary["shadow_difference_count"] == 0
    with db_mod.engine.connect() as conn:
        assert conn.execute(text("SELECT identity_revision FROM videos WHERE id=1")).scalar() == 1
        assert conn.execute(text("SELECT identity_revision FROM annotations WHERE id=1")).scalar() == 1


def test_reconciliation_is_repeatable_and_rolls_back_on_invalid_legacy(tmp_path):
    url = _url(tmp_path, "reconcile.db")
    run_migrations(url)
    db_mod.configure_engine(url)
    with db_mod.SessionLocal() as db:
        user = User(username="maint", password_hash="h")
        db.add(user)
        db.flush()
        project = Project(name="p", status="active", created_by=user.id)
        db.add(project)
        db.flush()
        video = Video(project_id=project.id, filename="v.mp4", status="ready", identity_revision=0)
        db.add(video)
        db.flush()
        detection_import = DetectionImport(
            video_id=video.id, revision=1, schema_version="1.0", status="imported", active=True
        )
        db.add(detection_import)
        db.flush()
        raw = RawDetection(
            detection_import_id=detection_import.id,
            frame_index=0,
            frame_detection_index=0,
            raw_track_id=1,
        )
        track = CorrectedTrack(
            detection_import_id=detection_import.id,
            display_track_id=10,
            effective_detection_count=1,
            created_identity_revision=0,
            active=True,
        )
        db.add_all([raw, track])
        db.flush()
        db.add(
            CorrectedDetectionAssignment(
                raw_detection_id=raw.id, corrected_track_id=track.id, identity_revision=0
            )
        )
        db.commit()
        import_id, raw_id, track_id = detection_import.id, raw.id, track.id

    first = reconcile_detection_state(url, legacy_writer_stopped=True)
    assert first == {"override_count": 1, "shadow_difference_count": 0}
    second = reconcile_detection_state(url, legacy_writer_stopped=True)
    assert second == first
    with db_mod.SessionLocal() as db:
        override = db.get(DetectionStateOverride, raw_id)
        assert (override.display_track_id, override.updated_edit_version) == (10, 1)
        assert db.get(DetectionImport, import_id).next_display_track_id == 11

    with db_mod.engine.begin() as conn:
        conn.execute(text("UPDATE corrected_tracks SET active=0 WHERE id=:id"), {"id": track_id})
    with pytest.raises(RuntimeError, match="inactive_track"):
        reconcile_detection_state(url, legacy_writer_stopped=True)
    with db_mod.SessionLocal() as db:
        override = db.get(DetectionStateOverride, raw_id)
        assert override.display_track_id == 10


def test_reconciliation_requires_stopped_writer_and_rolls_back_post_mutation(tmp_path):
    url = _url(tmp_path, "reconcile-rollback.db")
    _seed_0007_current(url)
    run_migrations(url)
    with pytest.raises(RuntimeError, match="legacy writer"):
        reconcile_detection_state(url, legacy_writer_stopped=False)

    db_mod.configure_engine(url)
    with db_mod.engine.begin() as connection:
        connection.execute(text("UPDATE detection_imports SET next_display_track_id=77"))
        connection.execute(text("UPDATE detection_imports SET edit_version=0"))
        connection.execute(text("DELETE FROM detection_state_overrides"))

    def fail_after_mutation() -> None:
        raise RuntimeError("post mutation failure")

    with pytest.raises(RuntimeError, match="post mutation"):
        reconcile_detection_state(
            url,
            legacy_writer_stopped=True,
            _post_mutation_hook=fail_after_mutation,
        )
    with db_mod.engine.connect() as connection:
        row = connection.execute(
            text("SELECT edit_version,next_display_track_id FROM detection_imports WHERE id=1")
        ).one()
        assert row == (0, 77)
        assert connection.execute(text("SELECT count(*) FROM detection_state_overrides")).scalar() == 0


def test_reconciliation_draft_guard_and_shadow_failure_roll_back(tmp_path, monkeypatch):
    import app.detection_state_reconciliation as reconciliation_module

    url = _url(tmp_path, "reconcile-guards.db")
    _seed_0007_current(url)
    run_migrations(url)
    db_mod.configure_engine(url)
    now = datetime.utcnow()
    with db_mod.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO draft_identity_edits "
                "(id,detection_import_id,applied_edit_version,operation,params,created_at) "
                "VALUES (1,1,1,'split','{}',:now)"
            ),
            {"now": now},
        )
    with pytest.raises(RuntimeError, match="empty draft stack"):
        reconcile_detection_state(url, legacy_writer_stopped=True)
    with db_mod.engine.begin() as connection:
        connection.execute(text("DELETE FROM draft_identity_edits"))
        connection.execute(text("UPDATE detection_imports SET next_display_track_id=88"))

    original = reconciliation_module.rebuild_sparse_detection_state

    def mismatching(connection):
        summary = original(connection)
        summary["shadow_difference_count"] = 1
        return summary

    monkeypatch.setattr(reconciliation_module, "rebuild_sparse_detection_state", mismatching)
    with pytest.raises(RuntimeError, match="shadow mismatch"):
        reconcile_detection_state(url, legacy_writer_stopped=True)
    with db_mod.engine.connect() as connection:
        assert connection.execute(
            text("SELECT next_display_track_id FROM detection_imports WHERE id=1")
        ).scalar() == 88


def test_reconciliation_holds_begin_immediate_write_lock(tmp_path):
    import sqlite3

    url = _url(tmp_path, "reconcile-lock.db")
    _seed_0007_current(url)
    run_migrations(url)
    database_path = Path(url.removeprefix("sqlite:///"))
    lock_observed = False

    def competing_write() -> None:
        nonlocal lock_observed
        connection = sqlite3.connect(database_path, timeout=0.01)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                connection.execute("UPDATE detection_imports SET next_display_track_id=9")
            lock_observed = True
        finally:
            connection.close()

    reconcile_detection_state(
        url,
        legacy_writer_stopped=True,
        _post_mutation_hook=competing_write,
    )
    assert lock_observed is True


def test_0008_track_domain_database_checks(tmp_path):
    url = _url(tmp_path, "domain-checks.db")
    run_migrations(url)
    db_mod.configure_engine(url)
    with db_mod.SessionLocal() as db:
        user = User(username="domain", password_hash="h")
        db.add(user)
        db.flush()
        project = Project(name="p", status="active", created_by=user.id)
        db.add(project)
        db.flush()
        video = Video(project_id=project.id, filename="v.mp4", status="ready")
        db.add(video)
        db.flush()
        detection_import = DetectionImport(
            video_id=video.id, revision=1, schema_version="1.0", active=True
        )
        db.add(detection_import)
        db.commit()
        import_id = detection_import.id

        for invalid_raw in (-1, TRACK_ID_UPPER_BOUND):
            db.add(
                RawDetection(
                    detection_import_id=import_id,
                    frame_index=0,
                    frame_detection_index=1,
                    raw_track_id=invalid_raw,
                )
            )
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()

        valid_raw = RawDetection(
            detection_import_id=import_id,
            frame_index=0,
            frame_detection_index=0,
            raw_track_id=1,
        )
        db.add(valid_raw)
        db.commit()
        raw_id = valid_raw.id
        db.add(
            DetectionStateOverride(
                raw_detection_id=raw_id,
                detection_import_id=import_id,
                display_track_id=TRACK_ID_UPPER_BOUND,
                suppressed=False,
                updated_edit_version=1,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        snapshot = DetectionSnapshot(
            detection_import_id=import_id,
            source_edit_version=0,
            raw_detection_count=1,
            override_count=1,
            schema_version=1,
            fps=25.0,
            width=640,
            height=480,
            frame_count=1,
            keypoint_names=[],
            skeleton_edges=[],
        )
        db.add(snapshot)
        db.commit()
        db.add(
            DetectionSnapshotState(
                snapshot_id=snapshot.id,
                raw_detection_id=raw_id,
                detection_import_id=import_id,
                display_track_id=TRACK_ID_UPPER_BOUND,
                suppressed=False,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        detection_import = db.get(DetectionImport, import_id)
        detection_import.next_display_track_id = TRACK_ID_UPPER_BOUND
        db.commit()
        detection_import.next_display_track_id = -1
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
