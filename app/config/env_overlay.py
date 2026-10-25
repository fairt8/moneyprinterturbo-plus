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
    *,
    publish: dict | None = None,
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
    take(app, "material_cache_max_mb", ("MATERIAL_CACHE_MAX_MB",), _int)
    take(app, "storage_cap_mb", ("STORAGE_CAP_MB",), _int)
    take(app, "delete_after_delivery", ("DELETE_AFTER_DELIVERY",), _bool)
    take(app, "low_memory", ("MPT_LOW_MEMORY",), _bool)

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
    take(telegram, "admin_user_ids", ("TELEGRAM_ADMIN_USER_IDS",), _user_ids)
    take(telegram, "rate_limit_per_hour", ("TELEGRAM_RATE_LIMIT_PER_HOUR",), _int)
    take(telegram, "enabled", ("TELEGRAM_ENABLED",), _bool)
    take(telegram, "api_base_url", ("MPT_API_BASE_URL",))
    take(telegram, "default_voice", ("TELEGRAM_DEFAULT_VOICE",))
    take(telegram, "default_aspect", ("TELEGRAM_DEFAULT_ASPECT",))
    take(telegram, "default_language", ("TELEGRAM_DEFAULT_LANGUAGE",))
    take(telegram, "default_paragraphs", ("TELEGRAM_DEFAULT_PARAGRAPHS",), _int)
    take(telegram, "default_subtitles", ("TELEGRAM_DEFAULT_SUBTITLES",), _bool)

    if publish is not None:
        _apply_publish_environment(publish, env, applied, take)

    _apply_generic_llm(app, env, applied)

    # A public URL should also make API download links absolute when the
    # operator did not set a separate endpoint.
    public_base = str(telegram.get("public_base_url") or "").strip()
    if public_base and not str(app.get("endpoint") or "").strip():
        app["endpoint"] = public_base.rstrip("/")

    return applied


def _apply_generic_llm(app: dict, env: Mapping[str, str], applied: list[str]) -> None:
    """Map LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL onto an OpenAI-compatible provider.

    A custom relay uses provider ``oneapi``. ``compatible`` and
    ``openai_compatible`` are the same provider. When ``LLM_PROVIDER=openai``
    and the OpenAI-specific variables are empty, the generic variables fill
    those fields instead. A different provider is left unchanged.
    """

    key = env.get("LLM_API_KEY", "").strip()
    base = env.get("LLM_BASE_URL", "").strip().rstrip("/")
    model = env.get("LLM_MODEL", "").strip()
    if not key and not base and not model:
        return

    # These three variables are the generic relay. They override the provider
    # saved in config.toml unless LLM_PROVIDER names a different vendor.
    explicit = env.get("LLM_PROVIDER", "").strip().lower()
    if explicit and explicit not in {"oneapi", "compatible", "openai_compatible", "openai"}:
        return
    if explicit == "openai":
        target = "openai"
    else:
        target = "oneapi"
        app["llm_provider"] = "oneapi"
        if "LLM_PROVIDER" not in applied:
            applied.append("LLM_PROVIDER")

    if key and not str(app.get(f"{target}_api_key") or "").strip():
        app[f"{target}_api_key"] = key
        applied.append("LLM_API_KEY")
    if base and not str(app.get(f"{target}_base_url") or "").strip():
        app[f"{target}_base_url"] = base
        applied.append("LLM_BASE_URL")
    if model and not str(app.get(f"{target}_model_name") or "").strip():
        app[f"{target}_model_name"] = model
        applied.append("LLM_MODEL")


def _apply_publish_environment(publish: dict, env: Mapping[str, str], applied: list[str], take) -> None:
    take(publish, "tiktok_client_key", ("TIKTOK_CLIENT_KEY",))
    take(publish, "tiktok_client_secret", ("TIKTOK_CLIENT_SECRET",))
    take(publish, "tiktok_redirect_uri", ("TIKTOK_REDIRECT_URI",))
    take(publish, "tiktok_audited", ("TIKTOK_AUDITED",), _bool)
    take(publish, "tiktok_default_mode", ("TIKTOK_POST_MODE",))
    take(publish, "tiktok_daily_posts", ("TIKTOK_DAILY_POSTS_PER_ACCOUNT",), _int)
    take(publish, "youtube_redirect_uri", ("YOUTUBE_REDIRECT_URI",))
    take(publish, "stagger_seconds", ("PUBLISH_STAGGER_SECONDS",), _int)
    take(publish, "max_concurrent_jobs", ("PUBLISH_MAX_CONCURRENT_JOBS",), _int)
    take(publish, "vary_copy", ("PUBLISH_VARY_COPY",), _bool)
    take(publish, "public_base_url", ("PUBLIC_BASE_URL",))

    projects = _youtube_projects_from_env(env)
    if projects is not None:
        publish["youtube_projects"] = projects
        if env.get("YOUTUBE_CLIENT_ID", "").strip() or env.get("YOUTUBE_CLIENT_SECRET", "").strip():
            applied.append("YOUTUBE_CLIENT_ID")
        if env.get("YOUTUBE_PROJECTS", "").strip():
            applied.append("YOUTUBE_PROJECTS")
        if env.get("YOUTUBE_DAILY_UPLOAD_QUOTA", "").strip():
            applied.append("YOUTUBE_DAILY_UPLOAD_QUOTA")


def _youtube_projects_from_env(env: Mapping[str, str]) -> list[dict] | None:
    client_id = env.get("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = env.get("YOUTUBE_CLIENT_SECRET", "").strip()
    extra = env.get("YOUTUBE_PROJECTS", "").strip()
    quota_raw = env.get("YOUTUBE_DAILY_UPLOAD_QUOTA", "").strip()
    if not client_id and not client_secret and not extra:
        return None
    quota = _int(quota_raw) if quota_raw else 6
    projects = [
        {
            "id": "default",
            "client_id": client_id,
            "client_secret": client_secret,
            "daily_upload_quota": max(1, quota),
        }
    ]
    for slug in _csv(extra):
        project_id = slug.strip().lower()
        if not project_id or project_id == "default":
            continue
        if not project_id.replace("-", "").isalnum() or not project_id[0].isalpha():
            raise ValueError(
                "YOUTUBE_PROJECTS entries must be short slugs such as studio-b, "
                f"got {slug!r}"
            )
        prefix = "YOUTUBE_" + project_id.upper().replace("-", "_") + "_"
        project_quota = env.get(prefix + "DAILY_UPLOAD_QUOTA", "").strip()
        projects.append(
            {
                "id": project_id,
                "client_id": env.get(prefix + "CLIENT_ID", "").strip(),
                "client_secret": env.get(prefix + "CLIENT_SECRET", "").strip(),
                "daily_upload_quota": _int(project_quota) if project_quota else quota,
            }
        )
    return projects
