"""Long-polling and standalone webhook entrypoint.

Run with ``python -m app.telegram``. Webhook updates can also be served by the
API process; in that mode this process stays idle so Docker does not restart it.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading

from loguru import logger

from app.telegram.api_client import delete_webhook, register_integration
from app.telegram.delivery import plan_local_delivery, plan_remote_delivery
from app.telegram.http_pipeline import download_to
from app.telegram.logic import Reply
from app.telegram.runtime import get_conversation
from app.telegram.settings import load_telegram_settings
from app.telegram.tokens import MediaTokenError, issue_media_token


def _markup(buttons):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

    if not buttons:
        return None
    rows = []
    for row in buttons:
        built = []
        for button in row:
            if button.get("web_app_url"):
                built.append(
                    InlineKeyboardButton(
                        button["text"],
                        web_app=WebAppInfo(url=button["web_app_url"]),
                    )
                )
            else:
                built.append(
                    InlineKeyboardButton(
                        button["text"],
                        callback_data=button["callback_data"],
                    )
                )
        rows.append(built)
    return InlineKeyboardMarkup(rows)


def _chat_type(update) -> str:
    chat = update.effective_chat
    return getattr(chat, "type", "private") or "private"


def _user_id(update) -> int | None:
    user = update.effective_user
    if user is None:
        return None
    return int(user.id)


async def _send(update, reply: Reply):
    message = update.effective_message
    if message is None:
        return None
    markup = _markup(reply.buttons)
    if update.callback_query and update.callback_query.message:
        try:
            return await update.callback_query.edit_message_text(
                reply.text,
                reply_markup=markup,
            )
        except Exception:
            return await message.reply_text(reply.text, reply_markup=markup)
    return await message.reply_text(reply.text, reply_markup=markup)


def _download_url(settings, task_id: str) -> str | None:
    secret = settings.media_secret()
    if not settings.public_base_url or not secret:
        return None
    try:
        token = issue_media_token(
            secret,
            task_id,
            ttl_seconds=settings.media_token_ttl_seconds,
        )
    except MediaTokenError:
        return None
    return f"{settings.public_base_url}/api/v1/media/{task_id}?access={token}"


async def _deliver(bot, chat_id: int, task_id: str, snapshot: dict, user_id: int | None = None) -> None:
    settings = load_telegram_settings()
    link = _download_url(settings, task_id)
    files = [item for item in snapshot.get("files") or [] if isinstance(item, str)]
    if not files:
        if link:
            await bot.send_message(chat_id, f"Your video is ready.\n{link}")
        else:
            await bot.send_message(
                chat_id,
                "The video finished, but I could not find a file to send.",
            )
        return

    target = files[0]
    temp_path = ""
    try:
        if target.startswith(("http://", "https://")):
            decision = plan_remote_delivery(
                content_length=None,
                upload_limit=settings.max_upload_bytes,
                download_url=link,
            )
            # Content-Length is checked inside download_to. Start the download
            # only when we might still upload it.
            fd, temp_path = tempfile.mkstemp(prefix="mpt-tg-", suffix=".mp4")
            os.close(fd)
            headers = {}
            if settings.api_key:
                headers["X-API-Key"] = settings.api_key
            _size, too_big = await asyncio.to_thread(
                download_to,
                target,
                temp_path,
                headers,
                limit=settings.max_upload_bytes,
            )
            if too_big:
                decision = "link"
            else:
                decision = "upload"
                target = temp_path
            if decision == "link":
                if link:
                    await bot.send_message(
                        chat_id,
                        "The video is larger than Telegram allows me to upload. "
                        f"Download it here:\n{link}",
                    )
                else:
                    await bot.send_message(
                        chat_id,
                        "The video is larger than Telegram allows me to upload, and "
                        "no public download URL is configured.",
                    )
                return
        else:
            planned = plan_local_delivery(
                target,
                upload_limit=settings.max_upload_bytes,
                download_url=link,
            )
            from app.telegram.delivery import LinkDelivery, UnavailableDelivery

            if isinstance(planned, LinkDelivery):
                await bot.send_message(
                    chat_id,
                    f"{planned.reason}\nDownload it here:\n{planned.url}",
                )
                return
            if isinstance(planned, UnavailableDelivery):
                await bot.send_message(chat_id, planned.reason)
                return

        try:
            with open(target, "rb") as handle:
                await bot.send_video(
                    chat_id=chat_id,
                    video=handle,
                    caption="Your short video is ready.",
                    supports_streaming=True,
                )
        except Exception as video_error:
            logger.warning(f"send_video failed, trying a document: {video_error}")
            with open(target, "rb") as handle:
                await bot.send_document(
                    chat_id=chat_id,
                    document=handle,
                    caption="Your short video is ready.",
                    filename="video.mp4",
                )
    except Exception as exc:
        logger.exception(f"failed to deliver video {task_id}: {exc}")
        if link:
            await bot.send_message(
                chat_id,
                "I couldn't upload the video into the chat. Download it here:\n" + link,
            )
        else:
            await bot.send_message(
                chat_id,
                "I couldn't upload the video into the chat, and no download link is available.",
            )
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
    if user_id is not None:
        offer = get_conversation().publish_flow.offer(user_id, task_id)
        await bot.send_message(chat_id, offer.text, reply_markup=_markup(offer.buttons))
        if offer.watch_publish_ids:
            asyncio.create_task(_watch_publish(bot, chat_id, user_id, offer.watch_publish_ids))


async def _watch(bot, chat_id: int, message_id: int, user_id: int, task_id: str, topic: str) -> None:
    settings = load_telegram_settings()
    conversation = get_conversation()
    owner = f"tg:{user_id}"
    deadline = asyncio.get_running_loop().time() + settings.task_timeout_seconds
    last_text = ""
    snapshot: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        try:
            snapshot = await asyncio.to_thread(conversation.pipeline.snapshot, task_id, owner)
        except Exception as exc:
            snapshot = {"state": -1, "progress": 0, "error": str(exc), "failed_stage": "status"}
        text = conversation.progress_text(topic, snapshot)
        if text != last_text:
            try:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
            except Exception:
                pass
            last_text = text
        state = int(snapshot.get("state") or 0)
        if state in {-1, 1}:
            break
        await asyncio.sleep(settings.poll_interval_seconds)
    else:
        await bot.send_message(
            chat_id,
            "This is taking longer than expected. Use /status to check it.",
        )
        return

    if int(snapshot.get("state") or 0) == 1:
        await _deliver(bot, chat_id, task_id, snapshot, user_id)


async def _watch_publish(bot, chat_id: int, user_id: int, job_ids: list[str]) -> None:
    from app.telegram.publish_flow import describe_job

    settings = load_telegram_settings()
    publisher = getattr(get_conversation(), "publisher", None)
    if publisher is None:
        return
    pending = set(job_ids)
    deadline = asyncio.get_running_loop().time() + settings.task_timeout_seconds
    while pending and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(settings.poll_interval_seconds)
        for job_id in list(pending):
            try:
                job = await asyncio.to_thread(publisher.job, f"tg:{user_id}", job_id)
            except Exception as exc:
                pending.discard(job_id)
                await bot.send_message(chat_id, f"Could not read a publish job.\n{exc}")
                continue
            if not job or job.get("state") in {"complete", "failed"}:
                pending.discard(job_id)
                if job:
                    await bot.send_message(chat_id, describe_job(job))
    if pending:
        await bot.send_message(chat_id, "Some publish jobs are still running. Use /accounts to review them.")


def build_application(token: str):
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        MessageHandler,
        filters,
    )

    application = Application.builder().token(token).build()

    async def start(update, context):
        user_id = _user_id(update)
        if user_id is None:
            return
        reply = get_conversation().on_start(user_id, _chat_type(update))
        await _send(update, reply)

    async def help_command(update, context):
        user_id = _user_id(update)
        if user_id is None:
            return
        reply = get_conversation().on_help(user_id, _chat_type(update))
        await _send(update, reply)

    async def new(update, context):
        user_id = _user_id(update)
        if user_id is None:
            return
        reply = get_conversation().on_new(user_id, _chat_type(update))
        await _send(update, reply)

    async def settings_command(update, context):
        user_id = _user_id(update)
        if user_id is None:
            return
        reply = get_conversation().on_settings(user_id, _chat_type(update))
        await _send(update, reply)

    async def accounts(update, context):
        user_id = _user_id(update)
        if user_id is None:
            return
        reply = get_conversation().publish_flow.on_accounts(user_id, _chat_type(update))
        await _send(update, reply)

    async def status(update, context):
        user_id = _user_id(update)
        if user_id is None:
            return
        reply = get_conversation().on_status(user_id, _chat_type(update))
        await _send(update, reply)

    async def text(update, context):
        user_id = _user_id(update)
        if user_id is None or update.message is None:
            return
        reply = get_conversation().on_text(user_id, update.message.text or "", _chat_type(update))
        sent = await _send(update, reply)
        if reply.watch_publish_ids and update.effective_chat is not None:
            asyncio.create_task(
                _watch_publish(
                    context.bot,
                    update.effective_chat.id,
                    user_id,
                    reply.watch_publish_ids,
                )
            )
        if reply.watch_task_id and sent is not None:
            session = get_conversation().sessions.get(user_id, {})
            asyncio.create_task(
                _watch(
                    context.bot,
                    update.effective_chat.id,
                    sent.message_id,
                    user_id,
                    reply.watch_task_id,
                    session.get("topic") or "your video",
                )
            )

    async def callback(update, context):
        query = update.callback_query
        user_id = _user_id(update)
        if query is None or user_id is None:
            return
        await query.answer()
        reply = get_conversation().on_callback(user_id, query.data or "", _chat_type(update))
        sent = await _send(update, reply)
        if reply.watch_publish_ids and update.effective_chat is not None:
            asyncio.create_task(
                _watch_publish(
                    context.bot,
                    update.effective_chat.id,
                    user_id,
                    reply.watch_publish_ids,
                )
            )
        if reply.watch_task_id:
            message = sent or query.message
            message_id = getattr(message, "message_id", None)
            if message_id is not None:
                session = get_conversation().sessions.get(user_id, {})
                asyncio.create_task(
                    _watch(
                        context.bot,
                        update.effective_chat.id,
                        message_id,
                        user_id,
                        reply.watch_task_id,
                        session.get("topic") or "your video",
                    )
                )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("new", new))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("accounts", accounts))
    application.add_handler(CallbackQueryHandler(callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text))
    return application


def main() -> None:
    settings = load_telegram_settings()
    if not settings.bot_token:
        logger.error(
            "TELEGRAM_BOT_TOKEN is empty. Create a bot with @BotFather, put the "
            "token in .env or config.toml, and restart. This process will wait."
        )
        threading.Event().wait()
        return
    if not settings.enabled:
        logger.info("Telegram is disabled (telegram.enabled = false). Waiting.")
        threading.Event().wait()
        return
    if settings.mode == "webhook" and settings.webhook_on_api:
        logger.info(
            "Telegram webhook updates are handled by the API at "
            f"{settings.webhook_path}. This process will not poll."
        )
        register_integration(settings)
        threading.Event().wait()
        return

    from app.telegram.http_pipeline import HttpPipeline
    from app.telegram.runtime import get_conversation as build

    build(HttpPipeline(settings))
    application = build_application(settings.bot_token)
    if settings.mode == "webhook":
        register_integration(settings)
        path = settings.webhook_path.lstrip("/")
        logger.info(
            f"Starting Telegram webhook listener on {settings.webhook_listen}:{settings.webhook_port}"
        )
        application.run_webhook(
            listen=settings.webhook_listen,
            port=settings.webhook_port,
            url_path=path,
            webhook_url=settings.webhook_url or None,
            secret_token=settings.webhook_secret or None,
        )
        return

    delete_webhook(settings)
    register_integration(settings)
    logger.info("Starting Telegram bot in long-polling mode")
    application.run_polling(allowed_updates=["message", "callback_query"])
