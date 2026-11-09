"""Telegram and Mini App settings from config.toml plus the environment overlay."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def _int(value, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ids(value) -> frozenset[int]:
    if value is None or value == "":
        return frozenset()
    items = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
    parsed: set[int] = set()
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        if not text.isdigit():
            raise ValueError(
                "telegram.allowed_user_ids must contain integers, "
                f"got {item!r}"
            )
        parsed.add(int(text))
    return frozenset(parsed)


@dataclass(frozen=True)
class TelegramSettings:
    bot_token: str = ""
    mode: str = "polling"
    webhook_url: str = ""
    webhook_path: str = "/telegram/webhook"
    webhook_secret: str = ""
    webhook_on_api: bool = True
    webhook_listen: str = "0.0.0.0"
    webhook_port: int = 8081
    allowed_user_ids: frozenset[int] = frozenset()
    rate_limit_per_hour: int = 5
    public_base_url: str = ""
    web_password: str = ""
    media_token_secret: str = ""
    media_token_ttl_seconds: int = 86_400
    max_upload_bytes: int = 49_000_000
    poll_interval_seconds: int = 4
    task_timeout_seconds: int = 3_600
    init_data_max_age_seconds: int = 86_400
    service_secret: str = ""
    api_base_url: str = "http://127.0.0.1:8080"
    api_key: str = ""
    default_aspect: str = "9:16"
    default_voice: str = "gemini:Charon-Informative"
    default_paragraphs: int = 1
    default_language: str = ""
    default_subtitles: bool = True
    video_source: str = "gemini_image"
    enabled: bool = False
    admin_user_ids: frozenset[int] = frozenset()

    def is_user_allowed(self, user_id: int) -> bool:
        if not self.allowed_user_ids:
            return True
        try:
            return int(user_id) in self.allowed_user_ids
        except (TypeError, ValueError):
            return False

    def is_admin(self, user_id: int) -> bool:
        """Admins may inspect disk usage. An empty admin list uses the allowlist.

        When both lists are empty the command is refused, so a public bot
        cannot purge the disk.
        """

        allowed = self.admin_user_ids or self.allowed_user_ids
        if not allowed:
            return False
        try:
            return int(user_id) in allowed
        except (TypeError, ValueError):
            return False

    def miniapp_url(self) -> str:
        if not self.public_base_url:
            return ""
        return self.public_base_url.rstrip("/") + "/miniapp"

    def media_secret(self) -> str:
        return (
            self.media_token_secret
            or self.service_secret
            or self.bot_token
            or self.api_key
            or ""
        )

    @property
    def auth_required(self) -> bool:
        return bool(self.bot_token or self.web_password or self.api_key)


def _telegram_enabled(section, token: str) -> bool:
    """Turn the bot on when a token exists unless TELEGRAM_ENABLED says otherwise.

    The environment value overrides config.toml. Docker Compose forwards
    TELEGRAM_ENABLED and defaults it to true. When the variable is unset, a
    configured token still starts the bot, including deployments whose
    config.toml was copied while ``enabled = false``.
    """

    raw = os.environ.get("TELEGRAM_ENABLED")
    if raw is not None and str(raw).strip() != "":
        return _bool(raw, False)
    if token:
        return True
    return _bool(section.get("enabled", False), False)


def load_telegram_settings() -> TelegramSettings:
    from app.config import config

    section = config.telegram
    app = config.app
    token = str(section.get("bot_token") or "").strip()
    mode = str(section.get("mode") or "polling").strip().lower()
    if mode not in {"polling", "webhook"}:
        mode = "polling"

    webhook_secret = str(section.get("webhook_secret") or "").strip()
    service_secret = str(section.get("service_secret") or "").strip()
    if not service_secret:
        service_secret = webhook_secret or token

    public_base = str(
        section.get("public_base_url") or app.get("endpoint") or ""
    ).strip().rstrip("/")

    enabled = _telegram_enabled(section, token)

    paragraphs = _int(section.get("default_paragraphs", 1), 1)
    paragraphs = min(10, max(1, paragraphs))

    return TelegramSettings(
        bot_token=token,
        mode=mode,
        webhook_url=str(section.get("webhook_url") or "").strip(),
        webhook_path=str(section.get("webhook_path") or "/telegram/webhook").strip()
        or "/telegram/webhook",
        webhook_secret=webhook_secret,
        webhook_on_api=_bool(section.get("webhook_on_api", True), True),
        webhook_listen=str(section.get("webhook_listen") or "0.0.0.0").strip(),
        webhook_port=_int(section.get("webhook_port", 8081), 8081),
        allowed_user_ids=_ids(section.get("allowed_user_ids", [])),
        rate_limit_per_hour=max(0, _int(section.get("rate_limit_per_hour", 5), 5)),
        public_base_url=public_base,
        web_password=str(section.get("web_password") or "").strip(),
        media_token_secret=str(section.get("media_token_secret") or "").strip(),
        media_token_ttl_seconds=max(
            60, _int(section.get("media_token_ttl_seconds", 86_400), 86_400)
        ),
        max_upload_bytes=max(
            1, _int(section.get("max_upload_bytes", 49_000_000), 49_000_000)
        ),
        poll_interval_seconds=max(1, _int(section.get("poll_interval_seconds", 4), 4)),
        task_timeout_seconds=max(
            30, _int(section.get("task_timeout_seconds", 3_600), 3_600)
        ),
        init_data_max_age_seconds=max(
            30, _int(section.get("init_data_max_age_seconds", 86_400), 86_400)
        ),
        service_secret=service_secret,
        api_base_url=str(
            section.get("api_base_url") or "http://127.0.0.1:8080"
        ).strip().rstrip("/"),
        api_key=str(app.get("api_key") or "").strip()
        if isinstance(app.get("api_key"), str)
        else "",
        default_aspect=str(section.get("default_aspect") or "9:16").strip() or "9:16",
        default_voice=str(section.get("default_voice") or "gemini:Charon-Informative").strip()
        or "gemini:Charon-Informative",
        default_paragraphs=paragraphs,
        default_language=str(section.get("default_language") or "").strip(),
        default_subtitles=_bool(section.get("default_subtitles", True), True),
        video_source=str(app.get("video_source") or "gemini_image").strip() or "gemini_image",
        enabled=enabled,
        admin_user_ids=_ids(section.get("admin_user_ids", [])),
    )
