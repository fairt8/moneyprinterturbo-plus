"""One-time OAuth for YouTube channels and TikTok accounts."""

from __future__ import annotations

import secrets
import time
from urllib.parse import urlencode

from app.publish import tiktok, youtube
from app.publish.errors import PublishRejected
from app.publish.settings import PublishSettings, load_publish_settings
from app.publish.store import PublishStore, iso, new_id

YOUTUBE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TIKTOK_AUTH = "https://www.tiktok.com/v2/auth/authorize/"
YOUTUBE_SCOPE = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube.readonly"
)
TIKTOK_SCOPE = "user.info.basic,video.publish,video.upload"


def begin(
    store: PublishStore,
    settings: PublishSettings,
    *,
    owner: str,
    platform: str,
    label: str = "",
    project_id: str = "default",
    account_id: str = "",
) -> str:
    platform = _platform(platform)
    if platform == "youtube":
        project = settings.youtube_project(project_id) or (
            settings.youtube_projects[0] if len(settings.youtube_projects) == 1 else None
        )
        if project is None or not (project.client_id and project.client_secret):
            raise PublishRejected(
                "YouTube is not configured. Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET."
            )
        if not settings.youtube_redirect_uri:
            raise PublishRejected("Set PUBLIC_BASE_URL or YOUTUBE_REDIRECT_URI before linking a channel.")
        project_id = project.id
    else:
        if not settings.tiktok_configured():
            raise PublishRejected("TikTok is not configured. Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET.")
        if not settings.tiktok_redirect_uri:
            raise PublishRejected("Set PUBLIC_BASE_URL or TIKTOK_REDIRECT_URI before linking an account.")
        project_id = ""
    if account_id:
        existing = store.account_secret(account_id)
        if existing is None or existing.get("owner") != owner or existing.get("platform") != platform:
            raise PublishRejected("That account is not on this profile.", status_code=404)
    state = secrets.token_urlsafe(24)
    store.save_oauth_state(
        state,
        {
            "owner": owner,
            "platform": platform,
            "label": (label or "").strip()[:80],
            "project_id": project_id,
            "account_id": account_id,
            "expires_at": time.time() + settings.connect_ttl_seconds,
        },
    )
    if platform == "youtube":
        project = settings.youtube_project(project_id)
        query = urlencode(
            {
                "client_id": project.client_id,
                "redirect_uri": settings.youtube_redirect_uri,
                "response_type": "code",
                "scope": YOUTUBE_SCOPE,
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
                "state": state,
            }
        )
        return f"{YOUTUBE_AUTH}?{query}"
    query = urlencode(
        {
            "client_key": settings.tiktok_client_key,
            "scope": TIKTOK_SCOPE,
            "response_type": "code",
            "redirect_uri": settings.tiktok_redirect_uri,
            "state": state,
        }
    )
    return f"{TIKTOK_AUTH}?{query}"


def finish(store: PublishStore, settings: PublishSettings, *, platform: str, code: str, state: str) -> dict:
    platform = _platform(platform)
    payload = store.pop_oauth_state(state)
    if not payload or payload.get("platform") != platform:
        raise PublishRejected("This authorization link was already used or has expired.", status_code=400)
    if float(payload.get("expires_at") or 0) < time.time():
        raise PublishRejected("This authorization link has expired. Start again.", status_code=400)
    if not code:
        raise PublishRejected("The provider did not return an authorization code.", status_code=400)
    owner = str(payload.get("owner") or "")
    label = str(payload.get("label") or "").strip()
    account_id = str(payload.get("account_id") or "")
    if platform == "youtube":
        project = settings.youtube_project(str(payload.get("project_id") or "default"))
        if project is None:
            raise PublishRejected("The YouTube project for this link is no longer configured.")
        token = youtube.exchange_code(
            client_id=project.client_id,
            client_secret=project.client_secret,
            code=code,
            redirect_uri=settings.youtube_redirect_uri,
        )
        profile = youtube.channel_profile(str(token.get("access_token") or ""))
        record = _record(
            store,
            owner=owner,
            platform="youtube",
            account_id=account_id,
            external_id=profile["external_id"],
            display_name=profile["display_name"],
            label=label,
            project_id=project.id,
            token=token,
        )
    else:
        token = tiktok.exchange_code(
            client_key=settings.tiktok_client_key,
            client_secret=settings.tiktok_client_secret,
            code=code,
            redirect_uri=settings.tiktok_redirect_uri,
        )
        profile = tiktok.user_profile(str(token.get("access_token") or ""))
        record = _record(
            store,
            owner=owner,
            platform="tiktok",
            account_id=account_id,
            external_id=profile["external_id"],
            display_name=profile["display_name"],
            label=label,
            project_id="",
            token=token,
        )
    return store.upsert_account(record)


def finish_current(*, platform: str, code: str, state: str) -> dict:
    from app.publish.store import get_store

    return finish(get_store(), load_publish_settings(), platform=platform, code=code, state=state)


def _record(
    store: PublishStore,
    *,
    owner: str,
    platform: str,
    account_id: str,
    external_id: str,
    display_name: str,
    label: str,
    project_id: str,
    token: dict,
) -> dict:
    existing = store.account_secret(account_id) if account_id else None
    if existing is None:
        for account in store.read_accounts()["accounts"].values():
            if (
                isinstance(account, dict)
                and account.get("owner") == owner
                and account.get("platform") == platform
                and account.get("external_id") == external_id
            ):
                existing = account
                break
    now = iso()
    expires_in = int(token.get("expires_in") or 3600)
    refresh_in = int(token.get("refresh_expires_in") or 0)
    record = dict(existing or {})
    record.update(
        {
            "id": (existing or {}).get("id") or new_id("y" if platform == "youtube" else "t"),
            "owner": owner,
            "platform": platform,
            "label": label or (existing or {}).get("label") or display_name,
            "external_id": external_id,
            "display_name": display_name,
            "project_id": project_id,
            "refresh_token": str(token.get("refresh_token") or (existing or {}).get("refresh_token") or ""),
            "access_token": str(token.get("access_token") or ""),
            "expires_at": time.time() + expires_in,
            "refresh_expires_at": time.time() + refresh_in if refresh_in else float((existing or {}).get("refresh_expires_at") or 0),
            "scope": str(token.get("scope") or ""),
            "needs_reauth": False,
            "linked_at": (existing or {}).get("linked_at") or now,
            "updated_at": now,
        }
    )
    if not record["refresh_token"]:
        raise PublishRejected(
            "The provider did not return a refresh token. Remove the app's access and link the account again, "
            "and for YouTube keep prompt=consent so Google issues a refresh token."
        )
    return record


def _platform(value: str) -> str:
    platform = (value or "").strip().lower()
    if platform in {"youtube", "yt"}:
        return "youtube"
    if platform in {"tiktok", "tt"}:
        return "tiktok"
    raise PublishRejected("Choose youtube or tiktok.")
