"""Short-lived HMAC tokens for video download links.

Telegram and a normal browser cannot attach the API key header to a plain
video URL. A signed token lets /api/v1/media/{task_id} serve one finished
file until it expires.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time


class MediaTokenError(ValueError):
    pass


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def issue_media_token(secret: str, task_id: str, *, ttl_seconds: int, now: float | None = None) -> str:
    if not secret:
        raise MediaTokenError("No media token secret is configured.")
    if not task_id or "/" in task_id or ".." in task_id:
        raise MediaTokenError("Invalid task id.")
    current = int(time.time() if now is None else now)
    expires_at = current + int(ttl_seconds)
    payload = f"{task_id}.{expires_at}".encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return _b64encode(payload + b"." + signature.encode("ascii"))


def read_media_token(secret: str, token: str, *, now: float | None = None) -> str:
    """Return the task id when the token is valid and unexpired."""

    if not secret or not token:
        raise MediaTokenError("Missing media token.")
    try:
        decoded = _b64decode(token).decode("utf-8")
        task_id, expires_text, signature = decoded.split(".", 2)
        expires_at = int(expires_text)
    except (ValueError, UnicodeError) as exc:
        raise MediaTokenError("Malformed media token.") from exc

    payload = f"{task_id}.{expires_at}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise MediaTokenError("Media token signature does not match.")

    current = int(time.time() if now is None else now)
    if expires_at < current:
        raise MediaTokenError("Media token has expired.")
    if not task_id or "/" in task_id or ".." in task_id:
        raise MediaTokenError("Invalid task id.")
    return task_id
