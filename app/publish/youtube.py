"""YouTube Data API v3: OAuth refresh and resumable video upload."""

from __future__ import annotations

import os
import time

from app.publish.errors import AuthExpired, PublishError, QuotaExceeded, TransientPublishError
from app.publish.http import error_text, request

TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNEL_URL = "https://www.googleapis.com/youtube/v3/channels"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK_SIZE = 8 * 1024 * 1024
_RETRYABLE = {429, 500, 502, 503, 504}


def exchange_code(*, client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    response = request(
        "POST",
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    return _token_body(response)


def refresh_access_token(*, client_id: str, client_secret: str, refresh_token: str) -> dict:
    response = request(
        "POST",
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    body = _token_body(response)
    body.setdefault("refresh_token", refresh_token)
    return body


def channel_profile(access_token: str) -> dict:
    response = request(
        "GET",
        CHANNEL_URL,
        params={"part": "snippet", "mine": "true"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    _raise_for_status(response)
    body = _json(response)
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise PublishError("YouTube did not return a channel for this Google account.")
    item = items[0]
    snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
    channel_id = str(item.get("id") or "")
    title = str(snippet.get("title") or channel_id or "YouTube channel")
    if not channel_id:
        raise PublishError("YouTube did not return a channel id.")
    return {"external_id": channel_id, "display_name": title}


def ensure_access_token(account: dict, project) -> tuple[str, float]:
    """Return a usable access token, refreshing it when it is close to expiry."""

    expires_at = float(account.get("expires_at") or 0)
    access = str(account.get("access_token") or "")
    if access and expires_at > time.time() + 60:
        return access, expires_at
    refresh = str(account.get("refresh_token") or "")
    if not refresh:
        raise AuthExpired("This YouTube channel has no refresh token. Link it again.")
    body = refresh_access_token(
        client_id=project.client_id,
        client_secret=project.client_secret,
        refresh_token=refresh,
    )
    token = str(body.get("access_token") or "")
    if not token:
        raise AuthExpired("YouTube did not return an access token.")
    expires = time.time() + int(body.get("expires_in") or 3600)
    return token, expires


def upload_video(
    *,
    access_token: str,
    path: str,
    title: str,
    description: str,
    tags: list[str],
    privacy: str,
    session_url: str = "",
    offset: int = 0,
) -> dict:
    """Resumable upload. Returns the video id and the session URL."""

    size = os.path.getsize(path)
    if size <= 0:
        raise PublishError("The video file is empty.")
    location = session_url
    if not location:
        init = request(
            "POST",
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(size),
            },
            json={
                "snippet": {
                    "title": title[:100],
                    "description": description[:5000],
                    "tags": tags[:15],
                    "categoryId": "22",
                },
                "status": {
                    "privacyStatus": privacy,
                    "selfDeclaredMadeForKids": False,
                    "embeddable": True,
                },
            },
            timeout=60,
        )
        if init.status_code == 401:
            raise AuthExpired("YouTube rejected the access token.")
        _raise_for_status(init)
        location = init.headers.get("Location") or init.headers.get("location") or ""
        if not location:
            raise PublishError("YouTube did not return a resumable upload URL.")
        offset = 0

    with open(path, "rb") as handle:
        if offset:
            handle.seek(offset)
        while offset < size:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            end = offset + len(chunk) - 1
            put = request(
                "PUT",
                location,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                },
                data=chunk,
                timeout=300,
            )
            if put.status_code in {200, 201}:
                body = _json(put)
                video_id = str(body.get("id") or "") if isinstance(body, dict) else ""
                if not video_id:
                    raise PublishError("YouTube accepted the upload but did not return a video id.")
                return {"video_id": video_id, "session_url": location, "offset": size}
            if put.status_code == 308:
                offset = end + 1
                continue
            if put.status_code == 401:
                raise AuthExpired("YouTube rejected the access token during upload.")
            _raise_for_status(put)
            raise TransientPublishError(
                "YouTube upload stopped before the video id was returned. "
                f"Session remains at {location}."
            )
    raise TransientPublishError("YouTube upload ended before the file was fully sent.")


def watch_url(video_id: str, shorts: bool) -> str:
    if shorts:
        return f"https://www.youtube.com/shorts/{video_id}"
    return f"https://www.youtube.com/watch?v={video_id}"


def _json(response) -> dict:
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _token_body(response) -> dict:
    body = _json(response)
    if response.status_code == 401 or _reason(body) in {"invalid_grant", "invalid_client"}:
        raise AuthExpired("YouTube refused the refresh token. Link this channel again.")
    if response.status_code in _RETRYABLE:
        raise TransientPublishError(f"YouTube token request failed: {error_text(response)}")
    if response.status_code >= 400:
        raise PublishError(f"YouTube token request failed: {error_text(response)}")
    return body


def _raise_for_status(response) -> None:
    if response.status_code < 400:
        return
    reason = _reason(_json(response))
    if reason in {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded"}:
        raise QuotaExceeded(
            "YouTube rejected the upload because the Google Cloud project's daily quota is used up. "
            "videos.insert costs 1,600 units and the default project quota is 10,000 units, about 6 uploads. "
            "Request a higher quota, or link this channel with another Cloud project."
        )
    if response.status_code in _RETRYABLE:
        raise TransientPublishError(f"YouTube upload failed: {error_text(response)}")
    if response.status_code == 401:
        raise AuthExpired("YouTube rejected the access token.")
    raise PublishError(f"YouTube upload failed: {error_text(response)}")


def _reason(body: dict) -> str:
    error = body.get("error")
    if isinstance(error, dict):
        errors = error.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("reason") or "")
        return str(error.get("status") or error.get("message") or "")
    if isinstance(error, str):
        return error
    return ""
