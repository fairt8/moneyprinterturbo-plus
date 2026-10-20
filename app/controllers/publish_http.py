"""Browser pages for OAuth and account management."""

from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from app.publish.errors import PublishError, PublishRejected
from app.publish.oauth import finish_current
from app.publish.service import provider_authorize_url
from app.publish.tokens import read_connect_token
from app.telegram.settings import load_telegram_settings
from app.telegram.web_auth import AuthError, authenticate
from app.utils import utils

router = APIRouter()


def _page(name: str) -> str:
    return os.path.join(utils.public_dir("miniapp"), name)


def _owner(request: Request, connect: str) -> str:
    settings = load_telegram_settings()
    if connect:
        return read_connect_token(settings.media_secret(), connect)
    try:
        return authenticate(request.headers, settings).owner
    except AuthError as exc:
        if settings.auth_required:
            raise PublishRejected(exc.message, status_code=exc.status_code) from exc
        return "anon"


def _html(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    document = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{ margin: 0; font-family: Georgia, serif; background: #f6f1e8; color: #1c1915; }}
main {{ width: min(640px, calc(100% - 32px)); margin: 48px auto; }}
a {{ color: #8d4318; }}
</style></head><body><main><h1>{title}</h1>{body}<p><a href="/accounts">Back to accounts</a></p></main></body></html>"""
    return HTMLResponse(document, status_code=status_code)


def _start(request: Request, platform: str, connect: str, label: str, project_id: str, account_id: str):
    try:
        owner = _owner(request, connect)
        url = provider_authorize_url(
            owner,
            platform,
            label=label,
            project_id=project_id or "default",
            account_id=account_id,
        )
    except PublishRejected as exc:
        return _html("Could not start linking", f"<p>{exc.message}</p>", exc.status_code)
    return RedirectResponse(url, status_code=302)


def _callback(platform: str, code: str, state: str, error: str):
    if error:
        return _html("Authorization was cancelled", f"<p>{error}</p>", 400)
    try:
        account = finish_current(platform=platform, code=code, state=state)
    except (PublishRejected, PublishError) as exc:
        message = getattr(exc, "message", str(exc))
        status = getattr(exc, "status_code", 400)
        return _html("Could not link that account", f"<p>{message}</p>", status)
    label = account.get("label") or account.get("display_name") or "account"
    platform_name = "YouTube" if account.get("platform") == "youtube" else "TikTok"
    return _html(
        f"{platform_name} account linked",
        f"<p><strong>{label}</strong> is saved on this server. Tokens stay on disk and are not shown here.</p>",
    )


@router.get("/accounts", include_in_schema=False)
@router.get("/accounts/", include_in_schema=False)
def accounts_page():
    path = _page("accounts.html")
    if not os.path.isfile(path):
        return JSONResponse(status_code=500, content={"status": 500, "message": "Accounts page is not installed."})
    return FileResponse(path, media_type="text/html")


@router.get("/auth/youtube/start", include_in_schema=False)
def youtube_start(
    request: Request,
    connect: str = "",
    label: str = "",
    project_id: str = "default",
    account_id: str = "",
):
    return _start(request, "youtube", connect, label, project_id, account_id)


@router.get("/auth/youtube/callback", include_in_schema=False)
def youtube_callback(code: str = "", state: str = "", error: str = ""):
    return _callback("youtube", code, state, error)


@router.get("/auth/tiktok/start", include_in_schema=False)
def tiktok_start(
    request: Request,
    connect: str = "",
    label: str = "",
    project_id: str = "default",
    account_id: str = "",
):
    return _start(request, "tiktok", connect, label, project_id, account_id)


@router.get("/auth/tiktok/callback", include_in_schema=False)
def tiktok_callback(code: str = "", state: str = "", error: str = ""):
    return _callback("tiktok", code, state, error)
