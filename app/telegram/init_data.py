"""Validate Telegram Mini App initData.

The algorithm is the one documented by Telegram:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

secret_key = HMAC_SHA256(key="WebAppData", message=bot_token)
hash = hex(HMAC_SHA256(key=secret_key, message=data_check_string))
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


class InitDataError(ValueError):
    """initData is missing, expired, or signed by a different bot token."""


@dataclass(frozen=True)
class TelegramWebUser:
    user_id: int
    username: str = ""
    first_name: str = ""


def _data_check_string(fields: dict[str, str]) -> str:
    lines = [f"{key}={value}" for key, value in sorted(fields.items()) if key != "hash"]
    return "\n".join(lines)


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 86_400,
    now: float | None = None,
) -> TelegramWebUser:
    """Return the signed Telegram user, or raise InitDataError."""

    if not bot_token:
        raise InitDataError("The bot token is not configured, so Mini App sign-in is unavailable.")
    if not init_data or not init_data.strip():
        raise InitDataError("Missing Telegram sign-in data. Open this page from the bot.")

    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    fields: dict[str, str] = {}
    for key, value in pairs:
        fields[key] = value

    presented = fields.get("hash", "")
    if not presented:
        raise InitDataError("Telegram sign-in data has no signature.")

    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calculated = hmac.new(
        secret_key,
        _data_check_string(fields).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(calculated, presented.lower()):
        raise InitDataError("Telegram sign-in data failed signature check.")

    try:
        auth_date = int(fields.get("auth_date", ""))
    except (TypeError, ValueError):
        raise InitDataError("Telegram sign-in data has no auth_date.") from None

    current = time.time() if now is None else now
    if auth_date > current + 60:
        raise InitDataError("Telegram sign-in data is timestamped in the future.")
    if current - auth_date > max_age_seconds:
        raise InitDataError("Telegram sign-in data has expired. Close the Mini App and open it again.")

    raw_user = fields.get("user", "")
    try:
        user = json.loads(raw_user) if raw_user else None
    except json.JSONDecodeError as exc:
        raise InitDataError("Telegram sign-in data has a malformed user.") from exc
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise InitDataError("Telegram sign-in data does not include a user id.")

    return TelegramWebUser(
        user_id=user["id"],
        username=str(user.get("username") or ""),
        first_name=str(user.get("first_name") or ""),
    )
