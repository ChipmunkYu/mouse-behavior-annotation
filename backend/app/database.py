"""SQLAlchemy 引擎与会话管理（SQLite / 可切换其它方言）。"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


engine: Engine | None = None
SessionLocal: sessionmaker | None = None


def configure_engine(database_url: str) -> Engine:
    """配置全局引擎与 Session 工厂，必须在首次请求前调用。"""
    global engine, SessionLocal

    kwargs: dict = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if database_url == "sqlite:///:memory:":
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool

    engine = create_engine(database_url, **kwargs)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_sqlite_fk(dbapi_conn, _record):  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return engine


def ensure_schema(database_url: str) -> None:
    """配置引擎后执行幂等 schema 迁移（供 create_app / seed_demo 启动共用）。

    - 文件/网络库：走 Alembic——全新空库建立完整 schema；未版本化 P1 旧库先标记
      baseline 再升级；已版本化直接升级到 head。不删除已有数据，重复执行无副作用。
    - `sqlite:///:memory:`：内存库每次全新，直接 create_all（无迁移版本可循）。
    """
    if database_url == "sqlite:///:memory:":
        Base.metadata.create_all(bind=engine)
        return
    from .migration import run_migrations

    run_migrations(database_url)


def get_db():
    """FastAPI 依赖：请求级会话。"""
    if SessionLocal is None:
        raise RuntimeError("Database not configured; call configure_engine first")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
