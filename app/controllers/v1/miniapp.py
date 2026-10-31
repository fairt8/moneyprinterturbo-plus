"""Mini App and browser endpoints for creating a short video."""

from __future__ import annotations

import os
import re

from fastapi import Body, Query, Request
from fastapi.responses import FileResponse
from loguru import logger

from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.telegram.inprocess import InProcessPipeline
from app.telegram.logic import JobOptions, PipelineError
from app.telegram.presets import (
    ASPECTS,
    FOOTAGE_SOURCES,
    LANGUAGES,
    VALID_ASPECTS,
    VALID_FOOTAGE_SOURCES,
    VALID_LANGUAGES,
    VOICE_NAME_RE_SOURCE,
    VOICES,
)
from app.telegram.runtime import get_job_store, get_rate_limiter
from app.telegram.settings import load_telegram_settings
from app.telegram.tokens import MediaTokenError, issue_media_token, read_media_token
from app.telegram.web_auth import AuthError, Principal, authenticate
from app.telegram.jobs import task_video_path

router = new_router()
_pipeline = InProcessPipeline()
_VOICE_RE = re.compile(VOICE_NAME_RE_SOURCE)


def get_pipeline() -> InProcessPipeline:
    return _pipeline


def _settings():
    return load_telegram_settings()


def _principal(request: Request) -> Principal:
    try:
        return authenticate(request.headers, _settings())
    except AuthError as exc:
        raise HttpException(task_id="", status_code=exc.status_code, message=exc.message) from exc


def _require_owner(principal: Principal, task_id: str) -> None:
    if principal.owner == "admin":
        return
    owner = get_job_store().owner_of(task_id)
    if owner != principal.owner:
        raise HttpException(task_id=task_id, status_code=404, message="No video with that id.")


def _signed_download(task_id: str) -> str | None:
    settings = _settings()
    if not task_video_path(task_id):
        return None
    secret = settings.media_secret()
    if not secret:
        return None
    try:
        token = issue_media_token(
            secret, task_id, ttl_seconds=settings.media_token_ttl_seconds
        )
    except MediaTokenError as exc:
        logger.warning(f"could not sign a download link: {exc}")
        return None
    base = settings.public_base_url
    path = f"/api/v1/media/{task_id}?access={token}"
    return f"{base}{path}" if base else path


@router.get("/miniapp/bootstrap", summary="Mini App form metadata")
def miniapp_bootstrap(request: Request):
    settings = _settings()
    # Bootstrap is public so the page can decide whether to show a password field.
    # It does not reveal whether a specific secret is correct.
    return {
        "status": 200,
        "message": "success",
        "data": {
            "auth_required": settings.auth_required,
            "miniapp_url": settings.miniapp_url(),
            "aspects": [{"value": value, "label": label} for value, label in ASPECTS],
            "voices": [{"value": value, "label": label} for value, label in VOICES],
            "languages": [
                {"value": value, "label": label} for value, label in LANGUAGES
            ],
            "sources": [
                {"value": value, "label": label} for value, label in FOOTAGE_SOURCES
            ],
            "defaults": {
                "video_aspect": settings.default_aspect
                if settings.default_aspect in VALID_ASPECTS
                else "9:16",
                "voice_name": settings.default_voice,
                "paragraph_number": settings.default_paragraphs,
                "video_language": settings.default_language
                if settings.default_language in VALID_LANGUAGES
                else "",
                "subtitle_enabled": settings.default_subtitles,
                "video_source": settings.video_source
                if settings.video_source in VALID_FOOTAGE_SOURCES
                else "pixabay",
            },
        },
    }


@router.post("/miniapp/videos", summary="Create a video from the Mini App or browser")
def create_miniapp_video(request: Request, body: dict = Body(...)):
    principal = _principal(request)
    settings = _settings()
    if not get_rate_limiter(settings).allow(principal.owner):
        raise HttpException(
            task_id="",
            status_code=429,
            message=(
                f"You have started {settings.rate_limit_per_hour} videos in the "
                "last hour. Wait a bit, then try again."
            ),
        )

    if not isinstance(body, dict):
        raise HttpException(task_id="", status_code=400, message="Send a JSON object.")

    topic = str(body.get("video_subject") or "").strip()
    if len(topic) < 2 or len(topic) > 500:
        raise HttpException(
            task_id="",
            status_code=400,
            message="The topic needs between 2 and 500 characters.",
        )

    aspect = str(body.get("video_aspect") or settings.default_aspect)
    if aspect not in VALID_ASPECTS:
        raise HttpException(task_id="", status_code=400, message="Choose a 9:16, 16:9, or 1:1 frame.")

    voice = str(body.get("voice_name") or settings.default_voice).strip()
    if not _VOICE_RE.match(voice) or len(voice) > 80:
        raise HttpException(
            task_id="",
            status_code=400,
            message="Voice names look like en-US-AriaNeural.",
        )

    try:
        paragraphs = int(body.get("paragraph_number") or settings.default_paragraphs)
    except (TypeError, ValueError):
        raise HttpException(task_id="", status_code=400, message="Length must be a number.") from None
    if paragraphs < 1 or paragraphs > 10:
        raise HttpException(task_id="", status_code=400, message="Length must be from 1 to 10 paragraphs.")

    language = str(body.get("video_language") or "")
    if language not in VALID_LANGUAGES:
        raise HttpException(task_id="", status_code=400, message="That language is not supported.")

    subtitles = body.get("subtitle_enabled", True)
    if not isinstance(subtitles, bool):
        raise HttpException(task_id="", status_code=400, message="Subtitles must be true or false.")

    requested_source = body.get("video_source")
    if requested_source is None or str(requested_source).strip() == "":
        video_source = settings.video_source or "pixabay"
    else:
        video_source = str(requested_source).strip()
        if video_source not in VALID_FOOTAGE_SOURCES:
            raise HttpException(
                task_id="",
                status_code=400,
                message="Choose Pixabay, Openverse, Wikimedia Commons, Pexels, or Coverr.",
            )

    options = JobOptions(
        aspect=aspect,
        voice=voice,
        paragraphs=paragraphs,
        language=language,
        subtitles=subtitles,
        video_source=video_source,
    )
    try:
        task_id = get_pipeline().submit(principal.owner, topic, options)
    except PipelineError as exc:
        raise HttpException(task_id="", status_code=400, message=str(exc)) from exc

    logger.info(f"mini app queued video owner={principal.kind} task_id={task_id}")
    return {
        "status": 200,
        "message": "success",
        "data": {"task_id": task_id, "state": 4, "progress": 0, "stage": "Writing the script"},
    }


@router.get("/miniapp/tasks/{task_id}", summary="Mini App task progress")
def miniapp_task(request: Request, task_id: str):
    principal = _principal(request)
    _require_owner(principal, task_id)
    try:
        snapshot = get_pipeline().snapshot(task_id, principal.owner)
    except PipelineError as exc:
        raise HttpException(task_id=task_id, status_code=404, message=str(exc)) from exc
    download_url = _signed_download(task_id) if snapshot.get("state") == 1 else None
    return {
        "status": 200,
        "message": "success",
        "data": {
            "task_id": task_id,
            "state": snapshot.get("state"),
            "progress": snapshot.get("progress"),
            "stage": snapshot.get("stage"),
            "error": snapshot.get("error"),
            "failed_stage": snapshot.get("failed_stage"),
            "download_url": download_url,
        },
    }


@router.get("/media/{task_id}", summary="Download a finished video with a signed token")
def download_media(
    task_id: str,
    access: str = Query("", description="Signed media token"),
):
    settings = _settings()
    secret = settings.media_secret()
    try:
        token_task = read_media_token(secret, access)
    except MediaTokenError as exc:
        raise HttpException(task_id=task_id, status_code=401, message=str(exc)) from exc
    if token_task != task_id:
        raise HttpException(task_id=task_id, status_code=401, message="Media token does not match this video.")
    path = task_video_path(task_id)
    if not path or not os.path.isfile(path):
        raise HttpException(task_id=task_id, status_code=404, message="That video is not ready.")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=os.path.basename(path),
    )
