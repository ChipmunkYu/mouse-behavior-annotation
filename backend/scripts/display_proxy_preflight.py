"""Read-only readiness check before enabling strict display-proxy playback."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402
from app.display_proxy_preflight import inspect_display_proxy_readiness  # noqa: E402

SAFE_UNAVAILABLE = "Display proxy strict-mode preflight: ERROR (configuration or database unavailable)"


def _create_preflight_engine(database_url: str):
    """Create an engine whose SQLite connections are read-only at the driver level."""
    parsed = make_url(database_url)
    if not parsed.drivername.startswith("sqlite"):
        return create_engine(database_url)

    database = parsed.database
    if not database or database == ":memory:":
        raise RuntimeError("database unavailable")
    database_path = Path(database).expanduser().resolve()

    def connect_read_only():
        # mode=ro prevents creation and writes; query_only also protects against
        # accidental writes if SQLite's URI handling changes upstream.
        connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only = ON")
        return connection

    return create_engine("sqlite://", creator=connect_read_only)


def _safe_configuration_summary(settings, *, env_file_provided: bool) -> str:
    env_name = settings.env.strip().lower()
    env_category = env_name if env_name in {"development", "test", "testing", "production"} else "other"
    database_category = (
        "sqlite" if make_url(settings.resolved_database_url).drivername.startswith("sqlite") else "non-sqlite"
    )
    return (
        "Configuration target: "
        f"env={env_category}; env_file={'provided' if env_file_provided else 'default'}; "
        f"data_dir={'configured' if settings.data_dir else 'unset'}; database={database_category}"
    )


def run_preflight(settings_loader=get_settings, output=print, *, env_file: str | Path | None = None) -> int:
    engine = None
    try:
        if env_file is not None:
            env_path = Path(env_file).expanduser()
            if not env_path.is_file():
                raise RuntimeError("configuration unavailable")
            # Explicit override makes this independent of the invoking shell's
            # cwd and applies the selected deployment environment before Settings.
            load_dotenv(dotenv_path=env_path, override=True, encoding="utf-8")
        settings = settings_loader()
        database_url = settings.resolved_database_url
        engine = _create_preflight_engine(database_url)
        factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        with factory() as db:
            summary = inspect_display_proxy_readiness(db, settings)
            db.rollback()
        output(_safe_configuration_summary(settings, env_file_provided=env_file is not None))
        for line in summary.lines():
            output(line)
        return 0 if summary.passed else 1
    except Exception:
        output(SAFE_UNAVAILABLE)
        return 2
    finally:
        if engine is not None:
            engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only readiness check before strict display-proxy playback"
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="explicit dotenv file loaded before application Settings (required by production procedure)",
    )
    args = parser.parse_args(argv)
    return run_preflight(env_file=args.env_file)


if __name__ == "__main__":
    raise SystemExit(main())
