"""Process-wide bot conversation, limiter, and stores."""

from __future__ import annotations

import os
import threading

from app.telegram.logic import Conversation
from app.telegram.rate_limit import RateLimiter
from app.telegram.settings import TelegramSettings, load_telegram_settings
from app.telegram.store import JobStore, SessionStore
from app.utils import utils

_lock = threading.RLock()
_conversation: Conversation | None = None
_limiter: RateLimiter | None = None
_jobs: JobStore | None = None
_sessions: SessionStore | None = None


def storage_path(*parts: str) -> str:
    return os.path.join(utils.storage_dir("telegram", create=True), *parts)


def get_job_store() -> JobStore:
    global _jobs
    with _lock:
        if _jobs is None:
            _jobs = JobStore(storage_path("jobs.json"))
        return _jobs


def get_session_store() -> SessionStore:
    global _sessions
    with _lock:
        if _sessions is None:
            _sessions = SessionStore(storage_path("sessions.json"))
        return _sessions


def get_rate_limiter(settings: TelegramSettings | None = None) -> RateLimiter:
    global _limiter
    settings = settings or load_telegram_settings()
    with _lock:
        if _limiter is None or _limiter.limit != settings.rate_limit_per_hour:
            _limiter = RateLimiter(settings.rate_limit_per_hour)
        return _limiter


def _publisher(local: bool):
    settings = load_telegram_settings()
    if local:
        from app.telegram.publish_local import LocalPublisher

        return LocalPublisher(settings.media_secret())
    from app.telegram.publish_client import HttpPublisher

    return HttpPublisher(settings)


def _build(pipeline, *, local: bool) -> Conversation:
    settings = load_telegram_settings()
    return Conversation(
        settings=settings,
        sessions=get_session_store(),
        rate_limiter=get_rate_limiter(settings),
        pipeline=pipeline,
        publisher=_publisher(local),
    )


def get_conversation(pipeline=None) -> Conversation:
    """Return the polling-bot conversation. Pass a pipeline to install one."""

    global _conversation
    with _lock:
        if _conversation is None:
            if pipeline is None:
                from app.telegram.http_pipeline import HttpPipeline

                pipeline = HttpPipeline(load_telegram_settings())
            _conversation = _build(pipeline, local=False)
        return _conversation


def get_api_conversation() -> Conversation:
    """Conversation used by the API webhook. It runs the pipeline in-process."""

    global _conversation
    from app.telegram.inprocess import InProcessPipeline

    with _lock:
        pipeline = getattr(_conversation, "pipeline", None)
        if _conversation is None or not isinstance(pipeline, InProcessPipeline):
            _conversation = _build(InProcessPipeline(), local=True)
        return _conversation


def reset_runtime() -> None:
    global _conversation, _limiter, _jobs, _sessions
    with _lock:
        _conversation = None
        _limiter = None
        _jobs = None
        _sessions = None
