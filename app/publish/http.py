"""HTTP entry point for provider calls.

Tests replace ``request`` with a fake. Production uses the ``requests`` library.
"""

from __future__ import annotations


def request(method: str, url: str, **kwargs):
    import requests

    kwargs.setdefault("timeout", 60)
    return requests.request(method, url, **kwargs)


def error_text(response, limit: int = 400) -> str:
    """A short provider message that does not include tokens or secrets."""

    body = {}
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            body = parsed
    except Exception:
        body = {}
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = error.get("message") or error.get("code") or error.get("status")
        reason = ""
        errors = error.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            reason = str(errors[0].get("reason") or "")
        text = " ".join(part for part in (str(message or ""), reason) if part).strip()
        if text:
            return text[:limit]
    if isinstance(error, str) and error.strip():
        return error.strip()[:limit]
    text = getattr(response, "text", "") or ""
    if not isinstance(text, str):
        text = ""
    text = " ".join(text.split())
    if text:
        return text[:limit]
    return f"HTTP {getattr(response, 'status_code', '?')}"
