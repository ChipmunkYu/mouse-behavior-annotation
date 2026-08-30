"""Settings 的环境相关安全约束。"""

import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from app.config import Settings


VALID_PRODUCTION = {
    "env": "production",
    "secret_key": "a-production-secret-with-at-least-32-chars",
    "demo_username": "annotation-admin",
    "demo_password": "strong-password-2026",
    "media_master_secret": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
}


def test_valid_production_credentials_are_accepted():
    settings = Settings(**VALID_PRODUCTION)
    assert settings.env == "production"


@pytest.mark.parametrize(
    "overrides",
    [
        {"secret_key": "dev-only-insecure-secret-change-me"},
        {"secret_key": "CHANGE_ME_GENERATE_A_STRONG_RANDOM_SECRET"},
        {"secret_key": "safe-prefix-PLACEHOLDER-value-that-is-long"},
        {"secret_key": "too-short"},
        {"demo_username": "demo"},
        {"demo_username": "CHANGE_ME_PRODUCTION_USERNAME"},
        {"demo_password": "demo123"},
        {"demo_password": "CHANGE_ME_PASSWORD"},
        {"demo_password": "short-pass"},
        {"media_master_secret": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"},
        {"media_master_secret": "CHANGE_ME_MEDIA_SECRET"},
        {"media_master_secret": "dG9vLXNob3J0"},
        {"media_master_secret": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="},
    ],
)
def test_invalid_production_credentials_fail_fast(overrides):
    with pytest.raises(ValidationError, match="invalid production credentials"):
        Settings(**(VALID_PRODUCTION | overrides))


def test_development_defaults_remain_available():
    settings = Settings(env="development")
    assert settings.secret_key == "dev-only-insecure-secret-change-me"
    assert settings.demo_password == "demo123"
    assert settings.media_ticket_enabled is False
    assert settings.media_legacy_bearer_enabled is True
    assert settings.media_ticket_ttl_seconds == 7200
    assert settings.media_binding_cookie_path == "/api/videos/"


@pytest.mark.parametrize("field,value", [
    ("media_ticket_cookie_name", ""),
    ("media_ticket_cookie_name", "mouse_media_binding"),
    ("media_binding_cookie_name", "mouse_media_ticket"),
    ("media_binding_cookie_path", "/"),
    ("media_binding_cookie_path", "/api/"),
    ("media_ticket_audience", "video-stream-binding"),
    ("media_binding_audience", "video-stream"),
    ("media_ticket_type", "media-binding"),
    ("media_binding_type", "media-ticket"),
])
def test_media_security_identifiers_are_fixed(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_media_ticket_ttl_above_hard_limit_is_rejected():
    with pytest.raises(ValidationError):
        Settings(media_ticket_ttl_seconds=7201)


def test_migrate_cli_rejects_invalid_production_credentials(tmp_path):
    """显式迁移同样经过 Settings，并在接触数据库前拒绝模板凭据。"""
    backend_dir = Path(__file__).resolve().parent.parent
    database = tmp_path / "must-not-be-created.db"
    env = dict(os.environ)
    env.update(
        ENV="production",
        SECRET_KEY="CHANGE_ME_GENERATE_A_STRONG_RANDOM_SECRET",
        DEMO_USERNAME="annotation-admin",
        DEMO_PASSWORD="strong-password-2026",
    )
    proc = subprocess.run(
        [sys.executable, "scripts/migrate.py", "--check", "--db-url", f"sqlite:///{database.as_posix()}"],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
    )
    assert proc.returncode != 0
    assert "invalid production credentials" in proc.stderr
    assert not database.exists()
