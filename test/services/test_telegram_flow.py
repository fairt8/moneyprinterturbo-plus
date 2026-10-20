"""Bot conversation, Mini App sign-in, download tokens, and delivery decisions."""

import os

from app.telegram.delivery import LinkDelivery, UploadDelivery, plan_local_delivery
from app.telegram.init_data import InitDataError, validate_init_data
from app.telegram.logic import Conversation, JobOptions, PipelineError
from app.telegram.presets import describe_stage
from app.telegram.rate_limit import RateLimiter
from app.telegram.settings import TelegramSettings
from app.telegram.store import JobStore, SessionStore
from app.telegram.tokens import MediaTokenError, issue_media_token, read_media_token
from app.telegram.updates import dispatch_update
from app.telegram.web_auth import AuthError, authenticate

# Independent of the implementation: computed with hmac in a one-off script.
_INIT_DATA = (
    "auth_date=1700000000&query_id=AAE&user=%7B%22id%22%3A42%2C%22first_name%22"
    "%3A%22Ada%22%2C%22username%22%3A%22ada%22%7D&hash="
    "80b89cd2c1a4e6073b957fe5daa2a246c380d254e01c9e30ce450002e7e7c35a"
)
_BOT_TOKEN = "123456:TEST-TOKEN"


class _Headers:
    def __init__(self, data):
        self._data = {key.lower(): value for key, value in data.items()}

    def get(self, key, default=""):
        return self._data.get(key.lower(), default)


class _Pipeline:
    def __init__(self):
        self.calls = []
        self.error = None
        self.snapshot_value = {
            "state": 4,
            "progress": 30,
            "error": None,
            "failed_stage": None,
        }

    def submit(self, owner, topic, options):
        if self.error:
            raise PipelineError(self.error)
        self.calls.append((owner, topic, options))
        return "task-1"

    def snapshot(self, task_id, owner):
        return dict(self.snapshot_value)


def _settings(**overrides) -> TelegramSettings:
    base = dict(
        bot_token=_BOT_TOKEN,
        public_base_url="https://videos.example",
        rate_limit_per_hour=2,
        service_secret="service-secret",
        web_password="studio-pass",
        api_key="api-secret",
        enabled=True,
    )
    base.update(overrides)
    return TelegramSettings(**base)


def _conversation(tmp_path, pipeline=None, **settings):
    pipeline = pipeline or _Pipeline()
    conversation = Conversation(
        settings=_settings(**settings),
        sessions=SessionStore(str(tmp_path / "sessions.json")),
        rate_limiter=RateLimiter(_settings(**settings).rate_limit_per_hour),
        pipeline=pipeline,
    )
    return conversation, pipeline


def test_init_data_accepts_a_known_signature():
    user = validate_init_data(
        _INIT_DATA,
        _BOT_TOKEN,
        now=1700000100,
    )
    assert user.user_id == 42
    assert user.username == "ada"


def test_init_data_rejects_a_tampered_user_and_an_expired_stamp():
    tampered = _INIT_DATA.replace("Ada", "Eve")
    try:
        validate_init_data(tampered, _BOT_TOKEN, now=1700000100)
        raise AssertionError("tampered initData was accepted")
    except InitDataError as exc:
        assert "signature" in str(exc).lower()

    try:
        validate_init_data(_INIT_DATA, _BOT_TOKEN, now=1700000000 + 90_000)
        raise AssertionError("expired initData was accepted")
    except InitDataError as exc:
        assert "expired" in str(exc).lower()


def test_media_token_round_trip_and_expiry():
    token = issue_media_token("secret", "task-9", ttl_seconds=60, now=1_000)
    assert read_media_token("secret", token, now=1_030) == "task-9"
    try:
        read_media_token("secret", token, now=2_000)
        raise AssertionError("expired token was accepted")
    except MediaTokenError:
        pass
    try:
        read_media_token("other", token, now=1_030)
        raise AssertionError("token signed by another secret was accepted")
    except MediaTokenError:
        pass


def test_web_auth_modes():
    settings = _settings()
    user = authenticate(
        _Headers({"X-Telegram-Init-Data": _INIT_DATA}),
        settings,
        now=1700000100,
    )
    assert user.owner == "tg:42"

    bot = authenticate(
        _Headers(
            {
                "X-Bot-Service-Secret": "service-secret",
                "X-Telegram-User-Id": "7",
            }
        ),
        settings,
    )
    assert bot.owner == "tg:7"

    web = authenticate(_Headers({"X-Web-Password": "studio-pass"}), settings)
    assert web.kind == "web"

    admin = authenticate(_Headers({"X-API-Key": "api-secret"}), settings)
    assert admin.kind == "admin"

    try:
        authenticate(_Headers({"X-Web-Password": "nope"}), settings)
        raise AssertionError("wrong password was accepted")
    except AuthError as exc:
        assert exc.status_code == 401

    open_settings = _settings(bot_token="", web_password="", api_key="", service_secret="")
    assert authenticate(_Headers({}), open_settings).kind == "anonymous"


def test_allowlist_blocks_unknown_accounts():
    settings = _settings(allowed_user_ids=frozenset({42}))
    try:
        authenticate(
            _Headers(
                {
                    "X-Bot-Service-Secret": "service-secret",
                    "X-Telegram-User-Id": "99",
                }
            ),
            settings,
        )
        raise AssertionError("unlisted user was accepted")
    except AuthError as exc:
        assert exc.status_code == 403


def test_bot_queues_a_topic_and_rate_limits_the_next_one(tmp_path):
    conversation, pipeline = _conversation(tmp_path)
    reply = conversation.on_text(5, "night market")
    assert "night market" in reply.text
    assert any(button["callback_data"] == "go" for row in reply.buttons for button in row)

    generated = conversation.on_callback(5, "go")
    assert generated.watch_task_id == "task-1"
    assert pipeline.calls[0][0] == "tg:5"
    assert pipeline.calls[0][1] == "night market"
    assert isinstance(pipeline.calls[0][2], JobOptions)

    conversation.on_text(5, "second topic")
    again = conversation.on_callback(5, "go")
    assert again.watch_task_id == "task-1"

    conversation.on_text(5, "third topic")
    blocked = conversation.on_callback(5, "go")
    assert blocked.watch_task_id is None
    assert "hour" in blocked.text
    assert len(pipeline.calls) == 2


def test_bot_commands_and_private_chat_guard(tmp_path):
    conversation, pipeline = _conversation(tmp_path, public_base_url="https://videos.example")
    start = conversation.on_start(5)
    assert "/help" in start.text
    assert start.buttons[0][0]["web_app_url"] == "https://videos.example/miniapp"

    assert "Commands" not in conversation.on_help(5).text or "How this works" in conversation.on_help(5).text
    group = conversation.on_text(5, "topic", chat_type="group")
    assert "private" in group.text.lower()
    assert pipeline.calls == []

    conversation.on_new(5)
    missing = conversation.on_callback(5, "go")
    assert "topic" in missing.text.lower()

    status = conversation.on_status(5)
    assert "don't have" in status.text


def test_settings_callbacks_change_the_saved_voice(tmp_path):
    conversation, _pipeline = _conversation(tmp_path)
    conversation.on_text(5, "harbor cranes")
    conversation.on_callback(5, "v:zh-CN-XiaoxiaoNeural")
    session = conversation.sessions.get(5, {})
    assert session["voice"] == "zh-CN-XiaoxiaoNeural"
    assert session["topic"] == "harbor cranes"

    failed = _Pipeline()
    failed.error = "ffmpeg is not available"
    conversation.pipeline = failed
    reply = conversation.on_callback(5, "go")
    assert "ffmpeg" in reply.text


def test_dispatch_update_sends_a_message_for_start():
    sent = []

    class Store:
        def get(self, user_id, defaults):
            return dict(defaults)

        def save(self, user_id, session):
            pass

    conversation = Conversation(
        settings=_settings(),
        sessions=Store(),
        rate_limiter=RateLimiter(0),
        pipeline=_Pipeline(),
    )
    reply = dispatch_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "text": "/start",
                "from": {"id": 5},
                "chat": {"id": 5, "type": "private"},
            },
        },
        conversation,
        lambda method, payload: sent.append((method, payload)),
    )
    assert reply is not None
    assert sent[0][0] == "sendMessage"
    assert sent[0][1]["chat_id"] == 5
    assert "topic" in sent[0][1]["text"]


def test_delivery_uses_a_link_when_the_file_is_too_large(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x" * 20)
    small = plan_local_delivery(str(video), upload_limit=50, download_url="https://example/v")
    assert isinstance(small, UploadDelivery)
    large = plan_local_delivery(str(video), upload_limit=10, download_url="https://example/v")
    assert isinstance(large, LinkDelivery)
    assert large.url == "https://example/v"


def test_stage_labels_follow_pipeline_checkpoints():
    assert describe_stage(0, 4) == "Writing the script"
    assert describe_stage(30, 4) == "Building subtitles"
    assert describe_stage(80, 4) == "Rendering the video"
    assert describe_stage(100, 1) == "Ready"


def test_job_store_remembers_the_owner(tmp_path):
    store = JobStore(str(tmp_path / "jobs.json"))
    store.bind("task-7", "tg:5")
    assert store.owner_of("task-7") == "tg:5"
    assert store.owner_of("missing") is None
    assert os.path.isfile(tmp_path / "jobs.json")


def test_rate_limiter_window():
    limiter = RateLimiter(1, window_seconds=10)
    assert limiter.allow("user", now=100)
    assert not limiter.allow("user", now=105)
    assert limiter.allow("user", now=111)
