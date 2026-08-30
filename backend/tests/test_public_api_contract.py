from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import database as db_mod
from app.config import Settings
from app.factory import create_schema_app
from app.main import create_app
from app.models import Annotation, Clip, Video
from app.routers.clips import _to_item
from app.schemas import JobOut, _sanitize_public_value
from app.video_playback import resolve_video_playback
from scripts.export_openapi import OUTPUT, rendered_openapi

from .conftest import auth_headers


FORBIDDEN = {
    "storage_path", "display_path", "clip_path", "thumbnail_path", "result_path",
    "video_path", "tracks_path", "metadata_path", "thumbnail_available",
}


def _property_names(node):
    if isinstance(node, dict):
        yield from node.get("properties", {}).keys()
        for value in node.values():
            yield from _property_names(value)
    elif isinstance(node, list):
        for value in node:
            yield from _property_names(value)


def test_openapi_snapshot_and_forbidden_public_fields():
    assert OUTPUT.read_text(encoding="utf-8") == rendered_openapi()
    schema = create_schema_app().openapi()
    assert FORBIDDEN.isdisjoint(set(_property_names(schema)))
    rendered = str(schema).lower()
    assert not any(term in rendered for term in FORBIDDEN)


def test_cold_schema_import_and_export_have_no_runtime_side_effects(tmp_path):
    backend = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(backend)}
    before = set(tmp_path.iterdir())
    subprocess.run(
        [sys.executable, "-c", "from app.factory import create_schema_app; create_schema_app().openapi()"],
        cwd=tmp_path, env=env, check=True,
    )
    subprocess.run(
        [sys.executable, str(backend / "scripts" / "export_openapi.py"), "--check"],
        cwd=tmp_path, env=env, check=True,
    )
    assert set(tmp_path.iterdir()) == before


def test_runtime_and_schema_surfaces_match(tmp_path):
    settings = Settings(
        env="test", data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'surface.db').as_posix()}",
        cleanup_enabled=False,
    )
    runtime = create_app(settings=settings).openapi()
    schema = create_schema_app().openapi()
    assert runtime["paths"] == schema["paths"]
    assert runtime["components"]["schemas"] == schema["components"]["schemas"]


def test_video_response_uses_only_playback_status(ctx):
    setup = ctx.make_project_with_video()
    body = setup["video"]
    assert body["playback_status"] == "unavailable"
    assert FORBIDDEN.isdisjoint(body)

    uploaded = ctx.client.post(
        f"/api/projects/{setup['project']['id']}/videos/upload",
        files={"file": ("play.mp4", b"PLAY", "video/mp4")}, headers=setup["headers"],
    ).json()
    assert uploaded["playback_status"] == "ready"
    listed = ctx.client.get(
        f"/api/projects/{setup['project']['id']}/videos", headers=setup["headers"],
    ).json()
    assert next(item for item in listed if item["id"] == uploaded["id"])["playback_status"] == "ready"
    settings = ctx.client.app.state.settings
    settings.display_proxies_enabled = True
    settings.display_proxy_allow_source_fallback = False
    strict = ctx.client.get(
        f"/api/projects/{setup['project']['id']}/videos", headers=setup["headers"],
    ).json()
    assert next(item for item in strict if item["id"] == uploaded["id"])["playback_status"] == "pending"
    assert ctx.client.get(f"/api/videos/{uploaded['id']}/stream", headers=setup["headers"]).status_code == 404
    settings.media_ticket_enabled = True
    assert ctx.client.post(
        f"/api/videos/{uploaded['id']}/stream-ticket", headers=setup["headers"],
    ).status_code == 404


def test_suppressions_without_active_import_remain_empty(ctx):
    setup = ctx.make_project_with_video()
    response = ctx.client.get(
        f"/api/projects/{setup['project']['id']}/videos/{setup['video']['id']}/detection-suppressions",
        headers=setup["headers"],
    )
    assert response.status_code == 200
    assert response.json() == []


def test_playback_resolver_default_fallback_and_strict_proxy(tmp_path):
    videos, proxies = tmp_path / "videos", tmp_path / "proxies"
    videos.mkdir(); proxies.mkdir()
    (videos / "source.mp4").write_bytes(b"SOURCE")
    video = SimpleNamespace(
        storage_path="source.mp4", status="uploaded",
        display_path=None, display_status="pending",
    )
    settings = SimpleNamespace(
        videos_dir=videos, display_proxies_dir=proxies,
        display_proxies_enabled=False, display_proxy_allow_source_fallback=True,
    )
    assert resolve_video_playback(video, settings).status == "ready"
    settings.display_proxies_enabled = True
    assert resolve_video_playback(video, settings).status == "ready"
    settings.display_proxy_allow_source_fallback = False
    assert resolve_video_playback(video, settings).status == "pending"
    video.display_status = "failed"
    assert resolve_video_playback(video, settings).status == "failed"
    video.display_status, video.display_path = "ready", "proxy.mp4"
    assert resolve_video_playback(video, settings).status == "pending"
    (proxies / "proxy.mp4").write_bytes(b"PROXY")
    assert resolve_video_playback(video, settings).status == "ready"
    (videos / "source.mp4").unlink()
    assert resolve_video_playback(video, settings).status == "ready"
    (proxies / "proxy.mp4").unlink()
    assert resolve_video_playback(video, settings).status == "unavailable"


def test_job_diagnostics_redact_absolute_paths():
    job = JobOut(
        id=1, job_type="media", status="failed", progress=0,
        error="ffmpeg failed at /srv/private/video.mp4",
        created_at=datetime.utcnow(),
    ).model_dump()
    assert job["error"] == "processing_failed"
    assert "payload" not in job
    diagnostic = {
        "nested": [
            r"failed C:\private folder\video clip.mp4",
            {"unc": r"\\server\private share\mouse clip.mp4"},
            "failed /srv/private folder/video clip.mp4",
        ]
    }
    sanitized = _sanitize_public_value(diagnostic)
    assert "private" not in str(sanitized)
    assert "server" not in str(sanitized)


def test_clip_item_key_separates_legacy_and_submission_id_spaces(tmp_path):
    values = dict(
        clip_id=None, annotation_id=7, video_id=1, video_filename="v.mp4",
        category_id=1, category_name="grooming", start_time=0.0, end_time=1.0,
        start_frame=0, end_frame=25, confidence="certain", media_status="pending",
        annotator_name=None, review_status="approved", created_at=datetime.utcnow(),
        category_group=None, category_participant_mode="unordered",
        role_definitions=[], participant_roles={}, mouse_ids=[],
        clip_path=None, thumbnail_path=None,
    )
    settings = SimpleNamespace(clips_dir=tmp_path, thumbnails_dir=tmp_path)
    legacy = _to_item(SimpleNamespace(authority_type="legacy", **values), settings)
    submission = _to_item(SimpleNamespace(authority_type="submission", **values), settings)
    assert legacy.item_key == "legacy:7"
    assert submission.item_key == "submission:7"
    assert legacy.item_key != submission.item_key


def _ready_clip(ctx):
    setup = ctx.make_project_with_video()
    headers, project, video = setup["headers"], setup["project"], setup["video"]
    annotation = ctx.client.post(
        f"/api/projects/{project['id']}/videos/{video['id']}/annotations",
        json={"category_id": setup["categories"][0]["id"], "start_frame": 0,
              "end_frame": 25, "start_time": 0, "end_time": 1},
        headers=headers,
    ).json()
    with ctx.session_factory() as db:
        row = db.get(Video, video["id"])
        row.workflow_status = "approved"
        ann = db.get(Annotation, annotation["id"])
        ann.review_status = "approved"
        clip = Clip(
            project_id=project["id"], annotation_id=ann.id,
            source_revision=row.media_revision, status="ready",
            clip_path="clip.mp4", thumbnail_path="thumb.jpg",
        )
        db.add(clip)
        db.commit()
        clip_id = clip.id
    ctx.client.app.state.settings.thumbnails_dir.joinpath("thumb.jpg").write_bytes(b"JPEG")
    ctx.client.app.state.settings.clips_dir.joinpath("clip.mp4").write_bytes(b"CLIP")
    return setup, clip_id


def test_thumbnail_authorization_ownership_status_and_safe_path(ctx, login_headers, tmp_path):
    setup, clip_id = _ready_clip(ctx)
    project_id, headers = setup["project"]["id"], setup["headers"]
    url = f"/api/projects/{project_id}/clips/{clip_id}/thumbnail"
    response = ctx.client.get(url, headers=headers)
    assert response.status_code == 200 and response.content == b"JPEG"
    assert "thumbnail.jpg" in response.headers["content-disposition"]
    assert "thumb.jpg" not in response.headers["content-disposition"]
    item = ctx.client.get(f"/api/projects/{project_id}/clips", headers=headers).json()["items"][0]
    assert item["clip_id"] == clip_id and item["media_status"] == "ready"
    assert FORBIDDEN.isdisjoint(item)
    clip_file = ctx.client.app.state.settings.clips_dir / "clip.mp4"
    clip_file.unlink()
    degraded = ctx.client.get(f"/api/projects/{project_id}/clips", headers=headers).json()["items"][0]
    assert degraded["media_status"] == "pending"
    assert ctx.client.get(url, headers=headers).status_code == 404
    clip_file.write_bytes(b"CLIP")
    assert ctx.client.get(url).status_code == 401

    ctx.create_user("outsider")
    assert ctx.client.get(url, headers=login_headers("outsider", "pw123")).status_code == 403
    other = ctx.client.post("/api/projects", json={"name": "other"}, headers=headers).json()
    assert ctx.client.get(
        f"/api/projects/{other['id']}/clips/{clip_id}/thumbnail", headers=headers,
    ).status_code == 404

    with ctx.session_factory() as db:
        clip = db.get(Clip, clip_id)
        clip.status = "pending"
        clip.thumbnail_path = None
        db.commit()
    assert ctx.client.get(url, headers=headers).status_code == 404

    outside = tmp_path.parent / "outside-thumbnail.jpg"
    outside.write_bytes(b"SECRET")
    for bad_path in (str(outside), "..\\outside-thumbnail.jpg", "."):
        with ctx.session_factory() as db:
            clip = db.get(Clip, clip_id)
            clip.status = "ready"
            clip.thumbnail_path = bad_path
            db.commit()
        assert ctx.client.get(url, headers=headers).status_code == 404

    with ctx.session_factory() as db:
        db.get(Clip, clip_id).thumbnail_path = "missing.jpg"
        db.commit()
    assert ctx.client.get(url, headers=headers).status_code == 404
