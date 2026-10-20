"""Config overlay, task cleanup, pipeline hints, and HTTP retries."""

import os

import requests

from app.config.env_overlay import apply_environment_overrides
from app.services.pipeline_errors import explain_pipeline_error
from app.services.task_retention import cleanup_expired_tasks
from app.utils.http_retry import request_with_retries


def test_environment_overrides_do_not_echo_secret_values():
    app = {"api_key": "", "pexels_api_keys": []}
    telegram = {}
    applied = apply_environment_overrides(
        app,
        telegram,
        {
            "API_KEY": "super-secret",
            "PEXELS_API_KEYS": "pex-1, pex-2",
            "TELEGRAM_BOT_TOKEN": "123:ABC",
            "TELEGRAM_ALLOWED_USER_IDS": "10, 11",
            "PUBLIC_BASE_URL": "https://videos.example",
            "TASK_RETENTION_HOURS": "12",
            "TELEGRAM_DEFAULT_SUBTITLES": "false",
        },
    )
    assert app["api_key"] == "super-secret"
    assert app["pexels_api_keys"] == ["pex-1", "pex-2"]
    assert app["endpoint"] == "https://videos.example"
    assert app["task_retention_hours"] == 12
    assert telegram["bot_token"] == "123:ABC"
    assert telegram["allowed_user_ids"] == [10, 11]
    assert telegram["default_subtitles"] is False
    assert "super-secret" not in " ".join(applied)
    assert "API_KEY" in applied


def test_cleanup_removes_old_tasks_and_keeps_busy_ones(tmp_path):
    old = tmp_path / "old-task"
    fresh = tmp_path / "fresh-task"
    busy = tmp_path / "busy-task"
    outside = tmp_path / "outside"
    old.mkdir()
    fresh.mkdir()
    busy.mkdir()
    outside.mkdir()
    link = tmp_path / "escape"
    link.symlink_to(outside, target_is_directory=True)
    now = 1_000_000
    os.utime(old, (now - 10_000, now - 10_000))
    os.utime(fresh, (now - 10, now - 10))
    os.utime(busy, (now - 10_000, now - 10_000))
    deleted = []

    removed = cleanup_expired_tasks(
        task_root=str(tmp_path),
        max_age_hours=1,
        now=now,
        is_busy=lambda task_id: task_id == "busy-task",
        delete_state=deleted.append,
    )

    assert removed == ["old-task"]
    assert not old.exists()
    assert fresh.exists()
    assert busy.exists()
    assert outside.exists()
    assert deleted == ["old-task"]
    assert cleanup_expired_tasks(task_root=str(tmp_path), max_age_hours=0) == []


def test_pipeline_error_hints_name_the_fix():
    message = explain_pipeline_error(FileNotFoundError("ffmpeg not found"))
    assert "FileNotFoundError" in message
    assert "FFMPEG_PATH" in message
    plain = explain_pipeline_error(RuntimeError("something else broke"))
    assert plain == "RuntimeError: something else broke"


def test_request_retries_connection_errors_then_returns(monkeypatch):
    calls = {"count": 0}

    def flaky():
        calls["count"] += 1
        if calls["count"] < 3:
            raise requests.ConnectionError("reset")
        response = requests.Response()
        response.status_code = 200
        return response

    slept = []
    response = request_with_retries(flaky, attempts=3, backoff=0.5, sleep=slept.append)
    assert response.status_code == 200
    assert calls["count"] == 3
    assert slept == [0.5, 1.0]


def test_request_retries_http_429(monkeypatch):
    codes = iter([429, 200])

    def fetch():
        response = requests.Response()
        response.status_code = next(codes)
        return response

    response = request_with_retries(fetch, attempts=3, backoff=0, sleep=lambda _seconds: None)
    assert response.status_code == 200
