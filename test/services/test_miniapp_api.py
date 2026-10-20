"""Mini App HTTP endpoints with the render pipeline replaced by a fake."""

from fastapi.testclient import TestClient

from app.controllers.v1 import miniapp
from app.telegram.runtime import get_job_store, reset_runtime


class _FakePipeline:
    def __init__(self):
        self.tasks = {}

    def submit(self, owner, topic, options):
        task_id = "task-mini"
        self.tasks[task_id] = {
            "owner": owner,
            "topic": topic,
            "options": options,
            "state": 4,
            "progress": 15,
        }
        get_job_store().bind(task_id, owner)
        return task_id

    def snapshot(self, task_id, owner):
        task = self.tasks[task_id]
        return {
            "task_id": task_id,
            "state": task["state"],
            "progress": task["progress"],
            "stage": "Writing the script",
            "error": None,
            "failed_stage": None,
            "files": [],
        }


def _client(monkeypatch):
    reset_runtime()
    fake = _FakePipeline()
    monkeypatch.setattr(miniapp, "get_pipeline", lambda: fake)
    monkeypatch.setattr(miniapp, "_settings", lambda: _open_settings())
    return TestClient(miniapp_app())


def _open_settings():
    from app.telegram.settings import TelegramSettings

    return TelegramSettings(
        bot_token="",
        web_password="",
        api_key="",
        service_secret="",
        rate_limit_per_hour=10,
        default_aspect="9:16",
        default_voice="en-US-AriaNeural",
        default_paragraphs=1,
        default_language="",
        default_subtitles=True,
        video_source="pexels",
    )


def miniapp_app():
    from app.asgi import app

    return app


def test_miniapp_page_and_bootstrap_load():
    client = TestClient(miniapp_app())
    page = client.get("/miniapp")
    assert page.status_code == 200
    assert "Make a short video" in page.text
    assert "MoneyPrinterTurbo" in page.text

    bootstrap = client.get("/api/v1/miniapp/bootstrap")
    assert bootstrap.status_code == 200
    body = bootstrap.json()["data"]
    assert body["aspects"]
    assert body["voices"]
    assert "auth_required" in body


def test_create_and_read_task_for_an_open_local_studio(monkeypatch):
    client = _client(monkeypatch)
    created = client.post(
        "/api/v1/miniapp/videos",
        json={
            "video_subject": "a quiet bookstore",
            "video_aspect": "9:16",
            "voice_name": "en-US-AriaNeural",
            "paragraph_number": 1,
            "video_language": "",
            "subtitle_enabled": True,
        },
    )
    assert created.status_code == 200
    task_id = created.json()["data"]["task_id"]
    assert task_id == "task-mini"

    status = client.get(f"/api/v1/miniapp/tasks/{task_id}")
    assert status.status_code == 200
    assert status.json()["data"]["progress"] == 15

    missing = client.get("/api/v1/miniapp/tasks/someone-elses-video")
    assert missing.status_code == 404


def test_password_is_required_when_configured(monkeypatch):
    reset_runtime()
    from app.telegram.settings import TelegramSettings

    locked = TelegramSettings(
        web_password="studio-pass",
        bot_token="",
        api_key="",
        service_secret="",
        rate_limit_per_hour=5,
    )
    fake = _FakePipeline()
    monkeypatch.setattr(miniapp, "get_pipeline", lambda: fake)
    monkeypatch.setattr(miniapp, "_settings", lambda: locked)
    client = TestClient(miniapp_app())
    rejected = client.post(
        "/api/v1/miniapp/videos",
        json={"video_subject": "a quiet bookstore"},
    )
    assert rejected.status_code == 401

    accepted = client.post(
        "/api/v1/miniapp/videos",
        headers={"X-Web-Password": "studio-pass"},
        json={
            "video_subject": "a quiet bookstore",
            "video_aspect": "9:16",
            "voice_name": "en-US-AriaNeural",
            "paragraph_number": 1,
            "video_language": "",
            "subtitle_enabled": False,
        },
    )
    assert accepted.status_code == 200


def test_webhook_rejects_a_bad_secret(monkeypatch):
    from app.telegram.settings import TelegramSettings

    settings = TelegramSettings(
        bot_token="123:ABC",
        mode="webhook",
        webhook_secret="expected-secret",
        enabled=True,
    )
    monkeypatch.setattr(
        "app.controllers.telegram_http.load_telegram_settings",
        lambda: settings,
    )
    client = TestClient(miniapp_app())
    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "nope"},
        json={"update_id": 1},
    )
    assert response.status_code == 403


def test_signed_download_rejects_a_bad_token(monkeypatch):
    from app.telegram.settings import TelegramSettings

    monkeypatch.setattr(
        miniapp,
        "_settings",
        lambda: TelegramSettings(media_token_secret="media-secret", bot_token=""),
    )
    client = TestClient(miniapp_app())
    response = client.get("/api/v1/media/task-1?access=not-a-token")
    assert response.status_code == 401
