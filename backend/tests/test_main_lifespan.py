import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_later_worker_start_failure_shuts_down_display_worker(tmp_path):
    settings = Settings(
        env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'lifespan.db').as_posix()}",
        display_proxies_enabled=True,
        display_proxy_synchronous=True,
        media_synchronous=True,
        cleanup_enabled=False,
    )
    app = create_app(settings=settings)
    display = app.state.display_proxy_worker
    calls = []
    display.start = lambda: calls.append("display-start")
    display.shutdown = lambda: calls.append("display-shutdown")

    def fail_media_start(*, recover):
        assert recover is False
        calls.append("media-start")
        raise RuntimeError("media startup failed")

    app.state.media_worker.start = fail_media_start
    with pytest.raises(RuntimeError, match="media startup failed"):
        with TestClient(app):
            pass
    assert calls == ["display-start", "media-start", "display-shutdown"]
