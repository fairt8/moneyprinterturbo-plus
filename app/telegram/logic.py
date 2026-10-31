"""Conversation flow for the Telegram bot.

Handlers return plain replies so tests can run without a Telegram connection.
The polling and webhook adapters turn those replies into Bot API calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.telegram.presets import (
    ASPECTS,
    FOOTAGE_SOURCES,
    LANGUAGES,
    VALID_ASPECTS,
    VALID_FOOTAGE_SOURCES,
    VALID_LANGUAGES,
    VOICE_NAME_RE_SOURCE,
    VOICES,
    describe_stage,
)
from app.telegram.rate_limit import RateLimiter
from app.telegram.settings import TelegramSettings
from app.telegram.store import SessionStore


class PipelineError(Exception):
    """The video pipeline rejected or could not find a task."""


@dataclass
class JobOptions:
    aspect: str = "9:16"
    voice: str = "en-US-AriaNeural"
    paragraphs: int = 1
    language: str = ""
    subtitles: bool = True
    video_source: str = "pexels"


@dataclass
class Reply:
    text: str
    buttons: list[list[dict]] | None = None
    watch_task_id: str | None = None
    watch_publish_ids: list[str] | None = None


_VOICE_RE = re.compile(VOICE_NAME_RE_SOURCE)
_PRIVATE_ONLY = "Send me a private message so the finished video stays between us."
_APPROVED_ONLY = "This bot is limited to approved Telegram accounts."


def _clip(text: str, limit: int = 3500) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


@dataclass
class Conversation:
    settings: TelegramSettings
    sessions: SessionStore
    rate_limiter: RateLimiter
    pipeline: object
    publisher: object | None = None
    _defaults: dict = field(init=False)

    def __post_init__(self) -> None:
        from app.telegram.publish_flow import PublishFlow

        self.publish_flow = PublishFlow(self)
        self._defaults = {
            "aspect": self.settings.default_aspect
            if self.settings.default_aspect in VALID_ASPECTS
            else "9:16",
            "voice": self.settings.default_voice or "en-US-AriaNeural",
            "paragraphs": self.settings.default_paragraphs,
            "language": self.settings.default_language
            if self.settings.default_language in VALID_LANGUAGES
            else "",
            "subtitles": self.settings.default_subtitles,
            "video_source": self.settings.video_source or "pixabay",
            "topic": "",
            "task_id": "",
            "awaiting": "",
        }

    def on_start(self, user_id: int, chat_type: str = "private") -> Reply:
        blocked = self._guard(user_id, chat_type)
        if blocked:
            return blocked
        return Reply(
            "Send a topic and I'll write a script, record a voiceover, pull stock "
            "footage, and send the short video back here.\n\n"
            "Commands: /new topic, /settings, /status, /help.",
            buttons=self._start_buttons(),
        )

    def on_help(self, user_id: int, chat_type: str = "private") -> Reply:
        blocked = self._guard(user_id, chat_type)
        if blocked:
            return blocked
        limit = self.settings.rate_limit_per_hour
        limit_line = (
            "There is no hourly limit configured."
            if limit <= 0
            else f"You can start {limit} video(s) per hour."
        )
        return Reply(
            "How this works\n"
            "1. Send a topic, for example: a calm tour of a night market.\n"
            "2. Check the frame, voice, length, language, and subtitles.\n"
            "3. Press Generate. I'll post progress in this chat.\n"
            "4. When it finishes I send the video, or a download link if it is "
            "too large for Telegram.\n\n"
            "/new clears the topic. /settings shows the current options. "
            "/status checks the latest video. /accounts links YouTube channels "
            "and TikTok accounts and chooses who receives a finished video. "
            "/storage shows disk usage. An admin can send /storage purge.\n"
            f"{limit_line}",
            buttons=self._start_buttons(),
        )

    def on_new(self, user_id: int, chat_type: str = "private") -> Reply:
        blocked = self._guard(user_id, chat_type)
        if blocked:
            return blocked
        session = self._load(user_id)
        session["topic"] = ""
        session["awaiting"] = "topic"
        self.sessions.save(user_id, session)
        return Reply("What should the next video be about? Send the topic in one message.")

    def on_settings(self, user_id: int, chat_type: str = "private") -> Reply:
        blocked = self._guard(user_id, chat_type)
        if blocked:
            return blocked
        session = self._load(user_id)
        return Reply(self._summary(session), buttons=self._summary_buttons(session))

    def on_storage(self, user_id: int, argument: str = "", chat_type: str = "private") -> Reply:
        blocked = self._guard(user_id, chat_type)
        if blocked:
            return blocked
        if not self.settings.is_admin(user_id):
            return Reply(
                "Only an admin can inspect storage. Set TELEGRAM_ADMIN_USER_IDS "
                "to your numeric Telegram id, or set TELEGRAM_ALLOWED_USER_IDS."
            )
        from app.services.storage_hygiene import format_report, purge_storage, report_storage

        if (argument or "").strip().lower() == "purge":
            report = purge_storage()
            removed = len(report.get("removed_tasks") or [])
            return Reply(
                f"Purged {removed} rendered video(s). OAuth account tokens were kept.\n\n"
                + format_report(report)
            )
        return Reply(format_report(report_storage()))

    def on_status(self, user_id: int, chat_type: str = "private") -> Reply:
        blocked = self._guard(user_id, chat_type)
        if blocked:
            return blocked
        session = self._load(user_id)
        task_id = session.get("task_id") or ""
        if not task_id:
            return Reply("You don't have a video in progress. Send a topic to start one.")
        try:
            snapshot = self.pipeline.snapshot(task_id, f"tg:{user_id}")
        except PipelineError as exc:
            return Reply(str(exc))
        return Reply(self._progress_text(session.get("topic") or "your video", snapshot))

    def on_text(self, user_id: int, text: str, chat_type: str = "private") -> Reply:
        blocked = self._guard(user_id, chat_type)
        if blocked:
            return blocked
        session = self._load(user_id)
        body = (text or "").strip()
        if not body:
            return Reply("Send a topic, or use /help.")
        publish_reply = self.publish_flow.on_text(user_id, body)
        if publish_reply is not None:
            return publish_reply
        if session.get("awaiting") == "voice":
            if not _VOICE_RE.match(body) or len(body) > 80:
                return Reply(
                    "Voice names look like en-US-AriaNeural. Send one Edge TTS voice name, "
                    "or press a voice button."
                )
            session["voice"] = body
            session["awaiting"] = ""
            self.sessions.save(user_id, session)
            return Reply(self._summary(session), buttons=self._summary_buttons(session))

        if len(body) > 500:
            return Reply("Keep the topic under 500 characters.")
        session["topic"] = body
        session["awaiting"] = ""
        self.sessions.save(user_id, session)
        return Reply(self._summary(session), buttons=self._summary_buttons(session))

    def on_callback(self, user_id: int, data: str, chat_type: str = "private") -> Reply:
        blocked = self._guard(user_id, chat_type)
        if blocked:
            return blocked
        session = self._load(user_id)
        data = (data or "").strip()

        publish_reply = self.publish_flow.on_callback(user_id, data)
        if publish_reply is not None:
            return publish_reply
        if data == "go":
            return self._generate(user_id, session)
        if data == "menu":
            session["awaiting"] = ""
            self.sessions.save(user_id, session)
            return Reply(self._summary(session), buttons=self._summary_buttons(session))
        if data == "aspect":
            return Reply("Choose a frame.", buttons=self._choice_buttons("a", ASPECTS))
        if data == "voice":
            rows = self._choice_buttons("v", VOICES)
            rows.append([{"text": "Type another voice", "callback_data": "voice-custom"}])
            rows.append([{"text": "Back", "callback_data": "menu"}])
            return Reply("Choose a voice.", buttons=rows)
        if data == "voice-custom":
            session["awaiting"] = "voice"
            self.sessions.save(user_id, session)
            return Reply("Send an Edge TTS voice name, for example en-US-JennyNeural.")
        if data == "paras":
            choices = tuple((str(n), f"{n} paragraph" if n == 1 else f"{n} paragraphs") for n in range(1, 5))
            return Reply("How long should the script be?", buttons=self._choice_buttons("p", choices))
        if data == "lang":
            labeled = tuple((code or "auto", label) for code, label in LANGUAGES)
            return Reply("Which language should the script use?", buttons=self._choice_buttons("l", labeled))
        if data == "footage":
            return Reply(
                "Choose stock footage. Pixabay is the usual primary source. "
                "Openverse and Wikimedia Commons add CC0 and public-domain files "
                "when an earlier source comes up short.",
                buttons=self._choice_buttons("src", FOOTAGE_SOURCES),
            )
        if data == "subs":
            return Reply(
                "Subtitles",
                buttons=[
                    [
                        {"text": "Subtitles on", "callback_data": "s:1"},
                        {"text": "Subtitles off", "callback_data": "s:0"},
                    ],
                    [{"text": "Back", "callback_data": "menu"}],
                ],
            )

        if data.startswith("a:") and data[2:] in VALID_ASPECTS:
            session["aspect"] = data[2:]
        elif data.startswith("v:") and _VOICE_RE.match(data[2:]):
            session["voice"] = data[2:]
        elif data.startswith("p:") and data[2:].isdigit():
            session["paragraphs"] = min(10, max(1, int(data[2:])))
        elif data.startswith("l:"):
            language = "" if data[2:] == "auto" else data[2:]
            if language not in VALID_LANGUAGES:
                return Reply("That language is not in the list.")
            session["language"] = language
        elif data == "s:1":
            session["subtitles"] = True
        elif data == "s:0":
            session["subtitles"] = False
        elif data.startswith("src:") and data[4:] in VALID_FOOTAGE_SOURCES:
            session["video_source"] = data[4:]
        else:
            return Reply("That button is no longer valid. Use /settings to start again.")

        session["awaiting"] = ""
        self.sessions.save(user_id, session)
        return Reply(self._summary(session), buttons=self._summary_buttons(session))

    def progress_text(self, topic: str, snapshot: dict) -> str:
        return self._progress_text(topic, snapshot)

    def _generate(self, user_id: int, session: dict) -> Reply:
        topic = (session.get("topic") or "").strip()
        if len(topic) < 2:
            return Reply("Send a topic first. A few words is enough.")
        owner = f"tg:{user_id}"
        if not self.rate_limiter.allow(owner):
            limit = self.settings.rate_limit_per_hour
            return Reply(
                f"You've started {limit} video(s) in the last hour. "
                "Wait a bit, then try again."
            )
        options = JobOptions(
            aspect=session["aspect"],
            voice=session["voice"],
            paragraphs=int(session["paragraphs"]),
            language=session.get("language") or "",
            subtitles=bool(session.get("subtitles", True)),
            video_source=session.get("video_source") or self.settings.video_source,
        )
        try:
            task_id = self.pipeline.submit(owner, topic, options)
        except PipelineError as exc:
            return Reply(_clip(str(exc)))
        session["task_id"] = task_id
        session["awaiting"] = ""
        self.sessions.save(user_id, session)
        return Reply(
            f"Queued “{topic}”.\nWriting the script… 0%",
            watch_task_id=task_id,
        )

    def _guard(self, user_id: int, chat_type: str) -> Reply | None:
        if chat_type != "private":
            return Reply(_PRIVATE_ONLY)
        if not self.settings.is_user_allowed(user_id):
            return Reply(_APPROVED_ONLY)
        return None

    def _load(self, user_id: int) -> dict:
        return self.sessions.get(user_id, self._defaults)

    def _summary(self, session: dict) -> str:
        topic = session.get("topic") or "(send a topic)"
        language = session.get("language") or "auto"
        subtitles = "on" if session.get("subtitles", True) else "off"
        footage = session.get("video_source") or self.settings.video_source
        return (
            f"Topic: {topic}\n"
            f"Frame: {session.get('aspect')}\n"
            f"Voice: {session.get('voice')}\n"
            f"Length: {session.get('paragraphs')} paragraph(s)\n"
            f"Language: {language}\n"
            f"Subtitles: {subtitles}\n"
            f"Footage: {footage}\n\n"
            "Press Generate, or change one option."
        )

    def _summary_buttons(self, session: dict) -> list[list[dict]]:
        rows: list[list[dict]] = []
        if (session.get("topic") or "").strip():
            rows.append([{"text": "Generate", "callback_data": "go"}])
        rows.extend(
            [
                [
                    {"text": "Frame", "callback_data": "aspect"},
                    {"text": "Voice", "callback_data": "voice"},
                ],
                [
                    {"text": "Length", "callback_data": "paras"},
                    {"text": "Language", "callback_data": "lang"},
                ],
                [{"text": "Subtitles", "callback_data": "subs"}],
                [{"text": "Footage", "callback_data": "footage"}],
                [{"text": "Accounts", "callback_data": "ac"}],
            ]
        )
        return rows

    def _start_buttons(self) -> list[list[dict]] | None:
        url = self.settings.miniapp_url()
        if not url:
            return None
        return [[{"text": "Open the Mini App", "web_app_url": url}]]

    def _choice_buttons(self, prefix: str, choices: tuple[tuple[str, str], ...]) -> list[list[dict]]:
        rows = []
        row: list[dict] = []
        for value, label in choices:
            row.append({"text": label, "callback_data": f"{prefix}:{value}"})
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([{"text": "Back", "callback_data": "menu"}])
        return rows

    def _progress_text(self, topic: str, snapshot: dict) -> str:
        state = int(snapshot.get("state") or 0)
        progress = int(snapshot.get("progress") or 0)
        stage = describe_stage(progress, state)
        if state == -1:
            failed = snapshot.get("failed_stage") or "pipeline"
            error = snapshot.get("error") or "The video could not be generated."
            return _clip(f"Could not finish “{topic}” during {failed}.\n{error}")
        if state == 1:
            return f"“{topic}” is ready."
        return f"“{topic}”\n{stage}… {progress}%"
