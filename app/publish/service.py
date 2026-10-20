"""Link accounts, group them into targets, and queue one job per account."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from app.publish import copy as copywriting
from app.publish.errors import PublishRejected
from app.publish.oauth import begin
from app.publish.settings import PublishSettings, load_publish_settings
from app.publish.store import PublishStore, get_store, iso, new_id
from app.publish.tokens import issue_connect_token

PRIVACY = {"public", "unlisted", "private"}
TIKTOK_MODES = {"direct", "inbox"}


def setup_view(owner: str, settings: PublishSettings | None = None, store: PublishStore | None = None) -> dict:
    settings = settings or load_publish_settings()
    store = store or get_store()
    return {
        "youtube_configured": settings.youtube_configured(),
        "tiktok_configured": settings.tiktok_configured(),
        "tiktok_audited": settings.tiktok_audited,
        "tiktok_default_mode": settings.tiktok_default_mode,
        "youtube_redirect_uri": settings.youtube_redirect_uri,
        "tiktok_redirect_uri": settings.tiktok_redirect_uri,
        "projects": [
            {"id": project.id, "daily_upload_quota": project.daily_upload_quota}
            for project in settings.youtube_projects
        ],
        "stagger_seconds": settings.stagger_seconds,
        "vary_copy_default": settings.vary_copy_default,
        "max_concurrent_jobs": settings.max_concurrent_jobs,
        "quota": store.quota_snapshot(owner, settings),
        "accounts": store.accounts_for(owner),
        "targets": store.targets_for(owner),
    }


def connect_url(
    owner: str,
    platform: str,
    *,
    label: str = "",
    project_id: str = "default",
    account_id: str = "",
    secret: str,
    settings: PublishSettings | None = None,
    store: PublishStore | None = None,
) -> str:
    settings = settings or load_publish_settings()
    store = store or get_store()
    if not settings.public_base_url:
        raise PublishRejected("Set PUBLIC_BASE_URL so the provider can redirect back to this server.")
    token = issue_connect_token(secret, owner, ttl_seconds=settings.connect_ttl_seconds)
    path = "/auth/youtube/start" if platform in {"youtube", "yt"} else "/auth/tiktok/start"
    from urllib.parse import urlencode

    query = urlencode(
        {
            "connect": token,
            "label": label,
            "project_id": project_id,
            "account_id": account_id,
        }
    )
    return f"{settings.public_base_url}{path}?{query}"


def provider_authorize_url(
    owner: str,
    platform: str,
    *,
    label: str = "",
    project_id: str = "default",
    account_id: str = "",
    settings: PublishSettings | None = None,
    store: PublishStore | None = None,
) -> str:
    settings = settings or load_publish_settings()
    store = store or get_store()
    return begin(
        store,
        settings,
        owner=owner,
        platform=platform,
        label=label,
        project_id=project_id,
        account_id=account_id,
    )


def rename_account(owner: str, account_id: str, label: str, store: PublishStore | None = None) -> dict:
    cleaned = " ".join((label or "").split())
    if not cleaned or len(cleaned) > 80:
        raise PublishRejected("Use a label between 1 and 80 characters.")
    updated = (store or get_store()).rename_account(owner, account_id, cleaned)
    if updated is None:
        raise PublishRejected("That account is not on this profile.", status_code=404)
    return updated


def unlink_account(owner: str, account_id: str, store: PublishStore | None = None) -> None:
    if not (store or get_store()).delete_account(owner, account_id):
        raise PublishRejected("That account is not on this profile.", status_code=404)


def save_target(
    owner: str,
    name: str,
    account_ids: list[str],
    *,
    target_id: str = "",
    is_default: bool = False,
    store: PublishStore | None = None,
) -> dict:
    store = store or get_store()
    cleaned = " ".join((name or "").split())
    if not cleaned or len(cleaned) > 80:
        raise PublishRejected("Use a target name between 1 and 80 characters.")
    known = {item["id"] for item in store.accounts_for(owner)}
    chosen = []
    for account_id in account_ids:
        if account_id not in known:
            raise PublishRejected(f"Account {account_id} is not on this profile.")
        if account_id not in chosen:
            chosen.append(account_id)
    if not chosen:
        raise PublishRejected("Pick at least one account for this target.")
    if target_id:
        existing = next((item for item in store.targets_for(owner) if item["id"] == target_id), None)
        if existing is None:
            raise PublishRejected("That target is not on this profile.", status_code=404)
    record = {
        "id": target_id or new_id("g"),
        "owner": owner,
        "name": cleaned,
        "account_ids": chosen,
        "is_default": bool(is_default),
    }
    return store.save_target(record)


def set_default_target(owner: str, target_id: str, store: PublishStore | None = None) -> dict:
    updated = (store or get_store()).set_default_target(owner, target_id)
    if updated is None:
        raise PublishRejected("That target is not on this profile.", status_code=404)
    return updated


def delete_target(owner: str, target_id: str, store: PublishStore | None = None) -> None:
    if not (store or get_store()).delete_target(owner, target_id):
        raise PublishRejected("That target is not on this profile.", status_code=404)


def preview_copy(owner: str, task_id: str, platform: str, *, store: PublishStore | None = None) -> dict:
    _require_video(owner, task_id)
    brief = load_brief(task_id)
    platform = "youtube" if platform in {"youtube", "yt", "youtube_shorts"} else "tiktok"
    generated = copywriting.generate_copy(
        subject=brief["subject"],
        script=brief["script"],
        platform=platform,
        language=brief["language"],
    )
    shorts = platform == "youtube" and brief["aspect"] in {"9:16", "1:1", ""}
    return copywriting.finalize_copy(
        platform=platform,
        title=generated["title"],
        description=generated["description"],
        hashtags=generated["hashtags"],
        shorts=shorts,
    ) | {"shorts": shorts, "aspect": brief["aspect"]}


def enqueue(
    owner: str,
    payload: dict,
    *,
    settings: PublishSettings | None = None,
    store: PublishStore | None = None,
) -> list[dict]:
    settings = settings or load_publish_settings()
    store = store or get_store()
    task_id = str(payload.get("task_id") or "").strip()
    _require_video(owner, task_id)
    accounts = _selected_accounts(store, owner, payload)
    if not accounts:
        raise PublishRejected("Pick at least one linked account.")
    privacy = str(payload.get("privacy") or "public").strip().lower()
    if privacy not in PRIVACY:
        raise PublishRejected("Privacy must be public, unlisted, or private.")
    mode = str(payload.get("tiktok_mode") or settings.tiktok_default_mode).strip().lower()
    if mode not in TIKTOK_MODES:
        raise PublishRejected("TikTok mode must be direct or inbox.")
    stagger = payload.get("stagger_seconds", settings.stagger_seconds)
    try:
        stagger = int(stagger)
    except (TypeError, ValueError):
        raise PublishRejected("Stagger must be a number of seconds.") from None
    if stagger < 0 or stagger > 3600:
        raise PublishRejected("Stagger must be between 0 and 3600 seconds.")
    vary = bool(payload.get("vary_copy", settings.vary_copy_default))
    shorts = payload.get("shorts")
    brief = load_brief(task_id)
    if shorts is None:
        shorts = brief["aspect"] in {"9:16", "1:1", ""}
    supplied_title = str(payload.get("title") or "").strip()
    supplied_description = str(payload.get("description") or "").strip()
    supplied_tags = _hashtags(payload.get("hashtags"))
    copies = _copies(
        accounts,
        brief=brief,
        title=supplied_title,
        description=supplied_description,
        hashtags=supplied_tags,
        vary=vary,
        shorts=bool(shorts),
    )
    now = time.time()
    stamp = iso()
    jobs = []
    for index, account in enumerate(accounts):
        text = copies[account["id"]]
        needs = account.get("needs_reauth")
        jobs.append(
            {
                "id": new_id("j", 6),
                "owner": owner,
                "task_id": task_id,
                "account_id": account["id"],
                "account_label": account["label"],
                "platform": account["platform"],
                "project_id": account.get("project_id") or "",
                "state": "queued",
                "phase": "init",
                "attempts": 0,
                "title": text["title"],
                "description": text["description"],
                "hashtags": text["hashtags"],
                "privacy": privacy,
                "tiktok_mode": mode,
                "shorts": bool(shorts) and account["platform"] == "youtube",
                "youtube_url": "",
                "tiktok_status": "",
                "tiktok_post_id": "",
                "remote_publish_id": "",
                "upload_url": "",
                "upload_offset": 0,
                "error": "This account needs to be linked again." if needs else "",
                "not_before": now + index * stagger,
                "created_at": stamp,
                "updated_at": stamp,
            }
        )
        if needs:
            jobs[-1]["state"] = "failed"
    return store.add_jobs(jobs)


def _copies(accounts, *, brief, title, description, hashtags, vary, shorts) -> dict:
    grouped: dict[str, list[dict]] = {}
    for account in accounts:
        grouped.setdefault(account["platform"], []).append(account)
    result = {}
    for platform, group in grouped.items():
        for index, account in enumerate(group, start=1):
            if vary and len(group) > 1:
                generated = copywriting.generate_copy(
                    subject=title or brief["subject"],
                    script=description or brief["script"],
                    platform=platform,
                    language=brief["language"],
                    variation=index,
                    total=len(group),
                )
                text = copywriting.finalize_copy(
                    platform=platform,
                    title=generated["title"],
                    description=generated["description"],
                    hashtags=generated["hashtags"],
                    shorts=shorts,
                )
            elif title:
                text = copywriting.finalize_copy(
                    platform=platform,
                    title=title,
                    description=description,
                    hashtags=hashtags,
                    shorts=shorts,
                )
            else:
                generated = copywriting.generate_copy(
                    subject=brief["subject"],
                    script=brief["script"],
                    platform=platform,
                    language=brief["language"],
                )
                text = copywriting.finalize_copy(
                    platform=platform,
                    title=generated["title"],
                    description=generated["description"],
                    hashtags=generated["hashtags"] or hashtags,
                    shorts=shorts,
                )
            result[account["id"]] = text
    return result


def _selected_accounts(store: PublishStore, owner: str, payload: dict) -> list[dict]:
    catalog = {item["id"]: item for item in store.accounts_for(owner)}
    secrets = {item_id: store.account_secret(item_id) for item_id in catalog}
    wanted: list[str] = []
    if payload.get("all"):
        wanted = list(catalog)
    elif payload.get("target_id"):
        target = next((item for item in store.targets_for(owner) if item["id"] == payload["target_id"]), None)
        if target is None:
            raise PublishRejected("That publish target is not on this profile.", status_code=404)
        wanted = list(target["account_ids"])
    else:
        raw = payload.get("account_ids") or []
        if isinstance(raw, str):
            raw = [raw]
        wanted = [str(item) for item in raw]
    platform = str(payload.get("platform") or "").strip().lower()
    if platform in {"yt", "youtube_shorts"}:
        platform = "youtube"
    if platform in {"tt"}:
        platform = "tiktok"
    selected = []
    for account_id in wanted:
        public = catalog.get(account_id)
        secret = secrets.get(account_id)
        if public is None or secret is None:
            raise PublishRejected(f"Account {account_id} is not on this profile.", status_code=404)
        if platform and public["platform"] != platform:
            continue
        merged = dict(public)
        merged["needs_reauth"] = bool(secret.get("needs_reauth"))
        selected.append(merged)
    return selected


def _require_video(owner: str, task_id: str) -> str:
    if not task_id or "/" in task_id or ".." in task_id:
        raise PublishRejected("Missing a video task id.")
    from app.telegram.jobs import task_video_path
    from app.telegram.runtime import get_job_store

    path = task_video_path(task_id)
    if not path:
        raise PublishRejected("That video is not ready to publish.", status_code=404)
    if owner != "admin":
        stored = get_job_store().owner_of(task_id)
        if stored != owner:
            raise PublishRejected("That video is not on this profile.", status_code=404)
    return path


def load_brief(task_id: str) -> dict:
    subject = ""
    script = ""
    aspect = "9:16"
    language = ""
    path = Path(os.path.join(_task_dir(task_id), "script.json"))
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            script = str(data.get("script") or "")
            params = data.get("params") if isinstance(data.get("params"), dict) else {}
            subject = str(params.get("video_subject") or "")
            aspect = str(params.get("video_aspect") or aspect)
            language = str(params.get("video_language") or "")
    try:
        from app.services import state as sm

        task = sm.state.get_task(task_id) or {}
    except Exception:
        task = {}
    if isinstance(task, dict):
        subject = subject or str(task.get("video_subject") or "")
    return {
        "subject": subject or "short video",
        "script": script,
        "aspect": aspect or "9:16",
        "language": language,
    }


def _task_dir(task_id: str) -> str:
    from app.utils import utils

    return utils.task_dir(task_id)


def _hashtags(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in value.replace(",", " ").split() if part.strip()]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []
