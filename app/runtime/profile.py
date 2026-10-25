"""Low-memory render profile for a shared VM.

Unset or false keeps the historical 1080p output. ``MPT_LOW_MEMORY=true``
(the Docker default) scales the frame to 720p, uses one MoviePy thread, and
asks libx264 for a fast preset.
"""

from __future__ import annotations

import os


def low_memory_enabled() -> bool:
    raw = os.environ.get("MPT_LOW_MEMORY", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    try:
        from app.config import config

        value = config.app.get("low_memory", False)
    except Exception:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def fit_resolution(width: int, height: int) -> tuple[int, int]:
    """Return 720p dimensions when low-memory mode is on. Both sides stay even."""

    if not low_memory_enabled():
        return width, height
    if width == height:
        return 720, 720
    if width > height:
        return 1280, 720
    return 720, 1280


def encode_threads(requested: int | None) -> int:
    if low_memory_enabled():
        return 1
    try:
        value = int(requested or 2)
    except (TypeError, ValueError):
        value = 2
    return max(1, min(8, value))


def ffmpeg_extra_params(existing: list[str] | None = None) -> list[str]:
    base = list(existing or [])
    if not low_memory_enabled() or "-preset" in base:
        return base
    return ["-preset", "veryfast", "-crf", "28", *base]
