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


def get_db():
    """FastAPI 依赖：请求级会话。"""
    if SessionLocal is None:
        raise RuntimeError("Database not configured; call configure_engine first")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
