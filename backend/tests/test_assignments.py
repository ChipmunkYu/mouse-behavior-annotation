"""视频分工 API 与不变量。"""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query, Session

from app import database as db_mod
from app.assignee_triggers import ASSIGNEE_CONFLICT_DETAIL
from app.models import BackgroundJob, ProjectMembership, Video


def _assignee_integrity_error() -> IntegrityError:
    original = sqlite3.IntegrityError(
        "assignee must be an active membership in the video project"
    )
    return IntegrityError("video assignee write", {}, original)


def _setup(ctx, login_headers):
    owner_headers = login_headers()
    project = ctx.client.post("/api/projects", json={"name": "assignments"}, headers=owner_headers).json()
    alice_id = ctx.create_user("alice"); bob_id = ctx.create_user("bob")
    ctx.add_member(project["id"], alice_id); ctx.add_member(project["id"], bob_id)
    with ctx.session_factory() as db:
        alice_mid = db.query(ProjectMembership.id).filter_by(project_id=project["id"], user_id=alice_id).scalar()
        bob_mid = db.query(ProjectMembership.id).filter_by(project_id=project["id"], user_id=bob_id).scalar()
    return project, owner_headers, login_headers("alice", "pw123"), login_headers("bob", "pw123"), alice_mid, bob_mid


def test_claim_competition_release_filters_and_stats(ctx, login_headers):
    project, owner_h, alice_h, bob_h, alice_mid, bob_mid = _setup(ctx, login_headers)
    video = ctx.client.post(f"/api/projects/{project['id']}/videos", json={"filename": "v.mp4"}, headers=owner_h).json()
    path = f"/api/projects/{project['id']}/videos/{video['id']}/claim"
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda h: ctx.client.post(path, headers=h), (alice_h, bob_h)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    winner = responses[0].json()["assignee_membership_id"] if responses[0].status_code == 200 else responses[1].json()["assignee_membership_id"]
    winner_h = alice_h if winner == alice_mid else bob_h
    assert len(ctx.client.get(f"/api/projects/{project['id']}/videos?view=mine", headers=winner_h).json()) == 1
    stats = ctx.client.get(f"/api/projects/{project['id']}/assignment-stats", headers=owner_h).json()
    assert stats["total"] == 1 and stats["unassigned"] == 0
    assert len(stats["by_assignee"]) == 3
    winner_stats = next(item for item in stats["by_assignee"] if item["assignee_membership_id"] == winner)
    assert winner_stats == {
        "assignee_membership_id": winner, "username": "alice" if winner == alice_mid else "bob",
        "total": 1, "draft": 1, "submitted": 0, "approved": 0, "rejected": 0,
    }
    released = ctx.client.post(f"/api/projects/{project['id']}/videos/{video['id']}/release", headers=winner_h)
    assert released.status_code == 200 and released.json()["assignee_membership_id"] is None


def test_claim_assignee_write_race_returns_stable_409_and_can_retry(
    monkeypatch, ctx, login_headers
):
    project, owner_h, alice_h, bob_h, alice_mid, bob_mid = _setup(ctx, login_headers)
    video = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "claim-race.mp4"},
        headers=owner_h,
    ).json()
    original_update = Query.update
    fail_claim = True

    def assignee_invalidated_at_write(query, values, *args, **kwargs):
        nonlocal fail_claim
        assignment = next(
            (
                value
                for key, value in values.items()
                if getattr(key, "key", key) == "assignee_membership_id"
            ),
            None,
        )
        if fail_claim and assignment == alice_mid:
            fail_claim = False
            raise _assignee_integrity_error()
        return original_update(query, values, *args, **kwargs)

    monkeypatch.setattr(Query, "update", assignee_invalidated_at_write)
    path = f"/api/projects/{project['id']}/videos/{video['id']}/claim"
    failed = ctx.client.post(path, headers=alice_h)
    assert failed.status_code == 409
    assert failed.json()["detail"] == ASSIGNEE_CONFLICT_DETAIL
    with ctx.session_factory() as db:
        assert db.get(Video, video["id"]).assignee_membership_id is None

    retried = ctx.client.post(path, headers=bob_h)
    assert retried.status_code == 200
    assert retried.json()["assignee_membership_id"] == bob_mid


def test_batch_assignment_validates_all_before_mutation_and_preserves_revisions(ctx, login_headers):
    project, owner_h, _alice_h, _bob_h, alice_mid, _bob_mid = _setup(ctx, login_headers)
    v1 = ctx.client.post(f"/api/projects/{project['id']}/videos", json={"filename": "1.mp4"}, headers=owner_h).json()
    v2 = ctx.client.post(f"/api/projects/{project['id']}/videos", json={"filename": "2.mp4"}, headers=owner_h).json()
    with ctx.session_factory() as db:
        row = db.get(Video, v2["id"]); row.workflow_status = "submitted"; db.commit()
    failed = ctx.client.post(
        f"/api/projects/{project['id']}/videos/assignments",
        json={"video_ids": [v1["id"], v2["id"]], "assignee_membership_id": alice_mid}, headers=owner_h,
    )
    assert failed.status_code == 409
    with ctx.session_factory() as db:
        assert db.get(Video, v1["id"]).assignee_membership_id is None
        before = (db.get(Video, v1["id"]).annotation_revision, db.get(Video, v1["id"]).media_revision)
        jobs_before = db.query(BackgroundJob).count()
    ok = ctx.client.post(
        f"/api/projects/{project['id']}/videos/assignments",
        json={"video_ids": [v1["id"]], "assignee_membership_id": alice_mid}, headers=owner_h,
    )
    assert ok.status_code == 200
    with ctx.session_factory() as db:
        row = db.get(Video, v1["id"])
        assert (row.annotation_revision, row.media_revision) == before
        assert db.query(BackgroundJob).count() == jobs_before


def test_batch_assignment_cas_rolls_back_if_submit_wins(monkeypatch, ctx, login_headers):
    project, owner_h, _alice_h, _bob_h, alice_mid, _ = _setup(ctx, login_headers)
    first = ctx.client.post(f"/api/projects/{project['id']}/videos", json={"filename": "1.mp4"}, headers=owner_h).json()
    second = ctx.client.post(f"/api/projects/{project['id']}/videos", json={"filename": "2.mp4"}, headers=owner_h).json()
    original_update = Query.update
    injected = False

    def submit_before_assignment(query, values, *args, **kwargs):
        nonlocal injected
        if not injected and any(getattr(key, "key", key) == "assignee_membership_id" for key in values):
            injected = True
            query.session.execute(update(Video).where(Video.id == second["id"]).values(workflow_status="submitted"))
        return original_update(query, values, *args, **kwargs)

    monkeypatch.setattr(Query, "update", submit_before_assignment)
    response = ctx.client.post(
        f"/api/projects/{project['id']}/videos/assignments",
        json={"video_ids": [first["id"], second["id"], second["id"]],
              "assignee_membership_id": alice_mid}, headers=owner_h,
    )
    assert response.status_code == 409
    with ctx.session_factory() as db:
        assert db.get(Video, first["id"]).assignee_membership_id is None
        assert db.get(Video, second["id"]).assignee_membership_id is None
        assert db.get(Video, second["id"]).workflow_status == "draft"


def test_cross_project_and_inactive_assignee_rejected(ctx, login_headers):
    project, owner_h, _alice_h, _bob_h, alice_mid, _ = _setup(ctx, login_headers)
    other = ctx.client.post("/api/projects", json={"name": "other"}, headers=owner_h).json()
    assert ctx.client.post(
        f"/api/projects/{other['id']}/videos",
        json={"filename": "x.mp4", "assignee_membership_id": alice_mid}, headers=owner_h,
    ).status_code == 400
    with ctx.session_factory() as db:
        db.get(ProjectMembership, alice_mid).status = "inactive"; db.commit()
    assert ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "x.mp4", "assignee_membership_id": alice_mid}, headers=owner_h,
    ).status_code == 400


def test_create_and_batch_assignment_assignee_race_returns_409_and_retries(
    monkeypatch, ctx, login_headers
):
    project, owner_h, _alice_h, _bob_h, alice_mid, bob_mid = _setup(ctx, login_headers)
    original_commit = Session.commit
    fail_create = True

    def race_commit(session):
        nonlocal fail_create
        if fail_create and any(
            isinstance(item, Video) and item.assignee_membership_id == alice_mid
            for item in session.new
        ):
            fail_create = False
            raise _assignee_integrity_error()
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", race_commit)
    failed_create = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "race.mp4", "assignee_membership_id": alice_mid},
        headers=owner_h,
    )
    assert failed_create.status_code == 409
    assert failed_create.json()["detail"] == ASSIGNEE_CONFLICT_DETAIL
    created = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "retry.mp4", "assignee_membership_id": bob_mid},
        headers=owner_h,
    )
    assert created.status_code == 201

    first = ctx.client.post(
        f"/api/projects/{project['id']}/videos", json={"filename": "1.mp4"}, headers=owner_h
    ).json()
    second = ctx.client.post(
        f"/api/projects/{project['id']}/videos", json={"filename": "2.mp4"}, headers=owner_h
    ).json()
    original_update = Query.update
    fail_batch = True

    def race_update(query, values, *args, **kwargs):
        nonlocal fail_batch
        if fail_batch and any(getattr(key, "key", key) == "assignee_membership_id" for key in values):
            fail_batch = False
            raise _assignee_integrity_error()
        return original_update(query, values, *args, **kwargs)

    monkeypatch.setattr(Query, "update", race_update)
    failed_batch = ctx.client.post(
        f"/api/projects/{project['id']}/videos/assignments",
        json={"video_ids": [first["id"], second["id"]], "assignee_membership_id": alice_mid},
        headers=owner_h,
    )
    assert failed_batch.status_code == 409
    assert failed_batch.json()["detail"] == ASSIGNEE_CONFLICT_DETAIL
    with ctx.session_factory() as db:
        assert db.get(Video, first["id"]).assignee_membership_id is None
        assert db.get(Video, second["id"]).assignee_membership_id is None
    retried = ctx.client.post(
        f"/api/projects/{project['id']}/videos/assignments",
        json={"video_ids": [first["id"], second["id"]], "assignee_membership_id": bob_mid},
        headers=owner_h,
    )
    assert retried.status_code == 200
    assert {item["assignee_membership_id"] for item in retried.json()} == {bob_mid}


def test_create_all_installs_direct_sql_assignee_barriers(ctx, login_headers):
    project, owner_h, _alice_h, _bob_h, alice_mid, _ = _setup(ctx, login_headers)
    other = ctx.client.post("/api/projects", json={"name": "other"}, headers=owner_h).json()
    video = ctx.client.post(f"/api/projects/{project['id']}/videos", json={"filename": "direct.mp4"}, headers=owner_h).json()
    with ctx.session_factory() as db:
        with pytest.raises(IntegrityError):
            db.execute(text("UPDATE videos SET project_id=:pid,assignee_membership_id=:mid WHERE id=:vid"),
                       {"pid": other["id"], "mid": alice_mid, "vid": video["id"]})
            db.commit()
        db.rollback()
        db.execute(text("UPDATE project_memberships SET status='inactive' WHERE id=:mid"), {"mid": alice_mid})
        db.commit()
        with pytest.raises(IntegrityError):
            db.execute(text("UPDATE videos SET assignee_membership_id=:mid WHERE id=:vid"),
                       {"mid": alice_mid, "vid": video["id"]})
            db.commit()
        db.rollback()
        db.execute(text("UPDATE project_memberships SET status='active' WHERE id=:mid"), {"mid": alice_mid})
        db.execute(text("UPDATE videos SET assignee_membership_id=:mid WHERE id=:vid"),
                   {"mid": alice_mid, "vid": video["id"]})
        db.commit()
        with pytest.raises(IntegrityError):
            db.execute(text("UPDATE project_memberships SET status='inactive' WHERE id=:mid"), {"mid": alice_mid})
            db.commit()
        db.rollback()
        with pytest.raises(IntegrityError):
            db.execute(text("DELETE FROM project_memberships WHERE id=:mid"), {"mid": alice_mid})
            db.commit()
        db.rollback()


def test_true_memory_ensure_schema_installs_all_assignee_barriers():
    url = "sqlite:///:memory:"
    engine = db_mod.configure_engine(url)
    db_mod.ensure_schema(url)
    with engine.begin() as conn:
        tables = set(conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).scalars())
        assert {"users", "projects", "project_memberships", "videos"} <= tables
        triggers = set(conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )).scalars())
        assert {
            "trg_videos_active_assignee_insert",
            "trg_videos_active_assignee_update",
            "trg_membership_assignee_stays_active",
        } <= triggers

        conn.execute(text(
            "INSERT INTO users(id,username,password_hash,created_at) VALUES "
            "(1,'owner','x',CURRENT_TIMESTAMP),(2,'alice','x',CURRENT_TIMESTAMP),"
            "(3,'bob','x',CURRENT_TIMESTAMP)"
        ))
        conn.execute(text(
            "INSERT INTO projects(id,name,status,created_by,invite_code,created_at,updated_at) VALUES "
            "(1,'one','active',1,'one',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),"
            "(2,'two','active',1,'two',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        ))
        conn.execute(text(
            "INSERT INTO project_memberships(id,project_id,user_id,role,can_review,status,created_at) VALUES "
            "(1,1,1,'owner',1,'active',CURRENT_TIMESTAMP),"
            "(2,1,2,'member',0,'active',CURRENT_TIMESTAMP),"
            "(3,2,3,'member',0,'active',CURRENT_TIMESTAMP),"
            "(4,1,3,'member',0,'inactive',CURRENT_TIMESTAMP)"
        ))
        conn.execute(text(
            "INSERT INTO videos(id,project_id,filename,status,workflow_status,annotation_revision,"
            "detection_import_revision,identity_revision,media_revision,created_at) "
            "VALUES (1,1,'v.mp4','metadata','draft',1,0,0,1,CURRENT_TIMESTAMP)"
        ))

        with pytest.raises(IntegrityError, match="assignee must be an active membership"):
            conn.execute(text("UPDATE videos SET assignee_membership_id=3 WHERE id=1"))
        with pytest.raises(IntegrityError, match="assignee must be an active membership"):
            conn.execute(text("UPDATE videos SET assignee_membership_id=4 WHERE id=1"))

        conn.execute(text("UPDATE videos SET assignee_membership_id=2 WHERE id=1"))
        assert conn.execute(text("SELECT assignee_membership_id FROM videos WHERE id=1")).scalar() == 2
        with pytest.raises(IntegrityError, match="assigned membership must remain active"):
            conn.execute(text("UPDATE project_memberships SET status='inactive' WHERE id=2"))
        with pytest.raises(IntegrityError):
            conn.execute(text("DELETE FROM project_memberships WHERE id=2"))
