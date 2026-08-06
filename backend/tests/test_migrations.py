"""验收：Alembic 迁移（全新库 / P1 旧库升级 / 0002→0003 / 幂等 / 启动自动迁移）
与外键 ON DELETE 策略（SET NULL / RESTRICT）及新模型约束。"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from app import database as db_mod
from app import models
from app.auth import hash_password
from app.config import Settings
from app.main import create_app
from app.migration import (
    MigrationStateError,
    current_revision,
    inspect_state,
    run_migrations,
    upgrade_to,
)
from app.models import (
    Annotation,
    BackgroundJob,
    BehaviorCategory,
    Clip,
    Project,
    ProjectMembership,
    Review,
    User,
    Video,
)

P1_TABLES = [
    "users",
    "projects",
    "project_memberships",
    "behavior_categories",
    "videos",
    "annotations",
]
ALL_TABLES = P1_TABLES + ["reviews", "clips", "background_jobs", "alembic_version"]
VIDEO_NEW_COLUMNS = {"workflow_status", "annotation_revision", "submitted_at", "approved_at", "approved_by"}


def _settings(tmp_path: Path, name: str = "mig.db") -> Settings:
    return Settings(
        env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / name).as_posix()}",
    )


def _build_unversioned_p1(url: str, keep_empty_version_table: bool = False) -> tuple[int, int]:
    """构造真实形态的“未版本化 P1 旧库”：P1 表 + 旧数据，且无 alembic_version。

    先按 0001 建 P1 表，再删除 alembic_version 并仅用 P1 列写入旧数据
    （等价于第一阶段 create_all + 旧代码写入的产物）。

    keep_empty_version_table=True 时额外创建“空 alembic_version 表”（无版本行），
    模拟批次 1 实际缺陷：先前 `alembic check` 在 P1 库中留下了版本表副作用。
    """
    from datetime import datetime

    from sqlalchemy import text

    upgrade_to(url, "0001")
    db_mod.configure_engine(url)
    now = datetime.utcnow()
    with db_mod.engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(
            text(
                "INSERT INTO users (username, password_hash, created_at) "
                "VALUES (:u, :p, :now)"
            ),
            {"u": "owner", "p": "hash", "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO projects (name, description, status, created_by, created_at, updated_at) "
                "VALUES (:name, :desc, :status, 1, :now, :now)"
            ),
            {"name": "旧项目", "desc": "迁移前", "status": "active", "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO project_memberships (project_id, user_id, role, status, created_at) "
                "VALUES (1, 1, 'owner', 'active', :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                'INSERT INTO behavior_categories (project_id, name, "group", color, sort_order, is_active, created_at) '
                "VALUES (1, '攻击行为', '社交行为', '#E6194B', 0, 1, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO videos (project_id, filename, duration, fps, status, uploaded_by, created_at) "
                "VALUES (1, 'old.mp4', 10.0, 25.0, 'ready', 1, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO annotations (video_id, annotator_id, category_id, reviewer_id, "
                "start_time, end_time, start_frame, end_frame, confidence, review_status, "
                "crop_region, created_at, updated_at) "
                "VALUES (1, 1, 1, 1, 1.0, 2.0, 25, 50, 'certain', 'approved', NULL, :now, :now)"
            ),
            {"now": now},
        )
        if keep_empty_version_table:
            # 批次 1 缺陷形态：alembic_version 表存在但没有任何版本行
            conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
    return 1, 1  # video_id, annotation_id（SQLite INTEGER PRIMARY KEY 自增起始为 1）


def _fk_options(url: str, table: str, column: str) -> dict:
    """返回指定表/列外键的 options（含 ondelete；未声明时为 {}）。"""
    db_mod.configure_engine(url)
    insp = sa_inspect(db_mod.engine)
    for fk in insp.get_foreign_keys(table):
        if fk["constrained_columns"] == [column]:
            return fk["options"]
    raise AssertionError(f"{table}.{column} 未找到外键")


def test_fresh_db_upgrade_head_full_schema(tmp_path):
    """全新空库：upgrade head 建立完整 schema（P1 表 + 新表 + 新列）。"""
    settings = _settings(tmp_path)
    url = settings.resolved_database_url
    assert inspect_state(url) == "empty"

    run_migrations(url)
    assert inspect_state(url) == "versioned"
    assert current_revision(url) == "0004"

    db_mod.configure_engine(url)
    insp = sa_inspect(db_mod.engine)
    tables = set(insp.get_table_names())
    for table in ALL_TABLES:
        assert table in tables, f"缺少表 {table}"
    columns = {c["name"] for c in insp.get_columns("videos")}
    assert VIDEO_NEW_COLUMNS <= columns
    # 旧 P1 列保留
    for col in ("filename", "status", "storage_path", "created_at"):
        assert col in columns
    # 0003：四处 users 外键具备显式 ON DELETE 策略
    assert _fk_options(url, "videos", "uploaded_by")["ondelete"] == "SET NULL"
    assert _fk_options(url, "annotations", "reviewer_id")["ondelete"] == "SET NULL"
    assert _fk_options(url, "projects", "created_by")["ondelete"] == "RESTRICT"
    assert _fk_options(url, "annotations", "annotator_id")["ondelete"] == "RESTRICT"


def test_p1_old_db_upgrade_preserves_data(tmp_path):
    """P1 旧 schema + 旧数据 → 迁移后旧数据仍在、新增列默认正确。"""
    settings = _settings(tmp_path, "p1.db")
    url = settings.resolved_database_url
    video_id, annotation_id = _build_unversioned_p1(url)
    assert inspect_state(url) == "unversioned_p1"

    state = run_migrations(url)
    assert state == "unversioned_p1"
    assert inspect_state(url) == "versioned"

    with db_mod.SessionLocal() as db:
        # 旧数据完整保留
        assert db.query(User).count() == 1
        assert db.query(Project).count() == 1
        assert db.query(BehaviorCategory).count() == 1
        assert db.query(ProjectMembership).count() == 1
        assert db.query(Annotation).count() == 1
        video = db.get(Video, video_id)
        assert video.filename == "old.mp4"
        assert video.status == "ready"  # 原媒体 status 保留
        assert video.duration == 10.0
        # 新增列默认正确
        assert video.workflow_status == "draft"
        assert video.annotation_revision == 1
        assert video.submitted_at is None
        assert video.approved_at is None
        assert video.approved_by is None
        ann = db.get(Annotation, annotation_id)
        assert ann.review_status == "approved"
        assert ann.start_time == 1.0
        # 新表已存在且为空
        assert db.query(Review).count() == 0
        assert db.query(Clip).count() == 0
        assert db.query(BackgroundJob).count() == 0
    # 0003：外键 ON DELETE 策略已显式化
    assert _fk_options(url, "videos", "uploaded_by")["ondelete"] == "SET NULL"
    assert _fk_options(url, "annotations", "reviewer_id")["ondelete"] == "SET NULL"
    assert _fk_options(url, "projects", "created_by")["ondelete"] == "RESTRICT"
    assert _fk_options(url, "annotations", "annotator_id")["ondelete"] == "RESTRICT"


def test_existing_0002_db_to_0004(tmp_path):
    """已版本化 0002 库（含数据）→ head（0004）：数据保留、FK 策略更新、幂等。"""
    from datetime import datetime

    from sqlalchemy import text

    settings = _settings(tmp_path, "v0002.db")
    url = settings.resolved_database_url
    upgrade_to(url, "0002")  # 版本化到 0002，等价旧版 0002 运行库
    assert current_revision(url) == "0002"

    db_mod.configure_engine(url)
    now = datetime.utcnow()
    with db_mod.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (username, password_hash, created_at) "
                "VALUES (:u, :p, :now)"
            ),
            {"u": "owner", "p": "h", "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO projects (name, status, created_by, created_at, updated_at) "
                "VALUES ('项目', 'active', 1, :now, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                'INSERT INTO behavior_categories (project_id, name, "group", '
                "color, sort_order, is_active, created_at) "
                "VALUES (1, '攻击', '社交行为', '#E6194B', 0, 1, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO videos (project_id, filename, status, uploaded_by, "
                "workflow_status, annotation_revision, created_at) "
                "VALUES (1, 'v.mp4', 'ready', 1, 'draft', 1, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO annotations (video_id, annotator_id, category_id, reviewer_id, "
                "start_time, end_time, start_frame, end_frame, confidence, review_status, "
                "created_at, updated_at) "
                "VALUES (1, 1, 1, 1, 0.0, 1.0, 0, 25, 'certain', 'pending', :now, :now)"
            ),
            {"now": now},
        )

    # 0002 状态：上传者/审核人外键尚无显式 ondelete
    assert _fk_options(url, "videos", "uploaded_by").get("ondelete") is None

    run_migrations(url)  # 0002 → head（0004）
    assert current_revision(url) == "0004"
    assert _fk_options(url, "videos", "uploaded_by")["ondelete"] == "SET NULL"
    assert _fk_options(url, "annotations", "reviewer_id")["ondelete"] == "SET NULL"
    assert _fk_options(url, "projects", "created_by")["ondelete"] == "RESTRICT"
    assert _fk_options(url, "annotations", "annotator_id")["ondelete"] == "RESTRICT"

    with db_mod.SessionLocal() as db:
        assert db.query(User).count() == 1
        assert db.query(Project).count() == 1
        video = db.query(Video).one()
        assert video.filename == "v.mp4"
        assert video.uploaded_by == 1
        assert video.workflow_status == "draft"
        ann = db.query(Annotation).one()
        assert ann.reviewer_id == 1
        assert ann.start_time == 0.0

    # 重复运行幂等，版本与数据不变
    run_migrations(url)
    assert current_revision(url) == "0004"
    with db_mod.SessionLocal() as db:
        assert db.query(Video).count() == 1
        assert db.query(Annotation).count() == 1


def test_delete_user_sets_null_uploaded_by_and_reviewer(tmp_path):
    """删除用户：仅被 videos.uploaded_by / annotations.reviewer_id 引用 → 置空不报错。"""
    from datetime import datetime

    from sqlalchemy import text

    settings = _settings(tmp_path, "setnull.db")
    url = settings.resolved_database_url
    run_migrations(url)  # head（0004）
    db_mod.configure_engine(url)
    now = datetime.utcnow()
    with db_mod.engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (username, password_hash, created_at) VALUES (:u, :p, :now)"),
            {"u": "owner", "p": "h", "now": now},
        )
        conn.execute(
            text("INSERT INTO users (username, password_hash, created_at) VALUES (:u, :p, :now)"),
            {"u": "uploader", "p": "h", "now": now},
        )
        conn.execute(
            text("INSERT INTO users (username, password_hash, created_at) VALUES (:u, :p, :now)"),
            {"u": "reviewer", "p": "h", "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO projects (name, status, created_by, created_at, updated_at) "
                "VALUES ('项目', 'active', 1, :now, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                'INSERT INTO behavior_categories (project_id, name, "group", '
                "sort_order, is_active, created_at) "
                "VALUES (1, '攻击', '社交行为', 0, 1, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO videos (project_id, filename, status, uploaded_by, "
                "workflow_status, annotation_revision, created_at) "
                "VALUES (1, 'v.mp4', 'ready', 2, 'draft', 1, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO annotations (video_id, annotator_id, category_id, reviewer_id, "
                "start_time, end_time, start_frame, end_frame, confidence, review_status, "
                "created_at, updated_at) "
                "VALUES (1, 1, 1, 3, 0.0, 1.0, 0, 25, 'certain', 'pending', :now, :now)"
            ),
            {"now": now},
        )

    # 删除仅被 videos.uploaded_by 引用的用户（id=2）→ uploaded_by 置空，删除成功
    with db_mod.engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = 2"))
    with db_mod.SessionLocal() as db:
        assert db.get(Video, 1).uploaded_by is None

    # 删除仅被 annotations.reviewer_id 引用的用户（id=3）→ reviewer_id 置空，删除成功
    with db_mod.engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = 3"))
    with db_mod.SessionLocal() as db:
        assert db.get(Annotation, 1).reviewer_id is None

    # 数据本身保留
    with db_mod.SessionLocal() as db:
        video = db.get(Video, 1)
        assert video.filename == "v.mp4"
        assert video.uploaded_by is None
        ann = db.get(Annotation, 1)
        assert ann.end_time == 1.0
        assert ann.reviewer_id is None


def test_delete_user_rejected_by_created_by_and_annotator(tmp_path):
    """删除被引用的用户被拒绝：projects.created_by / annotations.annotator_id（RESTRICT）。"""
    from datetime import datetime

    from sqlalchemy import text

    settings = _settings(tmp_path, "restrict.db")
    url = settings.resolved_database_url
    run_migrations(url)  # head（0004）
    db_mod.configure_engine(url)
    now = datetime.utcnow()
    with db_mod.engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (username, password_hash, created_at) VALUES (:u, :p, :now)"),
            {"u": "owner", "p": "h", "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO projects (name, status, created_by, created_at, updated_at) "
                "VALUES ('项目', 'active', 1, :now, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                'INSERT INTO behavior_categories (project_id, name, "group", '
                "sort_order, is_active, created_at) "
                "VALUES (1, '攻击', '社交行为', 0, 1, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO videos (project_id, filename, status, "
                "workflow_status, annotation_revision, created_at) "
                "VALUES (1, 'v.mp4', 'ready', 'draft', 1, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO annotations (video_id, annotator_id, category_id, "
                "start_time, end_time, start_frame, end_frame, confidence, review_status, "
                "created_at, updated_at) "
                "VALUES (1, 1, 1, 0.0, 1.0, 0, 25, 'certain', 'pending', :now, :now)"
            ),
            {"now": now},
        )

    # 用户被 projects.created_by 且被 annotations.annotator_id 引用 → DELETE 被拒绝
    with db_mod.engine.begin() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(text("DELETE FROM users WHERE id = 1"))

    # 删除未生效：用户与引用数据均保留
    with db_mod.SessionLocal() as db:
        assert db.get(User, 1) is not None
        assert db.query(Project).count() == 1
        assert db.query(Annotation).count() == 1
        assert db.query(Annotation).one().annotator_id == 1


def test_repeated_migration_idempotent(tmp_path):
    """重复迁移幂等：fresh 库两次、P1 库升级后再次运行，均无副作用。"""
    settings = _settings(tmp_path, "idem.db")
    url = settings.resolved_database_url
    run_migrations(url)
    db_mod.configure_engine(url)
    with db_mod.SessionLocal() as db:
        db.add(User(username="u1", password_hash=hash_password("x")))
        db.commit()

    run_migrations(url)  # 第二次
    run_migrations(url)  # 第三次
    assert inspect_state(url) == "versioned"
    with db_mod.SessionLocal() as db:
        assert db.query(User).filter(User.username == "u1").count() == 1

    # P1 旧库升级后再次运行
    settings2 = _settings(tmp_path, "idem_p1.db")
    url2 = settings2.resolved_database_url
    _build_unversioned_p1(url2)
    run_migrations(url2)
    run_migrations(url2)
    with db_mod.SessionLocal() as db:
        assert db.query(Video).count() == 1
        video = db.query(Video).one()
        assert video.workflow_status == "draft"
        assert video.annotation_revision == 1


def test_empty_version_table_defect_regression(tmp_path):
    """批次 1 缺陷回归：P1 schema + 旧数据 + 空 alembic_version 表。

    inspect_state 必须按版本行判定为 unversioned_p1（而非仅看表存在而误判
    versioned）；run_migrations 先 stamp 0001 再升级到 head：迁移成功、
    数据保留、版本到 head、重复运行幂等。
    """
    settings = _settings(tmp_path, "defect.db")
    url = settings.resolved_database_url
    video_id, annotation_id = _build_unversioned_p1(url, keep_empty_version_table=True)
    assert inspect_state(url) == "unversioned_p1"

    state = run_migrations(url)
    assert state == "unversioned_p1"
    assert inspect_state(url) == "versioned"
    assert current_revision(url) == "0004"

    db_mod.configure_engine(url)
    with db_mod.SessionLocal() as db:
        # 旧数据完整保留
        assert db.query(User).count() == 1
        assert db.query(Project).count() == 1
        assert db.query(BehaviorCategory).count() == 1
        assert db.query(ProjectMembership).count() == 1
        assert db.query(Annotation).count() == 1
        video = db.get(Video, video_id)
        assert video.filename == "old.mp4"
        assert video.status == "ready"
        assert video.duration == 10.0
        # 新增列默认正确
        assert video.workflow_status == "draft"
        assert video.annotation_revision == 1
        ann = db.get(Annotation, annotation_id)
        assert ann.review_status == "approved"
        assert ann.start_time == 1.0
        # 新表已存在且为空
        assert db.query(Review).count() == 0
        assert db.query(Clip).count() == 0
        assert db.query(BackgroundJob).count() == 0

    # 重复运行幂等，数据仍完整
    run_migrations(url)
    run_migrations(url)
    assert inspect_state(url) == "versioned"
    db_mod.configure_engine(url)
    with db_mod.SessionLocal() as db:
        assert db.query(User).count() == 1
        assert db.query(Annotation).count() == 1
        assert db.get(Annotation, annotation_id).start_time == 1.0


def test_empty_version_table_only_db_is_empty(tmp_path):
    """只有空 alembic_version 表（无任何数据表）→ empty，升级仍可建立完整 schema。"""
    from sqlalchemy import create_engine, text

    settings = _settings(tmp_path, "emptyver.db")
    url = settings.resolved_database_url
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
    eng.dispose()

    assert inspect_state(url) == "empty"
    run_migrations(url)
    assert inspect_state(url) == "versioned"
    assert current_revision(url) == "0004"

    db_mod.configure_engine(url)
    insp = sa_inspect(db_mod.engine)
    tables = set(insp.get_table_names())
    for table in ALL_TABLES:
        assert table in tables, f"缺少表 {table}"


def test_unknown_version_raises_without_modification(tmp_path):
    """未知版本号 → 安全报错；数据库未被修改、版本号保持原值。"""
    from sqlalchemy import text

    settings = _settings(tmp_path, "unknown.db")
    url = settings.resolved_database_url
    run_migrations(url)  # 先到 head（0004）
    db_mod.configure_engine(url)
    with db_mod.engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = '9999'"))

    with pytest.raises(MigrationStateError, match="未知迁移版本"):
        inspect_state(url)
    with pytest.raises(MigrationStateError):
        run_migrations(url)
    with db_mod.engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "9999"


def test_unexpected_table_raises_without_stamp(tmp_path):
    """非预期表 + 无版本行 → 安全报错，禁止盲 stamp（不写入任何版本行）。"""
    from sqlalchemy import text

    settings = _settings(tmp_path, "unexpected.db")
    url = settings.resolved_database_url
    _build_unversioned_p1(url)  # P1 表 + 数据，无 alembic_version
    db_mod.configure_engine(url)
    with db_mod.engine.begin() as conn:
        conn.execute(text("CREATE TABLE unexpected_extra (id INTEGER)"))

    with pytest.raises(MigrationStateError, match="非预期表"):
        inspect_state(url)
    with pytest.raises(MigrationStateError):
        run_migrations(url)
    # 未发生 stamp：alembic_version 仍不存在，数据未动
    assert "alembic_version" not in sa_inspect(db_mod.engine).get_table_names()


def test_mixed_p1_and_p2_without_version_raises(tmp_path):
    """P1 表 + 0002 增量表 + 无有效版本行 → 状态不一致，安全报错而非盲 stamp。"""
    settings = _settings(tmp_path, "mixed.db")
    url = settings.resolved_database_url
    run_migrations(url)  # 完整 head schema（P1 + P2 + 版本行）
    db_mod.configure_engine(url)
    from sqlalchemy import text

    with db_mod.engine.begin() as conn:
        conn.execute(text("DELETE FROM alembic_version"))  # 清空版本行 → 缺陷态

    assert "alembic_version" in sa_inspect(db_mod.engine).get_table_names()
    with pytest.raises(MigrationStateError, match="状态不一致"):
        inspect_state(url)
    with pytest.raises(MigrationStateError):
        run_migrations(url)


def test_current_revision_reporting(tmp_path):
    """current_revision：全新库 None → 迁移后返回 head 版本号。"""
    settings = _settings(tmp_path, "rev.db")
    url = settings.resolved_database_url
    assert current_revision(url) is None
    run_migrations(url)
    assert current_revision(url) == "0004"


def test_cli_check_distinguishes_empty_version_table(tmp_path):
    """CLI --check 输出能区分空版本表，且不修改数据库。"""
    import os
    import subprocess
    import sys

    settings = _settings(tmp_path, "cli_defect.db")
    url = settings.resolved_database_url
    _build_unversioned_p1(url, keep_empty_version_table=True)

    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    backend_dir = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "scripts/migrate.py", "--check", "--db-url", url],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "空版本表" in proc.stdout
    assert "未版本化" in proc.stdout
    # --check 未修改数据库：仍是 unversioned_p1（版本行仍为空）
    assert inspect_state(url) == "unversioned_p1"


def test_cli_check_reports_versioned_revision(tmp_path):
    """CLI --check 对已版本化库打印当前版本号。"""
    import os
    import subprocess
    import sys

    settings = _settings(tmp_path, "cli_versioned.db")
    url = settings.resolved_database_url
    run_migrations(url)

    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    backend_dir = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "scripts/migrate.py", "--check", "--db-url", url],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "已版本化" in proc.stdout
    assert "0004" in proc.stdout



def test_create_app_auto_migrates_p1_db(tmp_path):
    """README 最短启动：直接 create_app（不手动迁移）在 P1 旧库上不崩溃并自动升级。"""
    settings = _settings(tmp_path, "startup.db")
    url = settings.resolved_database_url
    upgrade_to(url, "0001")
    db_mod.configure_engine(url)
    with db_mod.SessionLocal() as db:
        user = User(username="owner", password_hash=hash_password("pw123"))
        db.add(user)
        db.commit()
        db.refresh(user)
        db.add(Project(name="P1 旧项目", status="active", created_by=user.id))
        db.commit()

    app = create_app(settings=settings)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
    assert inspect_state(url) == "versioned"

    with db_mod.SessionLocal() as db:
        video = Video(project_id=db.query(Project).one().id, filename="v.mp4", status="metadata")
        db.add(video)
        db.commit()
        db.refresh(video)
        assert video.workflow_status == "draft"
        assert video.annotation_revision == 1


def test_new_model_constraints_and_defaults(ctx):
    """新模型：状态默认 / 唯一约束 / 检查约束 / 外键 / 级联删除。"""
    with db_mod.SessionLocal() as db:
        owner = User(username="owner1", password_hash=hash_password("pw"))
        db.add(owner)
        db.commit()
        db.refresh(owner)
        project = Project(name="约束项目", status="active", created_by=owner.id)
        db.add(project)
        db.commit()
        db.refresh(project)
        db.add(ProjectMembership(project_id=project.id, user_id=owner.id, role="owner"))
        cat = BehaviorCategory(project_id=project.id, name="攻击", group="社交行为", sort_order=0)
        db.add(cat)
        db.commit()
        db.refresh(cat)
        video = Video(project_id=project.id, filename="c.mp4", status="ready")
        db.add(video)
        db.commit()
        db.refresh(video)
        ann = Annotation(
            video_id=video.id,
            annotator_id=owner.id,
            category_id=cat.id,
            start_time=0.0,
            end_time=1.0,
            start_frame=0,
            end_frame=25,
        )
        db.add(ann)
        db.commit()
        db.refresh(ann)

        # Review：created_at 默认值
        review = Review(
            project_id=project.id,
            video_id=video.id,
            reviewer_id=owner.id,
            result="approved",
            comment="ok",
            annotation_revision=1,
        )
        db.add(review)
        db.commit()
        db.refresh(review)
        assert review.created_at is not None

        # Clip：status 默认 pending + (annotation_id, source_revision) 唯一
        clip = Clip(project_id=project.id, annotation_id=ann.id, source_revision=1)
        db.add(clip)
        db.commit()
        db.refresh(clip)
        assert clip.status == "pending"
        assert clip.clip_path is None
        dup = Clip(project_id=project.id, annotation_id=ann.id, source_revision=1)
        db.add(dup)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # BackgroundJob：status/progress 默认 + progress 范围检查约束
        job = BackgroundJob(project_id=project.id, job_type="clip")
        db.add(job)
        db.commit()
        db.refresh(job)
        assert job.status == "queued"
        assert job.progress == 0
        bad = BackgroundJob(project_id=project.id, job_type="clip", progress=101)
        db.add(bad)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # Video：annotation_revision >= 1 检查约束
        bad_v = Video(project_id=project.id, filename="bad.mp4", status="ready", annotation_revision=0)
        db.add(bad_v)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # Video：approved_by 外键指向不存在的用户 → 拒绝
        bad_fk = Video(project_id=project.id, filename="fk.mp4", status="ready", approved_by=999999)
        db.add(bad_fk)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # 级联删除：删除视频 → reviews / clips / annotations 一并删除
        assert db.query(Review).filter(Review.video_id == video.id).count() == 1
        assert db.query(Clip).filter(Clip.annotation_id == ann.id).count() == 1
        db.delete(video)
        db.commit()
        assert db.query(Annotation).count() == 0
        assert db.query(Clip).count() == 0
        assert db.query(Review).count() == 0


def test_video_out_exposes_workflow_fields(ctx, login_headers):
    """Pydantic 输出模型暴露新增 Video 工作流字段，且 P1 行为不变。"""
    headers = login_headers()
    project = ctx.client.post("/api/projects", json={"name": "工作流字段"}, headers=headers).json()
    resp = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "wf.mp4", "status": "ready"},
        headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "ready"  # P1 媒体 status 保留
    assert body["workflow_status"] == "draft"
    assert body["annotation_revision"] == 1
    assert body["submitted_at"] is None
    assert body["approved_at"] is None
    assert body["approved_by"] is None


# ---------- 批次 4：0004 迁移（BackgroundJob.dedupe_key / attempts） ----------


def test_0004_fresh_db_adds_dedupe_and_attempts(tmp_path):
    """全新库 upgrade head：background_jobs 含 dedupe_key / attempts，且 dedupe_key 唯一索引生效。"""
    settings = _settings(tmp_path, "v0004.db")
    url = settings.resolved_database_url
    run_migrations(url)
    assert current_revision(url) == "0004"

    db_mod.configure_engine(url)
    insp = sa_inspect(db_mod.engine)
    cols = {c["name"] for c in insp.get_columns("background_jobs")}
    assert "dedupe_key" in cols
    assert "attempts" in cols
    # 唯一索引存在
    uniq = [i for i in insp.get_indexes("background_jobs") if i.get("unique")]
    assert any(i["name"] == "ix_background_jobs_dedupe_key" for i in uniq)
    assert any("dedupe_key" in i["column_names"] for i in uniq)

    # 新模型默认：attempts 默认 0、dedupe_key 默认 None
    with db_mod.SessionLocal() as db:
        job = BackgroundJob(project_id=None, job_type="media")
        db.add(job)
        db.commit()
        db.refresh(job)
        assert job.attempts == 0
        assert job.dedupe_key is None


def test_0003_db_upgrade_to_0004_preserves_data(tmp_path):
    """已版本化 0003 库（含 background_jobs 数据）→ 0004：数据保留、新列默认正确。"""
    from datetime import datetime

    from sqlalchemy import text

    settings = _settings(tmp_path, "v0003.db")
    url = settings.resolved_database_url
    upgrade_to(url, "0003")
    assert current_revision(url) == "0003"

    db_mod.configure_engine(url)
    now = datetime.utcnow()
    with db_mod.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO background_jobs (project_id, job_type, status, progress, "
                "created_at) VALUES (NULL, 'media', 'queued', 0, :now)"
            ),
            {"now": now},
        )

    run_migrations(url)
    assert current_revision(url) == "0004"
    with db_mod.SessionLocal() as db:
        job = db.query(BackgroundJob).one()
        assert job.job_type == "media"
        assert job.status == "queued"
        assert job.dedupe_key is None  # 旧行 dedupe_key 为空
        assert job.attempts == 0  # 默认 0

    # 重复运行幂等
    run_migrations(url)
    assert current_revision(url) == "0004"
    with db_mod.SessionLocal() as db:
        assert db.query(BackgroundJob).count() == 1


def test_dedupe_key_unique_enforced(tmp_path):
    """dedupe_key 唯一约束：同一键第二次插入被拒绝（防重复任务的 DB 级兜底）。"""
    settings = _settings(tmp_path, "dedupe.db")
    url = settings.resolved_database_url
    run_migrations(url)
    db_mod.configure_engine(url)
    with db_mod.SessionLocal() as db:
        db.add(BackgroundJob(project_id=None, job_type="media", dedupe_key="media:video:1:rev:1"))
        db.commit()
        dup = BackgroundJob(
            project_id=None, job_type="media", dedupe_key="media:video:1:rev:1"
        )
        db.add(dup)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        # 多个 NULL 仍允许（非媒体任务不受影响）
        db.add(BackgroundJob(project_id=None, job_type="cleanup"))
        db.add(BackgroundJob(project_id=None, job_type="export"))
        db.commit()
        assert db.query(BackgroundJob).count() == 3
