"""0012 migration-specific role and assignment constraints."""
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import database as db_mod
from app.migration import current_revision, downgrade_to, run_migrations, upgrade_to


def test_0011_to_0012_roles_videos_and_round_trip(tmp_path):
    url = f"sqlite:///{(tmp_path / 'assignment-migration.db').as_posix()}"
    upgrade_to(url, "0011")
    db_mod.configure_engine(url)
    with db_mod.engine.begin() as conn:
        conn.execute(text("INSERT INTO users(username,password_hash,created_at) VALUES ('u1','x',CURRENT_TIMESTAMP),('u2','x',CURRENT_TIMESTAMP),('u3','x',CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO projects(name,status,created_by,created_at,updated_at) VALUES ('p','active',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO project_memberships(project_id,user_id,role,status,created_at) VALUES (1,1,'owner','active',CURRENT_TIMESTAMP),(1,2,'reviewer','active',CURRENT_TIMESTAMP),(1,3,'annotator','active',CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO videos(project_id,filename,status,uploaded_by,created_at,workflow_status,annotation_revision,detection_import_revision,identity_revision,media_revision) VALUES (1,'v','metadata',1,CURRENT_TIMESTAMP,'draft',1,0,0,1)"))
    run_migrations(url)
    assert current_revision(url) == "0014"
    with db_mod.engine.connect() as conn:
        rows = conn.execute(text("SELECT role,can_review FROM project_memberships ORDER BY id")).fetchall()
        assert rows == [("owner", 0), ("member", 1), ("member", 0)]
        assert conn.execute(text("SELECT assignee_membership_id FROM videos")).scalar() is None
    downgrade_to(url, "0011")
    assert current_revision(url) == "0011"
    with db_mod.engine.connect() as conn:
        rows = conn.execute(text("SELECT role FROM project_memberships ORDER BY id")).scalars().all()
        assert rows == ["owner", "reviewer", "annotator"]
    run_migrations(url)
    assert current_revision(url) == "0014"
    with db_mod.engine.connect() as conn:
        rows = conn.execute(text("SELECT role,can_review FROM project_memberships ORDER BY id")).fetchall()
        assert rows == [("owner", 0), ("member", 1), ("member", 0)]
