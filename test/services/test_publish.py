"""Official YouTube and TikTok publishing with mocked provider APIs."""

import json
import os
import stat
import time

import pytest

from app.publish import oauth, queue, service, tiktok, youtube
from app.publish.errors import PublishError, PublishRejected
from app.publish.settings import PublishSettings, YoutubeProject
from app.publish.store import PublishStore
from app.publish.tokens import issue_connect_token, read_connect_token
from app.telegram.logic import Conversation
from app.telegram.publish_flow import describe_job
from app.telegram.rate_limit import RateLimiter
from app.telegram.settings import TelegramSettings
from app.telegram.store import SessionStore


class _Response:
    def __init__(self, status, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = {} if payload is None else payload
        self.headers = headers or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


def _settings(**overrides) -> PublishSettings:
    project = YoutubeProject("default", "client", "secret", overrides.pop("quota", 6))
    base = dict(
        public_base_url="https://videos.example",
        youtube_projects=(project,),
        youtube_redirect_uri="https://videos.example/auth/youtube/callback",
        tiktok_client_key="tt-key",
        tiktok_client_secret="tt-secret",
        tiktok_redirect_uri="https://videos.example/auth/tiktok/callback",
        tiktok_audited=False,
        tiktok_default_mode="direct",
        tiktok_daily_posts=15,
        stagger_seconds=10,
        max_concurrent_jobs=1,
        max_attempts=3,
    )
    base.update(overrides)
    return PublishSettings(**base)


def _store(tmp_path) -> PublishStore:
    return PublishStore(str(tmp_path / "publish"))


def _video(tmp_path) -> str:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"not a real mp4, but bytes the uploader can send")
    return str(path)


def _account(platform="youtube", account_id="y12345678", project_id="default") -> dict:
    now = time.time() + 3600
    return {
        "id": account_id,
        "owner": "tg:5",
        "platform": platform,
        "label": "Travel" if platform == "youtube" else "Brand",
        "external_id": "UC1" if platform == "youtube" else "open-1",
        "display_name": "Travel",
        "project_id": project_id,
        "refresh_token": "refresh-secret",
        "access_token": "access-secret",
        "expires_at": now,
        "needs_reauth": False,
        "linked_at": "2026-10-20T00:00:00+00:00",
        "updated_at": "2026-10-20T00:00:00+00:00",
    }


def test_connect_token_round_trip_and_rejection():
    token = issue_connect_token("secret", "tg:5", ttl_seconds=60, now=1_000)
    assert read_connect_token("secret", token, now=1_030) == "tg:5"
    with pytest.raises(PublishRejected):
        read_connect_token("other", token, now=1_030)


def test_account_file_hides_tokens_and_is_owner_only(tmp_path):
    store = _store(tmp_path)
    store.upsert_account(_account())
    mode = stat.S_IMODE(os.stat(store.accounts.path).st_mode)
    assert mode == 0o600
    public = store.accounts_for("tg:5")[0]
    assert "refresh_token" not in public
    assert "access-secret" not in json.dumps(public)
    assert store.accounts_for("tg:9") == []
    renamed = store.rename_account("tg:5", "y12345678", "Night market")
    assert renamed["label"] == "Night market"
    assert store.delete_account("tg:5", "y12345678") is True
    assert store.accounts_for("tg:5") == []


def test_oauth_state_is_single_use(tmp_path, monkeypatch):
    store = _store(tmp_path)
    settings = _settings()
    url = oauth.begin(store, settings, owner="tg:5", platform="youtube", label="Travel")
    assert "accounts.google.com" in url
    state = url.split("state=", 1)[1]
    monkeypatch.setattr(
        youtube,
        "exchange_code",
        lambda **kwargs: {"access_token": "a", "refresh_token": "r", "expires_in": 3600},
    )
    monkeypatch.setattr(
        youtube,
        "channel_profile",
        lambda token: {"external_id": "UC9", "display_name": "Travel"},
    )
    linked = oauth.finish(store, settings, platform="youtube", code="code", state=state)
    assert linked["label"] == "Travel"
    assert linked["external_id"] == "UC9"
    secret = store.account_secret(linked["id"])
    assert secret["refresh_token"] == "r"
    with pytest.raises(PublishRejected):
        oauth.finish(store, settings, platform="youtube", code="code", state=state)


def test_reauth_updates_the_same_account(tmp_path, monkeypatch):
    store = _store(tmp_path)
    settings = _settings()
    store.upsert_account(_account())
    url = oauth.begin(
        store,
        settings,
        owner="tg:5",
        platform="youtube",
        account_id="y12345678",
    )
    state = url.split("state=", 1)[1]
    monkeypatch.setattr(
        youtube,
        "exchange_code",
        lambda **kwargs: {"access_token": "new", "refresh_token": "next", "expires_in": 10},
    )
    monkeypatch.setattr(
        youtube,
        "channel_profile",
        lambda token: {"external_id": "UC1", "display_name": "Travel"},
    )
    linked = oauth.finish(store, settings, platform="youtube", code="code", state=state)
    assert linked["id"] == "y12345678"
    assert store.account_secret("y12345678")["refresh_token"] == "next"


def test_targets_and_multi_account_jobs_are_separate(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.upsert_account(_account())
    second = _account()
    second["id"] = "yabcdef99"
    second["label"] = "Cooking"
    store.upsert_account(second)
    tiktok_account = _account("tiktok", "tabcdef99")
    store.upsert_account(tiktok_account)
    target = service.save_target(
        "tg:5",
        "All shorts",
        ["y12345678", "tabcdef99"],
        is_default=True,
        store=store,
    )
    assert service.set_default_target("tg:5", target["id"], store=store)["is_default"] is True
    video = _video(tmp_path)
    monkeypatch.setattr("app.telegram.jobs.task_video_path", lambda task_id: video)
    monkeypatch.setattr(
        "app.telegram.runtime.get_job_store",
        lambda: type("Jobs", (), {"owner_of": lambda self, task_id: "tg:5"})(),
    )
    monkeypatch.setattr(
        "app.publish.copy.generate_social_metadata",
        lambda **kwargs: {
            "title": "Hook " + str(kwargs["video_subject"])[:12],
            "caption": "A caption",
            "hashtags": ["#story"],
        },
    )
    jobs = service.enqueue(
        "tg:5",
        {"task_id": "task-1", "target_id": target["id"], "vary_copy": True, "privacy": "private"},
        settings=_settings(stagger_seconds=15),
        store=store,
    )
    assert len(jobs) == 2
    assert jobs[0]["account_id"] != jobs[1]["account_id"]
    assert jobs[1]["not_before"] - jobs[0]["not_before"] == 15
    titles = {job["title"] for job in jobs}
    assert len(titles) == 2
    assert all("#Shorts" in job["title"] or "#Shorts" in " ".join(job["hashtags"]) for job in jobs if job["platform"] == "youtube")
    assert "refresh-secret" not in json.dumps(jobs)


def test_youtube_resumable_upload_and_refresh(tmp_path, monkeypatch):
    store = _store(tmp_path)
    account = _account()
    account["expires_at"] = 0
    store.upsert_account(account)
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        if "oauth2.googleapis.com/token" in url:
            return _Response(200, {"access_token": "fresh", "expires_in": 3600, "refresh_token": "refresh-secret"})
        if method == "POST" and "uploadType=resumable" in url or (method == "POST" and "upload/youtube" in url):
            return _Response(200, {}, headers={"Location": "https://upload.example/session"})
        if method == "PUT":
            return _Response(200, {"id": "vid123"})
        raise AssertionError(url)

    monkeypatch.setattr(youtube, "request", fake_request)
    monkeypatch.setattr(queue, "get_store", lambda: store)
    monkeypatch.setattr(queue, "load_publish_settings", lambda: _settings())
    video = _video(tmp_path)
    monkeypatch.setattr(queue, "task_video_path", lambda task_id: video)
    job = {
        "id": "j1",
        "owner": "tg:5",
        "task_id": "task-1",
        "account_id": "y12345678",
        "account_label": "Travel",
        "platform": "youtube",
        "project_id": "default",
        "state": "queued",
        "phase": "init",
        "attempts": 1,
        "title": "Night market #Shorts",
        "description": "Walk through the stalls.",
        "hashtags": ["#Shorts", "#travel"],
        "privacy": "public",
        "shorts": True,
        "quota_counted": False,
    }
    store.add_jobs([job])
    done = queue.run_job("j1")
    assert done["state"] == "complete"
    assert done["youtube_url"] == "https://www.youtube.com/shorts/vid123"
    assert any("oauth2.googleapis.com/token" in url for _, url in calls)
    assert store.account_secret("y12345678")["access_token"] == "fresh"
    assert "upload_url" not in done


def test_youtube_quota_stops_the_next_upload(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.upsert_account(_account())
    other = _account()
    other["id"] = "yabcdef99"
    other["label"] = "Second"
    store.upsert_account(other)
    monkeypatch.setattr(queue, "get_store", lambda: store)
    monkeypatch.setattr(queue, "load_publish_settings", lambda: _settings(quota=1))
    monkeypatch.setattr(queue, "task_video_path", lambda task_id: _video(tmp_path))
    monkeypatch.setattr(
        youtube,
        "request",
        lambda method, url, **kwargs: _Response(200, {"id": "vid"}, headers={"Location": "https://upload.example/s"}),
    )

    def one(account_id, job_id):
        return {
            "id": job_id,
            "owner": "tg:5",
            "task_id": "task-1",
            "account_id": account_id,
            "account_label": account_id,
            "platform": "youtube",
            "project_id": "default",
            "state": "queued",
            "phase": "init",
            "attempts": 1,
            "title": "Title #Shorts",
            "description": "Body",
            "hashtags": ["#Shorts"],
            "privacy": "unlisted",
            "shorts": True,
            "quota_counted": False,
        }

    store.add_jobs([one("y12345678", "j1"), one("yabcdef99", "j2")])
    assert queue.run_job("j1")["state"] == "complete"
    failed = queue.run_job("j2")
    assert failed["state"] == "failed"
    assert "quota" in failed["error"].lower() or "allowance" in failed["error"].lower()


def test_tiktok_direct_post_polls_until_complete(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.upsert_account(_account("tiktok", "tabcdef99"))
    statuses = iter(
        [
            {"status": "PROCESSING_UPLOAD", "fail_reason": ""},
            {
                "status": "PUBLISH_COMPLETE",
                "publicaly_available_post_id": ["7123"],
                "fail_reason": "",
            },
        ]
    )

    def fake_request(method, url, **kwargs):
        if url.endswith("/init/"):
            return _Response(200, {"data": {"publish_id": "p1", "upload_url": "https://upload.tiktok/v"}, "error": {"code": "ok"}})
        if method == "PUT":
            return _Response(201, {})
        if url.endswith("/status/fetch/"):
            return _Response(200, {"data": next(statuses), "error": {"code": "ok"}})
        raise AssertionError(url)

    monkeypatch.setattr(tiktok, "request", fake_request)

    def poll(token, publish_id, **kwargs):
        first = tiktok.fetch_status(token, publish_id)
        if first["status"] == "PUBLISH_COMPLETE":
            return first
        return tiktok.fetch_status(token, publish_id)

    monkeypatch.setattr(tiktok, "poll_status", poll)
    monkeypatch.setattr(queue, "get_store", lambda: store)
    monkeypatch.setattr(queue, "load_publish_settings", lambda: _settings(tiktok_audited=True))
    monkeypatch.setattr(queue, "task_video_path", lambda task_id: _video(tmp_path))
    store.add_jobs([
        {
            "id": "jt",
            "owner": "tg:5",
            "task_id": "task-1",
            "account_id": "tabcdef99",
            "account_label": "Brand",
            "platform": "tiktok",
            "state": "queued",
            "phase": "init",
            "attempts": 1,
            "title": "Night market",
            "description": "Come walk with me.",
            "hashtags": ["#fyp"],
            "privacy": "public",
            "tiktok_mode": "direct",
            "shorts": False,
            "quota_counted": False,
        }
    ])
    done = queue.run_job("jt")
    assert done["state"] == "complete"
    assert done["tiktok_status"] == "PUBLISH_COMPLETE"
    assert done["tiktok_post_id"] == "7123"


def test_unaudited_tiktok_rejects_a_public_direct_post(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.upsert_account(_account("tiktok", "tabcdef99"))
    monkeypatch.setattr(queue, "get_store", lambda: store)
    monkeypatch.setattr(queue, "load_publish_settings", lambda: _settings(tiktok_audited=False))
    monkeypatch.setattr(queue, "task_video_path", lambda task_id: _video(tmp_path))
    store.add_jobs([
        {
            "id": "jt",
            "owner": "tg:5",
            "task_id": "task-1",
            "account_id": "tabcdef99",
            "account_label": "Brand",
            "platform": "tiktok",
            "state": "queued",
            "phase": "init",
            "attempts": 1,
            "title": "Night market",
            "description": "Come walk with me.",
            "hashtags": ["#fyp"],
            "privacy": "public",
            "tiktok_mode": "direct",
            "shorts": False,
            "quota_counted": False,
        }
    ])
    failed = queue.run_job("jt")
    assert failed["state"] == "failed"
    assert "SELF_ONLY" in failed["error"] or "not audited" in failed["error"]


def test_tiktok_refresh_replaces_the_access_token(monkeypatch):
    account = _account("tiktok", "tabcdef99")
    account["expires_at"] = 0

    def fake_request(method, url, **kwargs):
        assert kwargs["data"]["grant_type"] == "refresh_token"
        assert kwargs["data"]["refresh_token"] == "refresh-secret"
        return _Response(
            200,
            {"access_token": "fresh-tt", "refresh_token": "rotated", "expires_in": 100, "refresh_expires_in": 500},
        )

    monkeypatch.setattr(tiktok, "request", fake_request)
    token, expires, refresh, _refresh_expires = tiktok.ensure_access_token(account, _settings())
    assert token == "fresh-tt"
    assert refresh == "rotated"
    assert expires > time.time()


def test_transient_youtube_error_is_retried_then_failed(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.upsert_account(_account())
    monkeypatch.setattr(queue, "get_store", lambda: store)
    monkeypatch.setattr(queue, "load_publish_settings", lambda: _settings(max_attempts=2))
    monkeypatch.setattr(queue, "task_video_path", lambda task_id: _video(tmp_path))
    monkeypatch.setattr(
        youtube,
        "request",
        lambda method, url, **kwargs: _Response(503, {"error": {"message": "unavailable"}}),
    )
    store.add_jobs([
        {
            "id": "j1",
            "owner": "tg:5",
            "task_id": "task-1",
            "account_id": "y12345678",
            "account_label": "Travel",
            "platform": "youtube",
            "project_id": "default",
            "state": "uploading",
            "phase": "init",
            "attempts": 1,
            "title": "Title",
            "description": "Body",
            "hashtags": [],
            "privacy": "private",
            "shorts": True,
            "quota_counted": False,
        }
    ])
    first = queue.run_job("j1")
    assert first["state"] == "queued"
    assert "unavailable" in first["error"]
    # The queued retry is claimed by a worker. Run it as the second attempt.
    secret = store.job_secret("j1")
    secret["state"] = "uploading"
    secret["attempts"] = 2
    store.replace_job(secret)
    second = queue.run_job("j1")
    assert second["state"] == "failed"


def test_publish_api_queues_selected_accounts(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.asgi import app
    from app.telegram.settings import TelegramSettings

    store = _store(tmp_path)
    store.upsert_account(_account())
    second = _account()
    second["id"] = "yabcdef99"
    second["label"] = "Cooking"
    second["owner"] = "web"
    store.upsert_account(second)
    first = _account()
    first["owner"] = "web"
    store.upsert_account(first)
    video = _video(tmp_path)
    monkeypatch.setattr("app.publish.service.get_store", lambda: store)
    monkeypatch.setattr("app.controllers.v1.publish.get_store", lambda: store)
    monkeypatch.setattr("app.telegram.jobs.task_video_path", lambda task_id: video)
    monkeypatch.setattr(
        "app.telegram.runtime.get_job_store",
        lambda: type("Jobs", (), {"owner_of": staticmethod(lambda task_id: "web")})(),
    )
    monkeypatch.setattr(
        "app.publish.copy.generate_social_metadata",
        lambda **kwargs: {"title": "Bookstore", "caption": "A quiet aisle.", "hashtags": ["#books"]},
    )
    monkeypatch.setattr(
        "app.controllers.v1.publish.load_telegram_settings",
        lambda: TelegramSettings(web_password="studio-pass", bot_token="", api_key="", service_secret=""),
    )
    client = TestClient(app)
    page = client.get("/accounts")
    assert page.status_code == 200
    assert "Link YouTube" in page.text
    denied = client.get("/api/v1/publish/accounts")
    assert denied.status_code == 401
    headers = {"X-Web-Password": "studio-pass"}
    listed = client.get("/api/v1/publish/accounts", headers=headers)
    assert listed.status_code == 200
    labels = [item["label"] for item in listed.json()["data"]["accounts"]]
    assert "Cooking" in labels
    created = client.post(
        "/api/v1/publish",
        headers=headers,
        json={
            "task_id": "task-1",
            "account_ids": ["y12345678", "yabcdef99"],
            "title": "Bookstore night",
            "description": "Turn the pages slowly.",
            "hashtags": ["#books"],
            "privacy": "unlisted",
            "stagger_seconds": 5,
        },
    )
    assert created.status_code == 200
    jobs = created.json()["data"]["jobs"]
    assert len(jobs) == 2
    assert {job["state"] for job in jobs} == {"queued"}
    missing = client.get("/api/v1/publish/jobs/missing", headers=headers)
    assert missing.status_code == 404


def test_bot_can_pick_several_accounts_and_report_each_result(tmp_path):
    class Publisher:
        def __init__(self):
            self.enqueued = None

        def accounts(self, owner):
            return [
                {"id": "y12345678", "platform": "youtube", "label": "Travel", "needs_reauth": False, "project_id": "default"},
                {"id": "yabcdef99", "platform": "youtube", "label": "Cooking", "needs_reauth": False, "project_id": "default"},
            ]

        def targets(self, owner):
            return [{"id": "g12345678", "name": "Both", "account_ids": ["y12345678", "yabcdef99"], "is_default": True}]

        def connect_url(self, owner, platform, label="", project_id="default", account_id=""):
            return "https://videos.example/auth/youtube/start?connect=1"

        def preview(self, owner, task_id, platform):
            return {"title": "Night market", "description": "Walk with me.", "hashtags": ["#city"]}

        def enqueue(self, owner, payload):
            self.enqueued = payload
            return [
                {"id": "j1", "state": "queued", "account_label": "Travel", "platform": "youtube"},
                {"id": "j2", "state": "failed", "account_label": "Cooking", "platform": "youtube", "error": "quota"},
            ]

        def rename(self, owner, account_id, label):
            return {"id": account_id, "label": label}

        def unlink(self, owner, account_id):
            return None

        def set_default_target(self, owner, target_id):
            return {"id": target_id, "name": "Both"}

        def save_target(self, owner, name, account_ids, is_default=False):
            return {"id": "g99999999", "name": name}

    publisher = Publisher()
    conversation = Conversation(
        settings=TelegramSettings(bot_token="123:ABC", public_base_url="https://videos.example"),
        sessions=SessionStore(str(tmp_path / "sessions.json")),
        rate_limiter=RateLimiter(0),
        pipeline=object(),
        publisher=publisher,
    )
    accounts = conversation.publish_flow.on_accounts(5)
    assert "Travel" in accounts.text
    assert "Cooking" in accounts.text
    session = conversation.sessions.get(5, {})
    session["task_id"] = "task-1"
    conversation.sessions.save(5, session)
    picker = conversation.on_callback(5, "up:yt")
    assert any(button["callback_data"] == "pk:y12345678" for row in picker.buttons for button in row)
    conversation.on_callback(5, "pk:yabcdef99")
    conversation.on_callback(5, "ok")
    published = conversation.on_callback(5, "do")
    assert publisher.enqueued["account_ids"] == ["y12345678"]
    assert published.watch_publish_ids == ["j1"]
    assert "quota" in published.text
    assert "https://www.youtube.com/shorts/abc" in describe_job(
        {"platform": "youtube", "account_label": "Travel", "state": "complete", "youtube_url": "https://www.youtube.com/shorts/abc"}
    )


def test_environment_publish_projects_do_not_echo_secrets():
    from app.config.env_overlay import apply_environment_overrides

    publish = {}
    applied = apply_environment_overrides(
        {},
        {},
        {
            "YOUTUBE_CLIENT_ID": "yt-client",
            "YOUTUBE_CLIENT_SECRET": "yt-secret",
            "YOUTUBE_PROJECTS": "studio-b",
            "YOUTUBE_STUDIO_B_CLIENT_ID": "other-client",
            "YOUTUBE_STUDIO_B_CLIENT_SECRET": "other-secret",
            "TIKTOK_CLIENT_KEY": "tt-key",
            "TIKTOK_AUDITED": "false",
            "PUBLISH_STAGGER_SECONDS": "30",
        },
        publish=publish,
    )
    assert publish["youtube_projects"][0]["client_secret"] == "yt-secret"
    assert publish["youtube_projects"][1]["id"] == "studio-b"
    assert publish["tiktok_audited"] is False
    assert publish["stagger_seconds"] == 30
    assert "yt-secret" not in " ".join(applied)
    assert "YOUTUBE_CLIENT_ID" in applied


def test_chunk_ranges_keep_a_short_file_in_one_request():
    assert tiktok.chunk_ranges(1024) == [(0, 1023)]
    with pytest.raises(PublishError):
        tiktok.privacy_level("public", audited=False, mode="direct")
    assert tiktok.privacy_level("public", audited=True, mode="direct") == "PUBLIC_TO_EVERYONE"
    assert tiktok.privacy_level("public", audited=False, mode="inbox") == "SELF_ONLY"
