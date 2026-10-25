"""Confirm an OpenAI-compatible relay lists the configured model.

The check never logs the API key. A relay that does not implement ``GET /models``
is reported as a warning so a strict gateway can still be used for scripts.
"""

from __future__ import annotations

import json
from typing import Callable
from urllib.parse import urlsplit

import requests
from loguru import logger

from app.models.llm_provider import get_llm_provider


Fetch = Callable[[str, dict], tuple[int, str]]
_LIST_LIMIT = 12


def _host(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if not parsed.scheme or not parsed.netloc:
        return base_url
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def _models_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.endswith("/models"):
        return base
    return base + "/models"


def _default_fetch(url: str, headers: dict) -> tuple[int, str]:
    response = requests.get(url, headers=headers, timeout=15)
    return response.status_code, response.text or ""


def _preview(ids: list[str]) -> str:
    shown = ids[:_LIST_LIMIT]
    extra = len(ids) - len(shown)
    text = ", ".join(shown) if shown else "(none)"
    if extra > 0:
        text += f", and {extra} more"
    return text


def check_llm_configuration(app_config: dict, *, fetch: Fetch | None = None) -> dict:
    """Return a status report. The API key is not copied into the result."""

    provider_id = str(app_config.get("llm_provider") or "").strip().lower() or "moonshot"
    provider = get_llm_provider(provider_id)
    if provider is None:
        return {
            "status": "error",
            "provider": provider_id,
            "model": "",
            "base_url": "",
            "message": f"LLM provider '{provider_id}' is not supported.",
        }

    api_key = str(app_config.get(provider.config_key("api_key")) or "").strip()
    configured_base = str(app_config.get(provider.config_key("base_url")) or "")
    base_url = provider.resolve_base_url(configured_base).strip()
    model = provider.resolve_model_name(
        str(app_config.get(provider.config_key("model_name")) or "")
    )
    public_base = _host(base_url)

    if provider.adapter != "openai_compatible":
        return {
            "status": "skipped",
            "provider": provider.provider_id,
            "model": model,
            "base_url": public_base,
            "message": (
                "LLM startup model list runs for OpenAI-compatible providers. "
                f"Current provider is {provider.provider_id}, model {model or '(default)'}."
            ),
        }

    if not api_key:
        return {
            "status": "skipped",
            "provider": provider.provider_id,
            "model": model,
            "base_url": public_base,
            "message": (
                "LLM startup check skipped: no API key is configured. "
                "Set LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL for an OpenAI-compatible relay."
            ),
        }
    if not base_url:
        return {
            "status": "error",
            "provider": provider.provider_id,
            "model": model,
            "base_url": "",
            "message": (
                "LLM base URL is empty. Set LLM_BASE_URL to the relay's "
                "OpenAI-compatible URL, including /v1."
            ),
        }
    if not model:
        return {
            "status": "error",
            "provider": provider.provider_id,
            "model": "",
            "base_url": public_base,
            "message": (
                f"LLM model name is empty for {public_base}. Set LLM_MODEL to a "
                "model the relay lists."
            ),
        }

    fetch = fetch or _default_fetch
    url = _models_url(base_url)
    try:
        status, body = fetch(url, {"Authorization": f"Bearer {api_key}"})
    except requests.RequestException as exc:
        return {
            "status": "error",
            "provider": provider.provider_id,
            "model": model,
            "base_url": public_base,
            "message": (
                f"Could not reach the model list at {public_base}: {exc.__class__.__name__}. "
                "Check LLM_BASE_URL and that this server can open that host."
            ),
        }

    if status == 401 or status == 403:
        return {
            "status": "error",
            "provider": provider.provider_id,
            "model": model,
            "base_url": public_base,
            "message": (
                f"The relay rejected the API key (HTTP {status}) for {public_base}. "
                "Check LLM_API_KEY and LLM_BASE_URL."
            ),
        }
    if status == 404:
        return {
            "status": "warning",
            "provider": provider.provider_id,
            "model": model,
            "base_url": public_base,
            "message": (
                f"Configured model '{model}' at {public_base}. The relay has no "
                "GET /models list (HTTP 404), so the name was not verified."
            ),
        }
    if status >= 400:
        return {
            "status": "error",
            "provider": provider.provider_id,
            "model": model,
            "base_url": public_base,
            "message": (
                f"Model list at {public_base} returned HTTP {status}. "
                f"Configured model is '{model}'."
            ),
        }

    try:
        payload = json.loads(body or "{}")
    except json.JSONDecodeError:
        return {
            "status": "error",
            "provider": provider.provider_id,
            "model": model,
            "base_url": public_base,
            "message": (
                f"Model list at {public_base} was not JSON. Configured model is '{model}'."
            ),
        }

    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {
            "status": "warning",
            "provider": provider.provider_id,
            "model": model,
            "base_url": public_base,
            "message": (
                f"Configured model '{model}' at {public_base}. The relay's model "
                "list had no data array, so the name was not verified."
            ),
        }

    ids = [str(item.get("id")) for item in rows if isinstance(item, dict) and item.get("id")]
    if model not in ids:
        return {
            "status": "error",
            "provider": provider.provider_id,
            "model": model,
            "base_url": public_base,
            "listed_models": ids[:_LIST_LIMIT],
            "message": (
                f"Configured model '{model}' is not in the list from {public_base}. "
                f"The relay listed: {_preview(ids)}."
            ),
        }
    return {
        "status": "ok",
        "provider": provider.provider_id,
        "model": model,
        "base_url": public_base,
        "listed_models": ids[:_LIST_LIMIT],
        "message": (
            f"LLM ready: provider {provider.provider_id}, model '{model}' "
            f"at {public_base}. Listed models include {_preview(ids)}."
        ),
    }


def log_llm_startup_check() -> dict:
    from app.config import config

    report = check_llm_configuration(config.app)
    message = str(report.get("message") or "")
    if report.get("status") in {"error"}:
        logger.error(message)
    elif report.get("status") == "warning":
        logger.warning(message)
    else:
        logger.info(message)
    return report
