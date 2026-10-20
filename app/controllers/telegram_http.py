"""Pages and the Telegram webhook served by the API process."""

from __future__ import annotations

import os
import secrets
import threading

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

from fastapi import APIRouter

from app.telegram.api_client import bot_request
from app.telegram.runtime import get_api_conversation
from app.telegram.settings import load_telegram_settings
from app.telegram.updates import dispatch_update
from app.utils import utils

router = APIRouter()


def _page_path() -> str:
    return os.path.join(utils.public_dir("miniapp"), "index.html")


@router.get("/miniapp", include_in_schema=False)
@router.get("/miniapp/", include_in_schema=False)
def miniapp_page():
    path = _page_path()
    if not os.path.isfile(path):
        return JSONResponse(
            status_code=500,
            content={"status": 500, "message": "Mini App page is not installed."},
        )
    return FileResponse(path, media_type="text/html")


def _sender(token: str):
    def send(method: str, payload: dict) -> None:
        bot_request(token, method, payload)

    return send


def _watch_later(chat_id: int, task_id: str, topic: str, user_id: int) -> None:
    """Poll an in-process task and push progress back to Telegram."""

    from app.telegram.bot import _deliver

    settings = load_telegram_settings()
    conversation = get_api_conversation()

    async def _run():
        import asyncio

        from telegram import Bot

        bot = Bot(settings.bot_token)
        deadline = asyncio.get_running_loop().time() + settings.task_timeout_seconds
        last = ""
        snapshot = {}
        while asyncio.get_running_loop().time() < deadline:
            try:
                snapshot = await asyncio.to_thread(
                    conversation.pipeline.snapshot, task_id, f"tg:{user_id}"
                )
            except Exception as exc:
                snapshot = {
                    "state": -1,
                    "progress": 0,
                    "error": str(exc),
                    "failed_stage": "status",
                }
            text = conversation.progress_text(topic, snapshot)
            if text != last:
                bot_request(
                    settings.bot_token,
                    "sendMessage",
                    {"chat_id": chat_id, "text": text},
                )
                last = text
            if int(snapshot.get("state") or 0) in {-1, 1}:
                break
            await asyncio.sleep(settings.poll_interval_seconds)
        if int(snapshot.get("state") or 0) == 1:
            await _deliver(bot, chat_id, task_id, snapshot, user_id)

    def _thread():
        import asyncio

        try:
            asyncio.run(_run())
        except Exception as exc:
            logger.exception(f"webhook progress watcher failed: {exc}")

    threading.Thread(target=_thread, name=f"tg-watch-{task_id[:8]}", daemon=True).start()


def _watch_publish_later(chat_id: int, user_id: int, job_ids: list[str]) -> None:
    settings = load_telegram_settings()
    publisher = getattr(get_api_conversation(), "publisher", None)
    if publisher is None:
        return

    def _thread():
        import time

        from app.telegram.publish_flow import describe_job

        pending = set(job_ids)
        deadline = time.time() + settings.task_timeout_seconds
        while pending and time.time() < deadline:
            time.sleep(settings.poll_interval_seconds)
            for job_id in list(pending):
                try:
                    job = publisher.job(f"tg:{user_id}", job_id)
                except Exception as exc:
                    pending.discard(job_id)
                    bot_request(
                        settings.bot_token,
                        "sendMessage",
                        {"chat_id": chat_id, "text": f"Could not read a publish job.\n{exc}"},
                    )
                    continue
                if not job or job.get("state") in {"complete", "failed"}:
                    pending.discard(job_id)
                    if job:
                        bot_request(
                            settings.bot_token,
                            "sendMessage",
                            {"chat_id": chat_id, "text": describe_job(job)},
                        )

    threading.Thread(target=_thread, name="tg-publish", daemon=True).start()


@router.post("/telegram/webhook", include_in_schema=False)
async def telegram_webhook(request: Request):
    settings = load_telegram_settings()
    if not settings.bot_token or settings.mode != "webhook":
        return JSONResponse(
            status_code=404,
            content={"status": 404, "message": "Telegram webhook is not enabled."},
        )
    if settings.webhook_secret:
        presented = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not secrets.compare_digest(
            presented.encode("utf-8"),
            settings.webhook_secret.encode("utf-8"),
        ):
            return JSONResponse(
                status_code=403,
                content={"status": 403, "message": "invalid webhook secret"},
            )
    try:
        update = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"status": 400, "message": "Telegram update must be JSON."},
        )
    if not isinstance(update, dict):
        return JSONResponse(
            status_code=400,
            content={"status": 400, "message": "Telegram update must be an object."},
        )

    conversation = get_api_conversation()
    reply = dispatch_update(update, conversation, _sender(settings.bot_token))
    if reply and reply.watch_task_id:
        message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
        chat = message.get("chat") or {}
        user = (update.get("callback_query") or update.get("message") or {}).get("from") or {}
        chat_id = chat.get("id")
        user_id = user.get("id")
        if isinstance(chat_id, int) and isinstance(user_id, int):
            session = conversation.sessions.get(user_id, {})
            _watch_later(
                chat_id,
                reply.watch_task_id,
                session.get("topic") or "your video",
                user_id,
            )
    if reply and reply.watch_publish_ids:
        message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
        chat = message.get("chat") or {}
        user = (update.get("callback_query") or update.get("message") or {}).get("from") or {}
        chat_id = chat.get("id")
        user_id = user.get("id")
        if isinstance(chat_id, int) and isinstance(user_id, int):
            _watch_publish_later(chat_id, user_id, list(reply.watch_publish_ids))
    return {"ok": True}
