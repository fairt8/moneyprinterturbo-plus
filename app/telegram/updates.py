"""Turn a Telegram update JSON object into bot replies and outgoing API calls.

Used by the API webhook so tests can exercise the same path as production
without opening a socket to Telegram.
"""

from __future__ import annotations

from typing import Callable

from app.telegram.logic import Conversation, Reply


Sender = Callable[[str, dict], None]


def _user_and_chat(container: dict) -> tuple[int | None, int | None, str]:
    user = container.get("from") or {}
    chat = container.get("chat") or {}
    user_id = user.get("id")
    chat_id = chat.get("id")
    chat_type = chat.get("type") or "private"
    if not isinstance(user_id, int):
        return None, None, chat_type
    if not isinstance(chat_id, int):
        chat_id = None
    return user_id, chat_id, chat_type


def _send_reply(sender: Sender, chat_id: int, reply: Reply, *, edit_message_id: int | None = None) -> None:
    payload: dict = {"chat_id": chat_id, "text": reply.text}
    if reply.buttons:
        keyboard = []
        for row in reply.buttons:
            built_row = []
            for button in row:
                if button.get("web_app_url"):
                    built_row.append(
                        {
                            "text": button["text"],
                            "web_app": {"url": button["web_app_url"]},
                        }
                    )
                else:
                    built_row.append(
                        {
                            "text": button["text"],
                            "callback_data": button["callback_data"],
                        }
                    )
            keyboard.append(built_row)
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    if edit_message_id is not None:
        payload["message_id"] = edit_message_id
        sender("editMessageText", payload)
    else:
        sender("sendMessage", payload)


def dispatch_update(update: dict, conversation: Conversation, sender: Sender) -> Reply | None:
    """Handle one update. Returns the reply when one was produced."""

    callback = update.get("callback_query")
    if isinstance(callback, dict):
        user_id, _chat_id, chat_type = _user_and_chat(callback)
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if user_id is None or not isinstance(chat_id, int):
            return None
        sender("answerCallbackQuery", {"callback_query_id": callback.get("id")})
        reply = conversation.on_callback(user_id, str(callback.get("data") or ""), chat_type)
        message_id = message.get("message_id") if isinstance(message.get("message_id"), int) else None
        _send_reply(sender, chat_id, reply, edit_message_id=message_id)
        return reply

    message = update.get("message")
    if not isinstance(message, dict):
        return None
    user_id, chat_id, chat_type = _user_and_chat(message)
    if user_id is None or chat_id is None:
        return None
    text = message.get("text") or ""
    command = _command(text)
    if command == "start":
        reply = conversation.on_start(user_id, chat_type)
    elif command == "help":
        reply = conversation.on_help(user_id, chat_type)
    elif command == "new":
        reply = conversation.on_new(user_id, chat_type)
    elif command == "settings":
        reply = conversation.on_settings(user_id, chat_type)
    elif command == "status":
        reply = conversation.on_status(user_id, chat_type)
    elif text.startswith("/"):
        reply = Reply("Unknown command. Use /help to see what I understand.")
    else:
        reply = conversation.on_text(user_id, text, chat_type)
    _send_reply(sender, chat_id, reply)
    return reply


def _command(text: str) -> str:
    if not text.startswith("/"):
        return ""
    name = text.split()[0][1:]
    if "@" in name:
        name = name.split("@", 1)[0]
    return name.lower()
