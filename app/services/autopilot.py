"""Unattended Shorts: pick a topic, render it, send it to Telegram.

Runs as one background thread inside the API process, so it needs no host
cron or systemd. Configuration comes from the environment (.env):

    AUTO_SCHEDULE_ENABLED=true
    AUTO_SCHEDULE_TIMES=09:00,18:00         local times, 24h
    AUTO_SCHEDULE_TZ=Africa/Lagos
    AUTO_SCHEDULE_PER_DAY=2                 uses the first N times
    AUTO_SCHEDULE_NICHE=personal finance and money tips
    AUTO_SCHEDULE_CHAT_ID=1318404489        defaults to the first admin id
    AUTO_SCHEDULE_VOICE=gemini:Charon-Informative
    AUTO_SCHEDULE_SOURCE=gemini_image
    AUTO_PUBLISH_ENABLED=false              post to every linked YouTube/TikTok
    AUTO_PUBLISH_PRIVACY=public

Videos are rendered one at a time. State (topics used, slots done, pending
jobs) lives in storage/autopilot/state.json so restarts neither repeat nor
lose work.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from loguru import logger

from app.config import config
from app.utils import utils

_STATE_LOCK = threading.Lock()
_WAKE = threading.Event()
_started = False
_lock_handle = None
_current: dict[str, Any] = {}

DEFAULT_NICHE = "personal finance and money tips"
SCRIPT_PROMPT = (
    "Write a YouTube Shorts voiceover of 95 to 130 words (about 40 to 50 seconds). "
    "Open with a one-sentence hook that creates curiosity. Use short, punchy, "
    "concrete sentences a beginner understands, with one practical example. "
    "End with a one-line takeaway and a call to follow for more money tips. "
    "Plain spoken English only: no headings, emojis, hashtags, lists, or stage directions. "
    "Educational only, no promises of guaranteed returns."
)


# ------------------------------------------------------------ settings


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def settings() -> dict[str, Any]:
    times = [t.strip() for t in _env("AUTO_SCHEDULE_TIMES", "09:00,18:00").split(",") if t.strip()]
    valid = [t for t in times if re.fullmatch(r"\d{1,2}:\d{2}", t)]
    try:
        per_day = max(0, min(int(_env("AUTO_SCHEDULE_PER_DAY", str(len(valid) or 2))), 12))
    except ValueError:
        per_day = len(valid)
    chat_id = _env("AUTO_SCHEDULE_CHAT_ID")
    if not chat_id:
        admins = (config.telegram or {}).get("admin_user_ids") or (config.telegram or {}).get("allowed_user_ids") or []
        chat_id = str(admins[0]) if admins else ""
    return {
        "enabled": _flag("AUTO_SCHEDULE_ENABLED", False),
        "times": valid[:per_day] if per_day else [],
        "tz": _env("AUTO_SCHEDULE_TZ", "Africa/Lagos"),
        "niche": _env("AUTO_SCHEDULE_NICHE", DEFAULT_NICHE),
        "chat_id": chat_id,
        "voice": _env("AUTO_SCHEDULE_VOICE", "gemini:Charon-Informative"),
        "source": _env("AUTO_SCHEDULE_SOURCE", "gemini_image"),
        "aspect": _env("AUTO_SCHEDULE_ASPECT", "9:16"),
        "auto_publish": _flag("AUTO_PUBLISH_ENABLED", False),
        "publish_privacy": _env("AUTO_PUBLISH_PRIVACY", "public"),
        "grace_hours": float(_env("AUTO_SCHEDULE_GRACE_HOURS", "0.5") or 0.5),
    }


def _tz(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        # Slim images may lack tzdata. Lagos has no DST, so UTC+1 is exact.
        offset = float(_env("AUTO_SCHEDULE_UTC_OFFSET", "1"))
        return timezone(timedelta(hours=offset))


# --------------------------------------------------------------- state


def _dir() -> str:
    path = os.path.join(utils.storage_dir(), "autopilot")
    os.makedirs(path, exist_ok=True)
    return path


def _state_path() -> str:
    return os.path.join(_dir(), "state.json")


def _load() -> dict[str, Any]:
    try:
        with open(_state_path(), encoding="utf-8") as handle:
            data = json.load(handle)
            if isinstance(data, dict):
                data.setdefault("topics", [])
                data.setdefault("slots", [])
                data.setdefault("pending", [])
                data.setdefault("history", [])
                return data
    except (OSError, ValueError):
        pass
    return {"topics": [], "slots": [], "pending": [], "history": []}


def _save(data: dict[str, Any]) -> None:
    data["topics"] = data.get("topics", [])[-120:]
    data["slots"] = data.get("slots", [])[-60:]
    data["history"] = data.get("history", [])[-40:]
    tmp = _state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, _state_path())


def enqueue(
    count: int = 1, topics: list[str] | None = None, reason: str = "manual", keep: bool = False
) -> list[dict]:
    items = []
    topics = [t.strip() for t in (topics or []) if isinstance(t, str) and t.strip()]
    with _STATE_LOCK:
        data = _load()
        for index in range(max(count, len(topics))):
            item = {
                "id": f"ap{int(time.time() * 1000) % 10**10}{index}",
                "topic": topics[index][:300] if index < len(topics) else "",
                "reason": reason,
                "keep": bool(keep),
                "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            data["pending"].append(item)
            items.append(item)
        _save(data)
    _WAKE.set()
    return items


def status() -> dict[str, Any]:
    with _STATE_LOCK:
        data = _load()
    conf = settings()
    return {
        "running": _started,
        "enabled": conf["enabled"],
        "times": conf["times"],
        "tz": conf["tz"],
        "niche": conf["niche"],
        "auto_publish": conf["auto_publish"],
        "current": dict(_current),
        "pending": data["pending"],
        "history": data["history"][-12:],
        "recent_topics": data["topics"][-12:],
    }


# ------------------------------------------------------------- topics


def _normalize(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", topic.lower()).strip()


def pick_topic(niche: str, used: list[str]) -> str:
    from app.services import llm

    recent = used[-60:]
    seen = {_normalize(t) for t in used}
    listing = "\n".join(f"- {t}" for t in recent) or "- (none yet)"
    prompt = (
        f"You run a faceless YouTube Shorts channel about {niche}.\n"
        "Suggest ONE fresh video topic that performs well with a broad audience: "
        "a specific, curiosity-driven angle (for example a numbered list, a common "
        "mistake, a surprising fact, or a simple rule). Keep it under 12 words. "
        "It must be clearly different from every topic already used:\n"
        f"{listing}\n\n"
        'Reply with only JSON: {"topic": "..."}'
    )
    for _ in range(4):
        try:
            response = llm._generate_response(prompt)
        except Exception as exc:
            logger.warning(f"autopilot topic request failed: {type(exc).__name__}")
            response = ""
        if not isinstance(response, str) or response.startswith("Error: "):
            continue
        match = re.search(r"\{.*\}", response, flags=re.DOTALL)
        topic = ""
        if match:
            try:
                topic = str(json.loads(match.group(0)).get("topic") or "").strip()
            except ValueError:
                topic = ""
        if not topic:
            topic = response.strip().strip('"').splitlines()[0][:120]
        if topic and _normalize(topic) not in seen:
            return topic[:200]
    fallback = [
        "The 50/30/20 budget rule explained in 40 seconds",
        "Why paying yourself first builds wealth",
        "3 signs you are living beyond your means",
        "How compound interest turns small savings into big money",
        "The emergency fund rule most people ignore",
    ]
    for topic in fallback:
        if _normalize(topic) not in seen:
            return topic
    return f"{fallback[0]} ({datetime.now().strftime('%b %d')})"


# ------------------------------------------------------------ delivery


def _bot_token() -> str:
    return str((config.telegram or {}).get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()


def _tg(method: str, **kwargs) -> dict:
    token = _bot_token()
    if not token:
        raise RuntimeError("no Telegram bot token configured")
    response = requests.post(f"https://api.telegram.org/bot{token}/{method}", timeout=300, **kwargs)
    try:
        body = response.json()
    except ValueError:
        body = {"ok": False, "description": f"HTTP {response.status_code}"}
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {str(body.get('description'))[:200]}")
    return body


def _copy_text(meta: dict, topic: str) -> str:
    title = (meta.get("title") or topic).strip()
    caption = (meta.get("caption") or "").strip()
    tags = " ".join(meta.get("hashtags") or [])
    return (
        "Ready to post\n\n"
        f"TITLE:\n{title}\n\n"
        f"DESCRIPTION:\n{caption}\n\n"
        f"HASHTAGS:\n{tags}"
    )


def deliver(chat_id: str, video_path: str, meta: dict, topic: str, task_id: str) -> None:
    limit = 49_000_000
    caption = (meta.get("title") or topic)[:900]
    size = os.path.getsize(video_path)
    if size <= limit:
        with open(video_path, "rb") as handle:
            try:
                _tg(
                    "sendVideo",
                    data={"chat_id": chat_id, "caption": caption, "supports_streaming": "true"},
                    files={"video": ("short.mp4", handle, "video/mp4")},
                )
            except RuntimeError:
                handle.seek(0)
                _tg(
                    "sendDocument",
                    data={"chat_id": chat_id, "caption": caption},
                    files={"document": ("short.mp4", handle, "video/mp4")},
                )
    else:
        from app.controllers.v1.miniapp import _signed_download

        link = _signed_download(task_id) or "(no public download link configured)"
        _tg("sendMessage", data={"chat_id": chat_id, "text": f"{caption}\nToo large for Telegram: {link}"})
    _tg("sendMessage", data={"chat_id": chat_id, "text": _copy_text(meta, topic)[:4000]})


def auto_publish(owner: str, task_id: str, meta: dict, conf: dict) -> list[dict]:
    """Queue the video on every linked YouTube/TikTok account (hook for later).

    Returns the queued jobs, or [] when publishing is off or no account is linked.
    """

    if not conf.get("auto_publish"):
        return []
    try:
        from app.publish import service
        from app.publish.store import get_store

        if not get_store().accounts_for(owner):
            logger.info("autopilot: auto-publish is on but no account is linked yet")
            return []
        return service.enqueue(
            owner,
            {
                "task_id": task_id,
                "all": True,
                "title": meta.get("title") or "",
                "description": meta.get("caption") or "",
                "hashtags": meta.get("hashtags") or [],
                "privacy": conf.get("publish_privacy") or "public",
            },
        )
    except Exception as exc:
        logger.warning(f"autopilot: auto-publish failed: {type(exc).__name__}: {str(exc)[:200]}")
        return []


# ----------------------------------------------------------------- run


def _local_request():
    from starlette.requests import Request

    return Request(
        {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": "/api/v1/videos",
            "raw_path": b"/api/v1/videos", "query_string": b"", "headers": [],
            "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 80),
        }
    )


def produce(item: dict, conf: dict) -> dict:
    from app.controllers.v1.video import create_task
    from app.models.schema import TaskVideoRequest
    from app.services import llm
    from app.services import state as sm
    from app.telegram.jobs import task_video_path

    with _STATE_LOCK:
        data = _load()
        used = list(data["topics"])
    topic = item.get("topic") or pick_topic(conf["niche"], used)
    with _STATE_LOCK:
        data = _load()
        data["topics"].append(topic)
        _save(data)
    _current.update(id=item["id"], topic=topic, stage="rendering", started=time.time())
    logger.info(f"autopilot: producing {topic!r}")

    body = TaskVideoRequest(
        video_subject=topic,
        video_aspect=conf["aspect"],
        voice_name=conf["voice"],
        video_source=conf["source"],
        video_language="en",
        paragraph_number=1,
        subtitle_enabled=True,
        video_script_prompt=SCRIPT_PROMPT,
    )
    result = create_task(_local_request(), body, stop_at="video")
    task_id = ((result or {}).get("data") or {}).get("task_id")
    if not task_id:
        raise RuntimeError("task was not created")
    owner = f"tg:{conf['chat_id']}" if conf["chat_id"] else "admin"
    try:
        from app.telegram.runtime import get_job_store

        get_job_store().bind(task_id, owner)
    except Exception:
        pass
    _current["task_id"] = task_id

    deadline = time.time() + 60 * 60
    task = {}
    while time.time() < deadline:
        task = sm.state.get_task(task_id) or {}
        state = int(task.get("state") or 0)
        if state in (1, -1):
            break
        time.sleep(10)
    state = int(task.get("state") or 0)
    if state != 1:
        raise RuntimeError(f"render failed: {str(task.get('error') or 'timeout')[:300]}")

    video_path = task_video_path(task_id)
    if not video_path:
        raise RuntimeError("render finished but no video file was found")
    script = str(task.get("script") or "")
    details: dict = {}
    try:
        with open(os.path.join(utils.task_dir(task_id), "script.json"), encoding="utf-8") as handle:
            details = json.load(handle) or {}
        script = script or str(details.get("script") or "")
    except (OSError, ValueError):
        pass
    try:
        meta = llm.generate_social_metadata(video_subject=topic, video_script=script, platform="youtube")
    except Exception:
        meta = {"title": topic, "caption": "", "hashtags": ["#money", "#personalfinance", "#shorts"]}
    with open(os.path.join(utils.task_dir(task_id), "autopilot.json"), "w", encoding="utf-8") as handle:
        json.dump({"topic": topic, "meta": meta}, handle, ensure_ascii=False)

    record = {"id": item["id"], "topic": topic, "task_id": task_id, "size": os.path.getsize(video_path)}
    visuals = details.get("ai_visuals") if isinstance(details.get("ai_visuals"), dict) else {}
    record["visuals"] = visuals.get("image_providers") or ("stock fallback" if visuals else "stock")
    record["voice"] = (details.get("tts_fallback") or {}).get("used") or details.get("tts_voice_used") or conf["voice"]
    record["title"] = meta.get("title") or topic
    jobs = auto_publish(owner, task_id, meta, conf)
    record["publish_jobs"] = len(jobs)
    if conf["chat_id"]:
        _current["stage"] = "delivering"
        deliver(conf["chat_id"], video_path, meta, topic, task_id)
        record["delivered"] = True
    # "keep" holds the file until the retention window so an operator can copy it.
    hold = bool(item.get("keep")) or _flag("AUTOPILOT_KEEP_FILES", False)
    if not jobs and not hold:
        try:
            from app.services.storage_hygiene import release_task_after_delivery

            release_task_after_delivery(task_id)
        except Exception as exc:
            logger.warning(f"autopilot: cleanup failed: {exc}")
    return record


def _due_slots(conf: dict, done: list[str]) -> list[str]:
    tz = _tz(conf["tz"])
    now = datetime.now(tz)
    due = []
    for day_offset in (1, 0):
        day = (now - timedelta(days=day_offset)).date()
        for hhmm in conf["times"]:
            hour, minute = (int(x) for x in hhmm.split(":"))
            slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
            key = slot.strftime("%Y-%m-%dT%H:%M")
            if key in done:
                continue
            if slot <= now <= slot + timedelta(hours=conf["grace_hours"]):
                due.append(key)
    return due


def _loop() -> None:
    while True:
        try:
            conf = settings()
            if conf["enabled"]:
                with _STATE_LOCK:
                    data = _load()
                    due = _due_slots(conf, data["slots"])
                    if due:
                        data["slots"].extend(due)
                        _save(data)
                for key in due:
                    enqueue(1, reason=f"schedule {key}")
            item = None
            with _STATE_LOCK:
                data = _load()
                if data["pending"]:
                    item = data["pending"][0]
            if item:
                record = {"id": item["id"], "topic": item.get("topic", ""), "reason": item.get("reason")}
                try:
                    record.update(produce(item, conf))
                    record["status"] = "done"
                except Exception as exc:
                    logger.exception(f"autopilot run failed: {exc}")
                    record["status"] = "failed"
                    record["error"] = str(exc)[:300]
                    record["topic"] = _current.get("topic") or record["topic"]
                    if conf["chat_id"]:
                        try:
                            _tg("sendMessage", data={
                                "chat_id": conf["chat_id"],
                                "text": f"Auto video failed: {record['topic']}\n{record['error']}",
                            })
                        except Exception:
                            pass
                record["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                with _STATE_LOCK:
                    data = _load()
                    data["pending"] = [p for p in data["pending"] if p.get("id") != item["id"]]
                    data["history"].append(record)
                    _save(data)
                _current.clear()
                continue
        except Exception as exc:
            logger.exception(f"autopilot loop error: {exc}")
        _WAKE.wait(30)
        _WAKE.clear()


def start() -> bool:
    """Start the worker once per container (a file lock keeps it single)."""

    global _started, _lock_handle
    if _started:
        return True
    try:
        handle = open(os.path.join(_dir(), ".lock"), "w")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("autopilot worker already running in another process")
        return False
    _lock_handle = handle
    thread = threading.Thread(target=_loop, name="autopilot", daemon=True)
    thread.start()
    _started = True
    conf = settings()
    logger.info(
        f"autopilot worker started: enabled={conf['enabled']}, times={conf['times']}, tz={conf['tz']}"
    )
    return True
