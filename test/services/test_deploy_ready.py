"""Oracle deploy profile: storage limits, low memory, and the generic LLM."""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.config.env_overlay import apply_environment_overrides
from app.models.schema import VideoAspect
from app.runtime.profile import encode_threads, ffmpeg_extra_params, fit_resolution
from app.services.llm_check import check_llm_configuration
from app.services.storage_hygiene import (
    StoragePolicy,
    cleanup_storage,
    format_report,
    release_task_media,
)
from app.telegram.logic import Conversation
from app.telegram.rate_limit import RateLimiter
from app.telegram.settings import TelegramSettings
from app.telegram.store import SessionStore
from app.telegram.updates import dispatch_update


ROOT = Path(__file__).resolve().parents[2]


def _policy(**overrides) -> StoragePolicy:
    base = dict(
        retention_hours=24,
        cache_max_bytes=100,
        cap_bytes=10_000_000,
        delete_after_delivery=False,
        temp_max_age_hours=6,
    )
    base.update(overrides)
    return StoragePolicy(**base)


def _age(path: Path, seconds: float) -> None:
    stamp = datetime.now().timestamp() - seconds
    os.utime(path, (stamp, stamp))


def test_purge_drops_videos_and_keeps_oauth_tokens(tmp_path):
    task = tmp_path / "tasks" / "old-video"
    task.mkdir(parents=True)
    (task / "final.mp4").write_bytes(b"v" * 2000)
    _age(task, 5000)
    _age(task / "final.mp4", 5000)
    cache = tmp_path / "cache_videos"
    cache.mkdir()
    clip = cache / "vid-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.mp4"
    clip.write_bytes(b"c" * 500)
    _age(clip, 5000)
    accounts = tmp_path / "publish"
    accounts.mkdir()
    token_file = accounts / "accounts.json"
    token_file.write_text('{"accounts":{"y1":{"refresh_token":"keep-this-token"}}}\n', encoding="utf-8")
    jobs = accounts / "jobs.json"
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    jobs.write_text(
        '{"jobs":{"j1":{"state":"complete","updated_at":"%s"},'
        '"j2":{"state":"queued","updated_at":"%s"}}}\n' % (old, old),
        encoding="utf-8",
    )

    report = cleanup_storage(str(tmp_path), _policy(), purge=True, is_busy=lambda _task: False)

    assert not task.exists()
    assert "old-video" in report["removed_tasks"]
    assert not clip.exists()
    assert "keep-this-token" in token_file.read_text(encoding="utf-8")
    kept = jobs.read_text(encoding="utf-8")
    assert "j2" in kept
    assert "j1" not in kept
    assert "OAuth account tokens are kept." in format_report(report)


def test_cache_lru_and_storage_cap_delete_oldest_first(tmp_path):
    cache = tmp_path / "cache_videos"
    cache.mkdir()
    older = cache / "old.mp4"
    newer = cache / "new.mp4"
    older.write_bytes(b"a" * 80)
    newer.write_bytes(b"b" * 80)
    _age(older, 5000)
    _age(newer, 4000)
    task = tmp_path / "tasks" / "huge"
    task.mkdir(parents=True)
    (task / "film.mp4").write_bytes(b"z" * 5000)
    _age(task, 5000)

    cleanup_storage(
        str(tmp_path),
        _policy(retention_hours=1000, cache_max_bytes=100, cap_bytes=2000),
        is_busy=lambda task_id: task_id == "busy",
    )

    assert not older.exists()
    assert newer.exists()
    assert not task.exists()


def test_busy_task_survives_the_cap(tmp_path):
    task = tmp_path / "tasks" / "busy"
    task.mkdir(parents=True)
    (task / "film.mp4").write_bytes(b"z" * 5000)
    _age(task, 5000)
    cleanup_storage(
        str(tmp_path),
        _policy(retention_hours=1, cap_bytes=100),
        is_busy=lambda task_id: task_id == "busy",
    )
    assert task.exists()


def test_delete_after_delivery_waits_for_every_publish(tmp_path):
    task = tmp_path / "tasks" / "clip"
    task.mkdir(parents=True)
    (task / "final.mp4").write_text("video", encoding="utf-8")
    policy = _policy(delete_after_delivery=True)
    assert release_task_media(
        "clip",
        jobs=[{"state": "queued", "task_id": "clip"}],
        policy=policy,
        root=str(tmp_path),
        require_all_published=False,
        is_busy=lambda _task: False,
    ) is False
    assert task.exists()
    assert release_task_media(
        "clip",
        jobs=[{"state": "complete", "task_id": "clip"}],
        policy=policy,
        root=str(tmp_path),
        require_all_published=True,
        is_busy=lambda _task: False,
    ) is True
    assert not task.exists()


def test_low_memory_uses_720p_and_a_fast_preset(monkeypatch):
    monkeypatch.setenv("MPT_LOW_MEMORY", "true")
    assert fit_resolution(1080, 1920) == (720, 1280)
    assert VideoAspect.portrait.to_resolution() == (720, 1280)
    assert encode_threads(4) == 1
    params = ffmpeg_extra_params(["-vf", "scale"])
    assert params[:4] == ["-preset", "veryfast", "-crf", "28"]
    monkeypatch.setenv("MPT_LOW_MEMORY", "false")
    assert VideoAspect.portrait.to_resolution() == (1080, 1920)
    assert encode_threads(4) == 4


def test_generic_llm_env_maps_onto_oneapi_and_lists_the_model():
    app = {"llm_provider": "moonshot"}
    applied = apply_environment_overrides(
        app,
        {},
        {
            "LLM_API_KEY": "relay-key",
            "LLM_BASE_URL": "https://relay.example/v1/",
            "LLM_MODEL": "studio-model",
        },
    )
    assert app["llm_provider"] == "oneapi"
    assert app["oneapi_api_key"] == "relay-key"
    assert app["oneapi_base_url"] == "https://relay.example/v1"
    assert app["oneapi_model_name"] == "studio-model"
    assert "relay-key" not in " ".join(applied)

    def fetch(url, headers):
        assert url == "https://relay.example/v1/models"
        assert headers["Authorization"] == "Bearer relay-key"
        return 200, '{"data":[{"id":"other"},{"id":"studio-model"}]}'

    report = check_llm_configuration(app, fetch=fetch)
    assert report["status"] == "ok"
    assert report["model"] == "studio-model"
    assert "relay-key" not in report["message"]

    missing = check_llm_configuration(app, fetch=lambda _url, _headers: (200, '{"data":[{"id":"other"}]}'))
    assert missing["status"] == "error"
    assert "studio-model" in missing["message"]
    assert "other" in missing["message"]

    rejected = check_llm_configuration(app, fetch=lambda _url, _headers: (401, "no"))
    assert rejected["status"] == "error"
    assert "401" in rejected["message"]
    assert "relay-key" not in rejected["message"]


def test_storage_command_and_admin_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.storage_hygiene.report_storage",
        lambda: {
            "total_bytes": 10,
            "cap_bytes": 20,
            "retention_hours": 24,
            "categories": {},
            "delete_after_delivery": False,
        },
    )
    monkeypatch.setattr(
        "app.services.storage_hygiene.purge_storage",
        lambda: {
            "total_bytes": 0,
            "cap_bytes": 20,
            "retention_hours": 24,
            "removed_tasks": ["gone"],
            "categories": {},
            "delete_after_delivery": False,
        },
    )
    settings = TelegramSettings(bot_token="1:TEST", admin_user_ids=frozenset({7}))
    conversation = Conversation(
        settings=settings,
        sessions=SessionStore(str(tmp_path / "sessions.json")),
        rate_limiter=RateLimiter(0),
        pipeline=None,
    )
    denied = conversation.on_storage(3, "")
    assert "admin" in denied.text.lower()
    usage = conversation.on_storage(7, "")
    assert "Storage uses" in usage.text
    purged = conversation.on_storage(7, "purge")
    assert "Purged 1" in purged.text

    sent = []

    def sender(method, payload):
        sent.append((method, payload))

    dispatch_update(
        {
            "message": {
                "text": "/storage purge",
                "from": {"id": 7},
                "chat": {"id": 7, "type": "private"},
            }
        },
        conversation,
        sender,
    )
    assert "Purged 1" in sent[-1][1]["text"]

    from app.asgi import app

    client = TestClient(app)
    response = client.get("/api/v1/admin/storage")
    assert response.status_code == 200
    assert "summary" in response.json()["data"]
    posted = client.post("/api/v1/admin/storage", json={"action": "purge"})
    assert posted.status_code == 200
    assert posted.json()["data"]["removed_tasks"] == ["gone"]


def test_oracle_artifacts_are_wired():
    script = (ROOT / "scripts" / "deploy_oracle.sh").read_text(encoding="utf-8")
    assert "moneyprinterturbo" in script
    assert "docker system prune -a" in script
    assert "fallocate -l 2G /swapfile" in script
    assert "0.0.0.0" in script
    workflow = (ROOT / ".github" / "workflows" / "docker-ghcr.yml").read_text(encoding="utf-8")
    assert "linux/arm64" in workflow
    assert "ORACLE_SSH_KEY" in workflow
    assert "docker compose -p moneyprinterturbo pull" in workflow
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    service = blueprint["services"][0]
    assert service["dockerCommand"] == "python3 main.py"
    assert service["disk"]["mountPath"] == "/MoneyPrinterTurbo/storage"
    assert service["envVars"][0]["value"] == "webhook"
    import tomllib

    example = tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))
    assert example["app"]["task_retention_hours"] == 24
    assert example["app"]["max_concurrent_tasks"] == 1
    assert example["app"]["low_memory"] is False
