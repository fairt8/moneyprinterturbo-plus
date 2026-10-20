"""Small retry helper for transient HTTP failures.

Successful responses are returned on the first attempt. Connection errors and
429/5xx responses are tried again. Callers keep their existing handling for the
final response or the final exception.
"""

from __future__ import annotations

import os
import time
from typing import Callable

import requests


RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


def default_backoff_seconds() -> float:
    raw = os.getenv("MPT_HTTP_RETRY_BACKOFF", "")
    if raw.strip() == "":
        return 0.25
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.25


def _status_code(response) -> int:
    status = getattr(response, "status_code", 200)
    try:
        return int(status)
    except (TypeError, ValueError):
        # Test doubles sometimes use a MagicMock status. Treat that as a
        # finished response and let the caller parse the body.
        return 200


def request_with_retries(
    func: Callable,
    *,
    attempts: int = 3,
    backoff: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
):
    """Call ``func`` until it returns a usable response or attempts run out."""

    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if backoff is None:
        backoff = default_backoff_seconds()

    last_error: Exception | None = None
    response = None
    for attempt in range(1, attempts + 1):
        try:
            response = func()
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= attempts:
                raise
            sleep(backoff * attempt)
            continue

        if _status_code(response) in RETRY_STATUS_CODES and attempt < attempts:
            sleep(backoff * attempt)
            continue
        return response

    if last_error is not None:
        raise last_error
    return response
