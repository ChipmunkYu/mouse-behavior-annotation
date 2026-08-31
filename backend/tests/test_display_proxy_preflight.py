from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.display_proxy_jobs import enqueue_display_proxy
from app.display_proxy_preflight import (inspect_display_proxy_readiness,
                                         ready_entity_is_safe)
from app.display_proxy_processor import DISPLAY_PROXY_PROFILE_VERSION
from app.models import BackgroundJob, Video
from scripts.display_proxy_preflight import _create_preflight_engine, run_preflight


def _file_backed(ctx):
    info = ctx.make_project_with_video()
    settings = ctx.raw_client.app.state.settings
    source = settings.videos_dir / "source.mp4"
    source.write_bytes(b"source")
    with ctx.session_factory() as db:
        video = db.get(Video, info["video"]["id"])
        video.storage_path = source.name
        db.commit()
    return settings, info["video"]["id"]


def _make_ready(ctx, video_id):
    settings = ctx.raw_client.app.state.settings
    proxy = settings.display_proxies_dir / "safe-proxy.mp4"
    proxy.write_bytes(b"proxy")
    with ctx.session_factory() as db:
        video = db.get(Video, video_id)
        video.source_sha256 = video.display_source_sha256 = "a" * 64
        video.display_status = "ready"
        video.display_path = proxy.name
        video.display_profile_version = DISPLAY_PROXY_PROFILE_VERSION
        video.display_generated_at = datetime.utcnow()
        db.commit()


def test_preflight_success_is_read_only_and_metadata_only_is_excluded(ctx):
    settings, video_id = _file_backed(ctx)
    ctx.make_project_with_video(name="metadata-only")
    _make_ready(ctx, video_id)
    before = (settings.display_proxies_dir / "safe-proxy.mp4").read_bytes()
    with ctx.session_factory() as db:
        summary = inspect_display_proxy_readiness(db, settings)
        assert not db.new and not db.dirty and not db.deleted
    assert summary.passed
    assert (summary.file_backed_total, summary.ready_safe,
            summary.metadata_only_excluded) == (1, 1, 1)
    assert (settings.display_proxies_dir / "safe-proxy.mp4").read_bytes() == before

    output = []
    assert run_preflight(lambda: settings, output.append) == 0
    text = "\n".join(output)
    assert "PASS" in text and "source.mp4" not in text and "safe-proxy.mp4" not in text


def test_sqlite_engine_is_truly_read_only_and_target_check_still_succeeds(ctx):
    settings, video_id = _file_backed(ctx)
    _make_ready(ctx, video_id)
    engine = _create_preflight_engine(settings.resolved_database_url)
    try:
        with engine.connect() as connection:
            with pytest.raises(OperationalError, match="readonly"):
                connection.execute(text("INSERT INTO users (username) VALUES ('forbidden')"))
            with pytest.raises(OperationalError, match="readonly"):
                connection.execute(text("CREATE TABLE forbidden_preflight_write (id INTEGER)"))
    finally:
        engine.dispose()

    assert run_preflight(lambda: settings, lambda _line: None) == 0


def test_sqlite_read_only_mode_does_not_create_missing_database(tmp_path):
    missing = tmp_path / "must-not-be-created.db"
    engine = _create_preflight_engine(f"sqlite:///{missing.as_posix()}")
    try:
        with pytest.raises(OperationalError):
            with engine.connect():
                pass
    finally:
        engine.dispose()
    assert not missing.exists()


def test_preflight_reports_pending_and_active_job(ctx):
    settings, video_id = _file_backed(ctx)
    with ctx.session_factory() as db:
        video = db.get(Video, video_id)
        video.source_sha256 = "b" * 64
        enqueue_display_proxy(db, video)
        db.commit()
    output = []
    assert run_preflight(lambda: settings, output.append) == 1
    text = "\n".join(output)
    assert "pending: 1" in text and "active_display_jobs: 1" in text
    assert "b" * 64 not in text


def test_preflight_missing_and_bad_proxy_paths_are_invalid(ctx, tmp_path):
    settings, video_id = _file_backed(ctx)
    _make_ready(ctx, video_id)
    (settings.display_proxies_dir / "safe-proxy.mp4").unlink()
    with ctx.session_factory() as db:
        summary = inspect_display_proxy_readiness(db, settings)
    assert (summary.invalid, summary.ready_safe, summary.passed) == (1, 0, False)

    fake = SimpleNamespace(
        source_sha256="a" * 64, display_source_sha256="a" * 64,
        display_profile_version=DISPLAY_PROXY_PROFILE_VERSION, display_status="ready",
        display_path=str(tmp_path / "outside.mp4"),
    )
    (tmp_path / "outside.mp4").write_bytes(b"private")
    assert not ready_entity_is_safe(fake, settings)


def test_cli_configuration_failure_is_stable_and_redacted():
    output = []

    def fail():
        raise RuntimeError("postgres://user:secret@private/internal-name.mp4")

    assert run_preflight(fail, output.append) == 2
    assert output == [
        "Display proxy strict-mode preflight: ERROR (configuration or database unavailable)"
    ]


def test_cli_env_file_selects_database_without_leaking_values(ctx, tmp_path, monkeypatch):
    settings, video_id = _file_backed(ctx)
    _make_ready(ctx, video_id)
    secret = "never-print-this-secret"
    env_file = tmp_path / "selected-production.env"
    env_file.write_text(
        "\n".join((
            "ENV=testing",
            f"DATA_DIR={settings.data_dir.as_posix()}",
            f"DATABASE_URL={settings.resolved_database_url}",
            f"SECRET_KEY={secret}",
        )),
        encoding="utf-8",
    )
    for name in ("ENV", "DATA_DIR", "DATABASE_URL", "SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)

    output = []
    assert run_preflight(output=output.append, env_file=env_file) == 0
    rendered = "\n".join(output)
    assert "env=testing" in rendered
    assert "env_file=provided" in rendered
    assert "database=sqlite" in rendered
    assert "PASS" in rendered
    assert secret not in rendered
    assert str(env_file) not in rendered
    assert str(settings.data_dir) not in rendered
    assert settings.resolved_database_url not in rendered


def test_cli_bad_env_or_database_exits_two_without_creating_db(tmp_path, monkeypatch):
    output = []
    assert run_preflight(output=output.append, env_file=tmp_path / "missing.env") == 2
    assert output == [
        "Display proxy strict-mode preflight: ERROR (configuration or database unavailable)"
    ]

    missing_db = tmp_path / "missing.db"
    bad_env = tmp_path / "bad-target.env"
    bad_env.write_text(
        f"ENV=testing\nDATABASE_URL=sqlite:///{missing_db.as_posix()}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    output = []
    assert run_preflight(output=output.append, env_file=bad_env) == 2
    assert output == [
        "Display proxy strict-mode preflight: ERROR (configuration or database unavailable)"
    ]
    assert not missing_db.exists()
