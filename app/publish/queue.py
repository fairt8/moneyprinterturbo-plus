"""Run queued publish jobs one account at a time, with retries and stagger."""

from __future__ import annotations

import os
import threading
import time

from loguru import logger

from app.publish import tiktok, youtube
from app.publish.copy import tiktok_caption
from app.publish.errors import AuthExpired, PublishError, QuotaExceeded, TransientPublishError
from app.publish.settings import load_publish_settings
from app.publish.store import get_store, iso
from app.telegram.jobs import task_video_path

_started = False
_start_lock = threading.Lock()


def start_publish_worker() -> None:
    if os.getenv("MPT_PUBLISH_WORKER", "1") == "0":
        logger.info("publish worker is disabled")
        return
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="publish-worker", daemon=True).start()
    logger.info("publish worker started")


def _loop() -> None:
    while True:
        try:
            settings = load_publish_settings()
            job = get_store().claim_next(time.time(), settings.max_concurrent_jobs)
            if job is None:
                time.sleep(1.5)
                continue
            run_job(job["id"])
        except Exception as exc:
            logger.exception(f"publish worker error: {exc}")
            time.sleep(2)


def run_job(job_id: str) -> dict:
    store = get_store()
    settings = load_publish_settings()
    job = store.job_secret(job_id)
    if job is None:
        raise PublishError("Publish job not found.")
    if job.get("state") in {"complete", "failed"}:
        return store.public_job(job)
    try:
        if job.get("platform") == "youtube":
            _run_youtube(store, settings, job)
        elif job.get("platform") == "tiktok":
            _run_tiktok(store, settings, job)
        else:
            raise PublishError("Unknown platform.")
    except AuthExpired as exc:
        store.mark_reauth(str(job.get("account_id") or ""), True)
        _fail(store, job, str(exc))
    except QuotaExceeded as exc:
        _fail(store, job, str(exc))
    except TransientPublishError as exc:
        _retry_or_fail(store, settings, job, str(exc))
    except PublishError as exc:
        _fail(store, job, str(exc))
    except Exception as exc:
        logger.exception(f"publish job {job_id} failed")
        _retry_or_fail(store, settings, job, f"Unexpected publish error: {exc}")
    latest = store.job_secret(job_id) or job
    if latest.get("state") == "complete":
        try:
            from app.services.storage_hygiene import release_task_after_publish

            release_task_after_publish(str(latest.get("task_id") or ""))
        except Exception as exc:
            logger.warning(f"could not apply delete-after-publish for {job_id}: {exc}")
    return store.public_job(latest)


def _run_youtube(store, settings, job: dict) -> None:
    account = _account(store, job)
    project = settings.youtube_project(str(account.get("project_id") or job.get("project_id") or "default"))
    if project is None or not (project.client_id and project.client_secret):
        raise PublishError(
            "The Google Cloud project for this channel is not configured. "
            "Set its client id and secret, then re-authorize the channel."
        )
    if account.get("needs_reauth"):
        raise AuthExpired(f"{_label(job)} needs to be linked again.")
    path = _video(job)
    reserved = False
    counted = bool(job.get("quota_counted"))
    if not counted:
        if not store.reserve_quota("youtube", project.id, project.daily_upload_quota):
            raise QuotaExceeded(
                f"YouTube project '{project.id}' has used today's upload allowance "
                f"({project.daily_upload_quota}). The default Data API quota is 10,000 units "
                "and videos.insert costs 1,600, about 6 uploads. Request a higher quota in "
                "Google Cloud, or add another project with YOUTUBE_PROJECTS and link this channel to it."
            )
        reserved = True
        job["quota_counted"] = True
        store.replace_job(job)
    try:
        token, expires_at = youtube.ensure_access_token(account, project)
        store.save_tokens(account["id"], access_token=token, expires_at=expires_at, refresh_token=str(account.get("refresh_token") or ""))
        description = job.get("description") or ""
        tags = [str(tag).lstrip("#") for tag in job.get("hashtags") or [] if str(tag).strip()]
        hashtag_line = " ".join(str(tag) for tag in job.get("hashtags") or [])
        if hashtag_line and hashtag_line not in description:
            description = (description + "\n\n" + hashtag_line).strip()
        try:
            uploaded = youtube.upload_video(
                access_token=token,
                path=path,
                title=str(job.get("title") or "Short"),
                description=description,
                tags=tags,
                privacy=str(job.get("privacy") or "public"),
                session_url=str(job.get("upload_url") or ""),
                offset=int(job.get("upload_offset") or 0),
            )
        except AuthExpired:
            token, expires_at = youtube.ensure_access_token({**account, "expires_at": 0}, project)
            store.save_tokens(account["id"], access_token=token, expires_at=expires_at)
            uploaded = youtube.upload_video(
                access_token=token,
                path=path,
                title=str(job.get("title") or "Short"),
                description=description,
                tags=tags,
                privacy=str(job.get("privacy") or "public"),
                session_url="",
                offset=0,
            )
    except Exception:
        if reserved and not job.get("upload_url"):
            store.release_quota("youtube", project.id)
            job["quota_counted"] = False
        raise
    job["state"] = "complete"
    job["phase"] = "done"
    job["youtube_url"] = youtube.watch_url(uploaded["video_id"], bool(job.get("shorts")))
    job["error"] = ""
    job["updated_at"] = iso()
    job["upload_url"] = ""
    store.replace_job(job)


def _run_tiktok(store, settings, job: dict) -> None:
    if not settings.tiktok_configured():
        raise PublishError("TikTok is not configured. Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET.")
    account = _account(store, job)
    if account.get("needs_reauth"):
        raise AuthExpired(f"{_label(job)} needs to be linked again.")
    path = _video(job)
    mode = str(job.get("tiktok_mode") or settings.tiktok_default_mode)
    privacy = tiktok.privacy_level(str(job.get("privacy") or "private"), audited=settings.tiktok_audited, mode=mode)
    reserved = False
    if not job.get("quota_counted"):
        if not store.reserve_quota("tiktok", account["id"], settings.tiktok_daily_posts):
            raise QuotaExceeded(
                f"TikTok account '{_label(job)}' has reached the local daily cap of "
                f"{settings.tiktok_daily_posts} posts. TikTok also applies its own per-account "
                "rate limit. Change TIKTOK_DAILY_POSTS_PER_ACCOUNT to match your app, and set "
                "PUBLISH_STAGGER_SECONDS so posts are not sent in a burst."
            )
        reserved = True
        job["quota_counted"] = True
        store.replace_job(job)
    try:
        token, expires_at, refresh, refresh_expires = tiktok.ensure_access_token(account, settings)
        store.save_tokens(
            account["id"],
            access_token=token,
            expires_at=expires_at,
            refresh_token=refresh,
            refresh_expires_at=refresh_expires,
        )
        if not job.get("remote_publish_id"):
            caption = tiktok_caption(job.get("title") or "", job.get("description") or "", list(job.get("hashtags") or []))
            started = tiktok.init_upload(
                access_token=token,
                mode=mode,
                caption=caption,
                privacy_level=privacy,
                path=path,
            )
            job["remote_publish_id"] = started["publish_id"]
            job["upload_url"] = started["upload_url"]
            job["upload_ranges"] = started["ranges"]
            job["phase"] = "upload"
            job["upload_offset"] = 0
            store.replace_job(job)
        if job.get("phase") != "poll":
            tiktok.upload_chunks(
                upload_url=str(job.get("upload_url") or ""),
                path=path,
                ranges=[tuple(item) for item in job.get("upload_ranges") or tiktok.chunk_ranges(os.path.getsize(path))],
                start_index=int(job.get("upload_offset") or 0),
            )
            job["phase"] = "poll"
            store.replace_job(job)
        status = tiktok.poll_status(token, str(job.get("remote_publish_id") or ""))
    except Exception:
        if reserved and not job.get("remote_publish_id"):
            store.release_quota("tiktok", account["id"])
            job["quota_counted"] = False
            store.replace_job(job)
        raise
    job["tiktok_status"] = status.get("status") or ""
    job["tiktok_post_id"] = status.get("post_id") or ""
    job["updated_at"] = iso()
    if status.get("status") == "FAILED":
        job["upload_url"] = ""
        store.replace_job(job)
        raise PublishError(status.get("fail_reason") or "TikTok failed to publish this video.")
    if status.get("status") in {"PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"}:
        job["state"] = "complete"
        job["phase"] = "done"
        job["error"] = ""
        job["upload_url"] = ""
        store.replace_job(job)
        return
    if int(job.get("attempts") or 1) >= settings.max_attempts:
        _fail(
            store,
            job,
            "TikTok is still processing this post. Check the TikTok app. "
            f"Publish id: {job.get('remote_publish_id') or 'unknown'}.",
        )
        return
    job["state"] = "queued"
    job["phase"] = "poll"
    job["not_before"] = time.time() + _retry_delay(int(job.get("attempts") or 1))
    job["error"] = "TikTok is still processing this post."
    store.replace_job(job)


def _account(store, job: dict) -> dict:
    account = store.account_secret(str(job.get("account_id") or ""))
    if account is None or account.get("owner") != job.get("owner"):
        raise PublishError("The account for this job is no longer linked.")
    return account


def _video(job: dict) -> str:
    path = task_video_path(str(job.get("task_id") or ""))
    if not path:
        raise PublishError("The video file is no longer available.")
    return path


def _fail(store, job: dict, message: str) -> None:
    job["state"] = "failed"
    job["error"] = message
    job["updated_at"] = iso()
    job["upload_url"] = ""
    store.replace_job(job)


def _retry_or_fail(store, settings, job: dict, message: str) -> None:
    attempts = int(job.get("attempts") or 1)
    if attempts >= settings.max_attempts:
        _fail(store, job, message)
        return
    job["state"] = "queued"
    job["error"] = message
    job["not_before"] = time.time() + _retry_delay(attempts)
    job["updated_at"] = iso()
    store.replace_job(job)
    logger.warning(f"publish job {job.get('id')} will retry: {message}")


def _retry_delay(attempt: int) -> float:
    try:
        base = float(os.getenv("MPT_PUBLISH_RETRY_BACKOFF", "15"))
    except ValueError:
        base = 15.0
    return max(0.0, base) * max(1, attempt)


def _label(job: dict) -> str:
    return str(job.get("account_label") or job.get("account_id") or "account")
