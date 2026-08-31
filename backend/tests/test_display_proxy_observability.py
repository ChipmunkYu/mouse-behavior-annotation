import json
import logging
from types import SimpleNamespace

from app import video_playback
from app.display_proxy_jobs import DisplayProxyWorker
from app.display_proxy_observability import EVENT_LOGGER_NAME, log_display_event
from app.display_proxy_processor import DisplayProxyError
from app.models import Video
from app.video_playback import PlaybackResolution, observe_strict_playback
from tests.test_display_proxy_jobs import FakeDisplayProcessor, _queued


def _events(caplog):
    return [json.loads(record.message) for record in caplog.records
            if record.message.startswith("{") and "event" in record.message]


def test_event_emission_does_not_reconfigure_logger():
    logger = logging.getLogger(EVENT_LOGGER_NAME)
    handler = logging.NullHandler()
    original = (logger.disabled, logger.level, logger.propagate, list(logger.handlers))
    try:
        logger.disabled = True
        logger.setLevel(logging.ERROR)
        logger.propagate = False
        logger.handlers[:] = [handler]
        before = (logger.disabled, logger.level, logger.propagate, list(logger.handlers))

        log_display_event(logging.INFO, "state_preservation", video_id=1,
                          forbidden_secret="never emitted")

        assert (logger.disabled, logger.level, logger.propagate,
                list(logger.handlers)) == before
    finally:
        logger.disabled, logger.level, logger.propagate = original[:3]
        logger.handlers[:] = original[3]


def test_application_initialization_restores_single_propagating_logger(ctx, caplog):
    logger = logging.getLogger(EVENT_LOGGER_NAME)
    assert logger.disabled is False
    assert logger.level == logging.NOTSET
    assert logger.propagate is True
    assert logger.handlers == []

    caplog.set_level(logging.INFO)
    log_display_event(logging.INFO, "initialized", video_id=7)
    matching = [record for record in caplog.records if record.name == EVENT_LOGGER_NAME
                and json.loads(record.message).get("event") == "initialized"]
    assert len(matching) == 1


def test_operator_changes_after_initialization_are_respected(ctx, caplog):
    logger = logging.getLogger(EVENT_LOGGER_NAME)
    caplog.set_level(logging.DEBUG)
    logger.setLevel(logging.ERROR)
    before = (logger.disabled, logger.level, logger.propagate, list(logger.handlers))

    log_display_event(logging.INFO, "operator_filtered", video_id=8)

    assert not any(item["event"] == "operator_filtered" for item in _events(caplog))
    assert (logger.disabled, logger.level, logger.propagate,
            list(logger.handlers)) == before

    logger.disabled = True
    disabled_before = (logger.disabled, logger.level, logger.propagate, list(logger.handlers))
    log_display_event(logging.ERROR, "operator_disabled", video_id=9)
    assert not any(item["event"] == "operator_disabled" for item in _events(caplog))
    assert (logger.disabled, logger.level, logger.propagate,
            list(logger.handlers)) == disabled_before


def test_worker_events_are_structured_and_redacted(ctx, caplog):
    caplog.set_level(logging.INFO)
    settings, source, digest, job_id, video_id, _key = _queued(ctx)

    class SecretFailure:
        def render(self, **_kwargs):
            raise DisplayProxyError(
                f"Authorization=Bearer secret Cookie=ticket; {source}; {digest}; raw-stderr")

    worker = DisplayProxyWorker(processor=SecretFailure(),
                                session_factory=ctx.session_factory, settings=settings)
    worker.start()
    worker.shutdown()
    events = _events(caplog)
    assert all(record.name == EVENT_LOGGER_NAME for record in caplog.records
               if record.message.startswith("{") and "event" in record.message)
    assert {item["event"] for item in events} >= {
        "display_proxy_enqueue", "display_proxy_claim", "display_proxy_failed"
    }
    allowed = {"event", "job_id", "video_id", "project_id", "profile", "status",
               "elapsed_ms", "bytes", "error_category", "source_match"}
    assert all(set(item) <= allowed for item in events)
    rendered = "\n".join(record.message for record in caplog.records)
    for secret in ("Authorization", "Cookie", "ticket", str(source), digest, "raw-stderr"):
        assert secret not in rendered


def test_ready_and_strict_playback_events_include_only_safe_state(ctx, caplog):
    settings, _source, _digest, job_id, video_id, _key = _queued(ctx)
    caplog.set_level(logging.DEBUG)
    worker = DisplayProxyWorker(processor=FakeDisplayProcessor(),
                                session_factory=ctx.session_factory, settings=settings)
    worker.start()
    worker.shutdown()
    with ctx.session_factory() as db:
        video = db.get(Video, video_id)
        observe_strict_playback(video, PlaybackResolution("pending"))
        observe_strict_playback(video, PlaybackResolution("failed"))
        observe_strict_playback(video, PlaybackResolution("ready"))
    events = _events(caplog)
    assert {item["event"] for item in events} >= {
        "display_proxy_ready", "strict_playback_ready",
        "strict_playback_pending", "strict_playback_failed",
    }
    ready = next(item for item in events if item["event"] == "display_proxy_ready")
    assert ready["job_id"] == job_id and ready["bytes"] == 5


def test_strict_playback_lru_evicts_one_entry_without_relogging_all(monkeypatch):
    emitted = []
    monkeypatch.setattr(video_playback, "_OBSERVED_LIMIT", 3)
    monkeypatch.setattr(video_playback, "log_display_event",
                        lambda level, event, **fields: emitted.append((event, fields["video_id"])))
    with video_playback._observed_lock:
        video_playback._observed.clear()

    def video(video_id):
        return SimpleNamespace(id=video_id, project_id=1, source_sha256="source",
                               display_source_sha256="source",
                               display_profile_version=video_playback.DISPLAY_PROXY_PROFILE_VERSION)

    resolution = PlaybackResolution("pending")
    for video_id in (1, 2, 3):
        observe_strict_playback(video(video_id), resolution)
    observe_strict_playback(video(1), resolution)
    observe_strict_playback(video(4), resolution)
    for video_id in (1, 3, 4):
        observe_strict_playback(video(video_id), resolution)

    assert emitted == [("strict_playback_pending", video_id) for video_id in (1, 2, 3, 4)]
    assert list(video_playback._observed) == [1, 3, 4]
