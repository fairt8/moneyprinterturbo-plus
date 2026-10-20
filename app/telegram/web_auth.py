"""Authenticate Mini App requests from Telegram, a shared password, or the API key."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from app.telegram.init_data import InitDataError, validate_init_data
from app.telegram.settings import TelegramSettings


class AuthError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class Principal:
    kind: str
    owner: str
    telegram_user_id: int | None = None


def _matches(presented: str, expected: str) -> bool:
    if not presented or not expected:
        return False
    return secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def authenticate(
    headers,
    settings: TelegramSettings,
    *,
    now: float | None = None,
) -> Principal:
    """Identify the caller. Raises AuthError when credentials are required and missing."""

    init_data = headers.get("x-telegram-init-data", "") or ""
    web_password = headers.get("x-web-password", "") or ""
    api_key = headers.get("x-api-key", "") or ""
    service_secret = headers.get("x-bot-service-secret", "") or ""
    user_header = headers.get("x-telegram-user-id", "") or ""

    if init_data.strip():
        try:
            user = validate_init_data(
                init_data,
                settings.bot_token,
                max_age_seconds=settings.init_data_max_age_seconds,
                now=now,
            )
        except InitDataError as exc:
            raise AuthError(401, str(exc)) from exc
        if not settings.is_user_allowed(user.user_id):
            raise AuthError(403, "This Mini App is limited to approved Telegram accounts.")
        return Principal("telegram", f"tg:{user.user_id}", user.user_id)

    if service_secret and settings.service_secret and _matches(service_secret, settings.service_secret):
        if not user_header.isdigit():
            raise AuthError(401, "The bot request is missing a numeric Telegram user id.")
        user_id = int(user_header)
        if not settings.is_user_allowed(user_id):
            raise AuthError(403, "This bot is limited to approved Telegram accounts.")
        return Principal("telegram", f"tg:{user_id}", user_id)

    if settings.web_password and _matches(web_password, settings.web_password):
        return Principal("web", "web", None)

    if settings.api_key and _matches(api_key, settings.api_key):
        return Principal("admin", "admin", None)

    # A wrong explicit credential should not fall through to anonymous mode.
    if web_password or api_key or service_secret:
        raise AuthError(401, "That password or API key was not recognized.")

    if not settings.auth_required:
        return Principal("anonymous", "anon", None)

    raise AuthError(
        401,
        "Open this page from the Telegram bot, or enter the web password / API key.",
    )
