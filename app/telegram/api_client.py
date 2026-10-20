"""Telegram Bot API calls that do not go through the long-polling library."""

from __future__ import annotations

from loguru import logger

import requests


def bot_request(token: str, method: str, payload: dict | None = None, timeout: int = 20) -> dict:
    if not token:
        raise RuntimeError("Telegram bot token is not configured.")
    url = f"https://api.telegram.org/bot{token}/{method}"
    response = requests.post(url, json=payload or {}, timeout=timeout)
    try:
        body = response.json()
    except ValueError:
        body = {"ok": False, "description": response.text[:300]}
    if response.status_code >= 400 or not body.get("ok", False):
        description = body.get("description") if isinstance(body, dict) else response.text
        logger.warning(f"Telegram API {method} failed: {description}")
    return body if isinstance(body, dict) else {"ok": False}


def register_integration(settings) -> None:
    """Publish the Mini App button and, in webhook mode, the webhook URL.

    Failures are logged and do not stop the API. Polling mode must not set a
    webhook, or getUpdates will be rejected.
    """

    if not settings.enabled or not settings.bot_token:
        return

    miniapp = settings.miniapp_url()
    if miniapp:
        bot_request(
            settings.bot_token,
            "setChatMenuButton",
            {
                "menu_button": {
                    "type": "web_app",
                    "text": "Create",
                    "web_app": {"url": miniapp},
                }
            },
        )
        logger.info("Telegram menu button points at the Mini App")

    if settings.mode == "webhook" and settings.webhook_on_api:
        if not settings.webhook_url:
            logger.warning(
                "Telegram mode is webhook, but TELEGRAM_WEBHOOK_URL is empty. "
                "Updates will not arrive until you set it."
            )
            return
        payload = {
            "url": settings.webhook_url,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": False,
        }
        if settings.webhook_secret:
            payload["secret_token"] = settings.webhook_secret
        bot_request(settings.bot_token, "setWebhook", payload)
        logger.info("Telegram webhook registered")


def delete_webhook(settings) -> None:
    if not settings.bot_token:
        return
    bot_request(settings.bot_token, "deleteWebhook", {"drop_pending_updates": False})
    logger.info("Telegram webhook cleared for long polling")
