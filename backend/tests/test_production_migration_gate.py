from pathlib import Path

import pytest

from app.config import Settings
from app.main import create_app
from app.migration import (
    MigrationStateError,
    current_revision,
    require_schema_at_head,
    run_migrations,
    schema_head,
    upgrade_to,
)
from scripts import cleanup_retention, seed_demo


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _production_settings(tmp_path: Path, database_url: str) -> Settings:
    return Settings(
        env="production",
        data_dir=tmp_path / "data",
        database_url=database_url,
        secret_key="s" * 32,
        demo_username="admin",
        demo_password="password-long-enough",
        media_master_secret="AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE",
    )


def test_production_app_rejects_stale_database_without_upgrade(tmp_path):
    url = _url(tmp_path / "database.sqlite")
    upgrade_to(url, "0001")

    with pytest.raises(MigrationStateError, match="scripts/migrate.py"):
        create_app(_production_settings(tmp_path, url))

    assert current_revision(url) == "0001"
    assert not (tmp_path / "data").exists()


def test_production_seed_demo_rejects_stale_database_before_directories(tmp_path, monkeypatch):
    url = _url(tmp_path / "database.sqlite")
    upgrade_to(url, "0001")
    settings = _production_settings(tmp_path, url)
    monkeypatch.setattr(seed_demo, "get_settings", lambda: settings)
    monkeypatch.setattr(seed_demo.sys, "argv", ["seed_demo.py"])

    with pytest.raises(MigrationStateError, match="scripts/migrate.py"):
        seed_demo._main()

    assert current_revision(url) == "0001"
    assert not settings.data_dir.exists()


def test_production_cleanup_rejects_stale_database_before_directories(tmp_path, monkeypatch):
    url = _url(tmp_path / "database.sqlite")
    upgrade_to(url, "0001")
    settings = _production_settings(tmp_path, url)
    monkeypatch.setattr(cleanup_retention, "get_settings", lambda: settings)
    monkeypatch.setattr(cleanup_retention.sys, "argv", ["cleanup_retention.py"])

    with pytest.raises(MigrationStateError, match="scripts/migrate.py"):
        cleanup_retention.main()

    assert current_revision(url) == "0001"
    assert not settings.data_dir.exists()


def test_head_database_is_accepted(tmp_path):
    url = _url(tmp_path / "database.sqlite")
    run_migrations(url)

    require_schema_at_head(url)
    assert current_revision(url) == schema_head()


def test_missing_sqlite_gate_has_no_write_side_effects(tmp_path):
    database = tmp_path / "missing-parent" / "database.sqlite"

    with pytest.raises(MigrationStateError, match="数据库不存在"):
        require_schema_at_head(_url(database))

    assert not database.parent.exists()
    assert not Path(str(database) + ".pre-migration").exists()


def test_explicit_migration_still_upgrades(tmp_path):
    url = _url(tmp_path / "database.sqlite")
    upgrade_to(url, "0001")

    run_migrations(url)

    assert current_revision(url) == schema_head()


def test_development_app_still_migrates_fresh_database(tmp_path):
    database = tmp_path / "data" / "database.sqlite"
    settings = Settings(data_dir=tmp_path / "data", database_url=_url(database))

    create_app(settings)

    assert current_revision(settings.resolved_database_url) == schema_head()
