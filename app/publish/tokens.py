"""Short-lived tokens that let a Telegram user open the OAuth start page."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from app.publish.errors import PublishRejected


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def issue_connect_token(secret: str, owner: str, *, ttl_seconds: int, now: float | None = None) -> str:
    if not secret:
        raise PublishRejected("Set MEDIA_TOKEN_SECRET, the bot token, or an API key so connect links can be signed.")
    if not _valid_owner(owner):
        raise PublishRejected("That account owner is not valid.")
    current = int(time.time() if now is None else now)
    expires_at = current + int(ttl_seconds)
    payload = f"{owner}.{expires_at}".encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return _b64encode(payload + b"." + signature.encode("ascii"))


def read_connect_token(secret: str, token: str, *, now: float | None = None) -> str:
    if not secret or not token:
        raise PublishRejected("This connect link is missing a signature.", status_code=401)
    try:
        decoded = _b64decode(token).decode("utf-8")
        owner, expires_text, signature = decoded.split(".", 2)
        expires_at = int(expires_text)
    except (ValueError, UnicodeError) as exc:
        raise PublishRejected("This connect link is malformed.", status_code=401) from exc
    payload = f"{owner}.{expires_at}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise PublishRejected("This connect link is not valid.", status_code=401)
    current = int(time.time() if now is None else now)
    if expires_at < current:
        raise PublishRejected("This connect link has expired. Ask the bot for a new one.", status_code=401)
    if not _valid_owner(owner):
        raise PublishRejected("This connect link is not valid.", status_code=401)
    return owner


def _valid_owner(owner: str) -> bool:
    if owner in {"web", "admin", "anon"}:
        return True
    if owner.startswith("tg:") and owner[3:].isdigit():
        return True
    return False
