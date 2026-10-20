"""Publish API used by the Mini App, the browser, and the Telegram bot."""

from __future__ import annotations

from fastapi import Body, Request

from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.publish.errors import PublishRejected
from app.publish.service import (
    delete_target,
    enqueue,
    preview_copy,
    rename_account,
    save_target,
    set_default_target,
    setup_view,
    unlink_account,
)
from app.publish.settings import load_publish_settings
from app.publish.store import get_store
from app.telegram.settings import load_telegram_settings
from app.telegram.web_auth import AuthError, authenticate

router = new_router()


def _principal(request: Request):
    try:
        return authenticate(request.headers, load_telegram_settings())
    except AuthError as exc:
        raise HttpException(task_id="", status_code=exc.status_code, message=exc.message) from exc


def _data(payload):
    return {"status": 200, "message": "success", "data": payload}


def _rejected(exc: PublishRejected) -> HttpException:
    return HttpException(task_id="", status_code=exc.status_code, message=exc.message)


@router.get("/publish/setup", summary="Publishing configuration, accounts, targets, and quota")
def publish_setup(request: Request):
    principal = _principal(request)
    return _data(setup_view(principal.owner))


@router.get("/publish/accounts", summary="Linked YouTube channels and TikTok accounts")
def list_accounts(request: Request):
    principal = _principal(request)
    store = get_store()
    return _data({"accounts": store.accounts_for(principal.owner), "targets": store.targets_for(principal.owner)})


@router.post("/publish/accounts/{account_id}", summary="Rename a linked account")
def rename(request: Request, account_id: str, body: dict = Body(...)):
    principal = _principal(request)
    try:
        account = rename_account(principal.owner, account_id, str(body.get("label") or ""))
    except PublishRejected as exc:
        raise _rejected(exc) from exc
    return _data(account)


@router.delete("/publish/accounts/{account_id}", summary="Unlink an account")
def unlink(request: Request, account_id: str):
    principal = _principal(request)
    try:
        unlink_account(principal.owner, account_id)
    except PublishRejected as exc:
        raise _rejected(exc) from exc
    return _data({"removed": account_id})


@router.post("/publish/connect", summary="Create a one-time URL that starts OAuth")
def connect(request: Request, body: dict = Body(...)):
    principal = _principal(request)
    from app.publish.service import connect_url

    try:
        url = connect_url(
            principal.owner,
            str(body.get("platform") or ""),
            label=str(body.get("label") or ""),
            project_id=str(body.get("project_id") or "default"),
            account_id=str(body.get("account_id") or ""),
            secret=load_telegram_settings().media_secret(),
        )
    except PublishRejected as exc:
        raise _rejected(exc) from exc
    return _data({"url": url})


@router.get("/publish/targets", summary="Named groups of accounts")
def list_targets(request: Request):
    principal = _principal(request)
    return _data({"targets": get_store().targets_for(principal.owner)})


@router.post("/publish/targets", summary="Create or replace a publish target")
def upsert_target(request: Request, body: dict = Body(...)):
    principal = _principal(request)
    try:
        target = save_target(
            principal.owner,
            str(body.get("name") or ""),
            list(body.get("account_ids") or []),
            target_id=str(body.get("target_id") or ""),
            is_default=bool(body.get("is_default")),
        )
    except PublishRejected as exc:
        raise _rejected(exc) from exc
    return _data(target)


@router.post("/publish/targets/{target_id}/default", summary="Use this target for auto-publish")
def default_target(request: Request, target_id: str):
    principal = _principal(request)
    try:
        target = set_default_target(principal.owner, target_id)
    except PublishRejected as exc:
        raise _rejected(exc) from exc
    return _data(target)


@router.delete("/publish/targets/{target_id}", summary="Delete a publish target")
def remove_target(request: Request, target_id: str):
    principal = _principal(request)
    try:
        delete_target(principal.owner, target_id)
    except PublishRejected as exc:
        raise _rejected(exc) from exc
    return _data({"removed": target_id})


@router.post("/publish/metadata", summary="Draft a title, description, and hashtags")
def metadata(request: Request, body: dict = Body(...)):
    principal = _principal(request)
    try:
        draft = preview_copy(
            principal.owner,
            str(body.get("task_id") or ""),
            str(body.get("platform") or "youtube"),
        )
    except PublishRejected as exc:
        raise _rejected(exc) from exc
    return _data(draft)


@router.post("/publish", summary="Queue one publish job per selected account")
def create_jobs(request: Request, body: dict = Body(...)):
    principal = _principal(request)
    try:
        jobs = enqueue(principal.owner, body, settings=load_publish_settings())
    except PublishRejected as exc:
        raise _rejected(exc) from exc
    return _data({"jobs": jobs})


@router.get("/publish/jobs/{job_id}", summary="Status of one account's publish job")
def job_status(request: Request, job_id: str):
    principal = _principal(request)
    job = get_store().job_for(principal.owner, job_id)
    if job is None:
        raise HttpException(task_id=job_id, status_code=404, message="No publish job with that id.")
    return _data(job)


@router.get("/publish/tasks/{task_id}", summary="Publish jobs for one video")
def task_jobs(request: Request, task_id: str):
    principal = _principal(request)
    return _data({"jobs": get_store().jobs_for_task(principal.owner, task_id)})
