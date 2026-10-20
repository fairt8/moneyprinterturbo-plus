"""Apply environment variables on top of config.toml without writing secrets to disk.

The overlay mutates the in-memory config only. A later WebUI save can persist
those values into the gitignored config.toml; nothing here prints secret values.
"""

from __future__ import annotations

import os
from typing import Callable, Mapping


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str) -> int:
    return int(value.strip())


def _user_ids(value: str) -> list[int]:
    ids: list[int] = []
    for item in _csv(value):
        if not item.isdigit():
            raise ValueError(
                "TELEGRAM_ALLOWED_USER_IDS must be a comma-separated list of "
                f"integers, got {item!r}"
            )
        ids.append(int(item))
    return ids


def apply_environment_overrides(
    app: dict,
    telegram: dict,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Copy recognized environment variables into the live config dicts.

    Returns the names of the variables that were applied, never their values.
    """

    env = os.environ if environ is None else environ
    applied: list[str] = []

    def take(target: dict, key: str, names: tuple[str, ...], cast: Callable | None = None):
        for name in names:
            raw = env.get(name)
            if raw is None or raw.strip() == "":
                continue
            target[key] = cast(raw) if cast else raw.strip()
            applied.append(name)
            return

    take(app, "api_key", ("API_KEY", "APP_API_KEY"))
    take(app, "endpoint", ("PUBLIC_BASE_URL", "APP_ENDPOINT"))
    take(app, "ffmpeg_path", ("FFMPEG_PATH",))
    take(app, "imagemagick_path", ("IMAGEMAGICK_PATH", "IMAGEMAGICK_BINARY"))
    take(app, "llm_provider", ("LLM_PROVIDER",))
    take(app, "video_source", ("VIDEO_SOURCE",))
    take(app, "pexels_api_keys", ("PEXELS_API_KEYS", "PEXELS_API_KEY"), _csv)
    take(app, "pixabay_api_keys", ("PIXABAY_API_KEYS", "PIXABAY_API_KEY"), _csv)
    take(app, "task_retention_hours", ("TASK_RETENTION_HOURS",), _int)
    take(app, "max_concurrent_tasks", ("MAX_CONCURRENT_TASKS",), _int)
    take(app, "max_queued_tasks", ("MAX_QUEUED_TASKS",), _int)

    provider_keys = (
        "openai",
        "moonshot",
        "deepseek",
        "gemini",
        "groq",
        "qwen",
        "anthropic",
        "minimax",
        "pollinations",
    )
    for provider in provider_keys:
        upper = provider.upper()
        take(app, f"{provider}_api_key", (f"{upper}_API_KEY",))
        take(app, f"{provider}_base_url", (f"{upper}_BASE_URL",))
        take(app, f"{provider}_model_name", (f"{upper}_MODEL_NAME",))

    take(telegram, "bot_token", ("TELEGRAM_BOT_TOKEN",))
    take(telegram, "mode", ("TELEGRAM_MODE",))
    take(telegram, "webhook_url", ("TELEGRAM_WEBHOOK_URL",))
    take(telegram, "webhook_secret", ("TELEGRAM_WEBHOOK_SECRET",))
    take(telegram, "webhook_path", ("TELEGRAM_WEBHOOK_PATH",))
    take(telegram, "public_base_url", ("TELEGRAM_PUBLIC_BASE_URL", "PUBLIC_BASE_URL"))
    take(telegram, "web_password", ("WEB_PASSWORD", "TELEGRAM_WEB_PASSWORD"))
    take(telegram, "media_token_secret", ("MEDIA_TOKEN_SECRET",))
    take(telegram, "service_secret", ("TELEGRAM_SERVICE_SECRET",))
    take(telegram, "allowed_user_ids", ("TELEGRAM_ALLOWED_USER_IDS",), _user_ids)
    take(telegram, "rate_limit_per_hour", ("TELEGRAM_RATE_LIMIT_PER_HOUR",), _int)
    take(telegram, "enabled", ("TELEGRAM_ENABLED",), _bool)
    take(telegram, "api_base_url", ("MPT_API_BASE_URL",))
    take(telegram, "default_voice", ("TELEGRAM_DEFAULT_VOICE",))
    take(telegram, "default_aspect", ("TELEGRAM_DEFAULT_ASPECT",))
    take(telegram, "default_language", ("TELEGRAM_DEFAULT_LANGUAGE",))
    take(telegram, "default_paragraphs", ("TELEGRAM_DEFAULT_PARAGRAPHS",), _int)
    take(telegram, "default_subtitles", ("TELEGRAM_DEFAULT_SUBTITLES",), _bool)

    # A public URL should also make API download links absolute when the
    # operator did not set a separate endpoint.
    public_base = str(telegram.get("public_base_url") or "").strip()
    if public_base and not str(app.get("endpoint") or "").strip():
        app["endpoint"] = public_base.rstrip("/")

    return applied
