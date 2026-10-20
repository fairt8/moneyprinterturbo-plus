"""TikTok Login Kit and Content Posting API (direct post and inbox draft)."""

from __future__ import annotations

import os

from app.publish.errors import AuthExpired, PublishError, TransientPublishError
from app.publish.http import error_text, request

TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
USER_URL = "https://open.tiktokapis.com/v2/user/info/"
DIRECT_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
INBOX_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
_RETRYABLE = {429, 500, 502, 503, 504}
_MIN_CHUNK = 5 * 1024 * 1024
_MAX_SINGLE = 64 * 1024 * 1024


def exchange_code(*, client_key: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    response = request(
        "POST",
        TOKEN_URL,
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    return _token_body(response)


def refresh_access_token(*, client_key: str, client_secret: str, refresh_token: str) -> dict:
    response = request(
        "POST",
        TOKEN_URL,
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    body = _token_body(response)
    body.setdefault("refresh_token", refresh_token)
    return body


def user_profile(access_token: str) -> dict:
    response = request(
        "GET",
        USER_URL,
        params={"fields": "open_id,display_name"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    _raise_for_status(response)
    body = _json(response)
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    user = data.get("user") if isinstance(data.get("user"), dict) else data
    open_id = str(user.get("open_id") or "")
    name = str(user.get("display_name") or open_id or "TikTok account")
    if not open_id:
        raise PublishError("TikTok did not return an open id for this account.")
    return {"external_id": open_id, "display_name": name}


def ensure_access_token(account: dict, settings) -> tuple[str, float, str, float]:
    import time

    expires_at = float(account.get("expires_at") or 0)
    access = str(account.get("access_token") or "")
    refresh = str(account.get("refresh_token") or "")
    refresh_expires = float(account.get("refresh_expires_at") or 0)
    if access and expires_at > time.time() + 60:
        return access, expires_at, refresh, refresh_expires
    if not refresh:
        raise AuthExpired("This TikTok account has no refresh token. Link it again.")
    body = refresh_access_token(
        client_key=settings.tiktok_client_key,
        client_secret=settings.tiktok_client_secret,
        refresh_token=refresh,
    )
    token = str(body.get("access_token") or "")
    if not token:
        raise AuthExpired("TikTok did not return an access token.")
    new_refresh = str(body.get("refresh_token") or refresh)
    expires = time.time() + int(body.get("expires_in") or 86400)
    refresh_in = int(body.get("refresh_expires_in") or 0)
    refresh_expires = time.time() + refresh_in if refresh_in else refresh_expires
    return token, expires, new_refresh, refresh_expires


def init_upload(*, access_token: str, mode: str, caption: str, privacy_level: str, path: str) -> dict:
    size = os.path.getsize(path)
    ranges = chunk_ranges(size)
    if not ranges:
        raise PublishError("The video file is empty.")
    chunk_size = ranges[0][1] - ranges[0][0] + 1
    source = {
        "source": "FILE_UPLOAD",
        "video_size": size,
        "chunk_size": chunk_size,
        "total_chunk_count": len(ranges),
    }
    if mode == "inbox":
        url = INBOX_INIT_URL
        payload = {"source_info": source}
    else:
        url = DIRECT_INIT_URL
        payload = {
            "post_info": {
                "title": caption[:2200],
                "privacy_level": privacy_level,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
                "video_cover_timestamp_ms": 1000,
            },
            "source_info": source,
        }
    response = request(
        "POST",
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json=payload,
        timeout=60,
    )
    _raise_for_status(response)
    data = _data(response)
    publish_id = str(data.get("publish_id") or "")
    upload_url = str(data.get("upload_url") or "")
    if not publish_id or not upload_url:
        raise PublishError("TikTok did not return an upload URL.")
    return {"publish_id": publish_id, "upload_url": upload_url, "ranges": ranges}


def upload_chunks(*, upload_url: str, path: str, ranges: list[tuple[int, int]], start_index: int = 0) -> None:
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        for index, (start, end) in enumerate(ranges):
            if index < start_index:
                continue
            handle.seek(start)
            chunk = handle.read(end - start + 1)
            response = request(
                "PUT",
                upload_url,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                },
                data=chunk,
                timeout=300,
            )
            if response.status_code in {201, 206, 200}:
                continue
            if response.status_code in _RETRYABLE:
                raise TransientPublishError(
                    f"TikTok upload stopped at chunk {index + 1}: {error_text(response)}"
                )
            _raise_for_status(response)


def fetch_status(access_token: str, publish_id: str) -> dict:
    response = request(
        "POST",
        STATUS_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json={"publish_id": publish_id},
        timeout=30,
    )
    _raise_for_status(response)
    data = _data(response)
    status = str(data.get("status") or "")
    post_ids = data.get("publicaly_available_post_id") or data.get("publicly_available_post_id") or []
    post_id = ""
    if isinstance(post_ids, list) and post_ids:
        post_id = str(post_ids[0])
    return {
        "status": status,
        "post_id": post_id,
        "fail_reason": str(data.get("fail_reason") or ""),
    }


def poll_status(access_token: str, publish_id: str, *, attempts: int = 8, sleep=None) -> dict:
    import time

    pause = sleep or time.sleep
    last = {"status": "", "post_id": "", "fail_reason": ""}
    for attempt in range(attempts):
        last = fetch_status(access_token, publish_id)
        if last["status"] in {"PUBLISH_COMPLETE", "SEND_TO_USER_INBOX", "FAILED"}:
            return last
        if attempt < attempts - 1:
            pause(2)
    return last


def chunk_ranges(size: int) -> list[tuple[int, int]]:
    if size <= 0:
        return []
    if size <= _MAX_SINGLE:
        return [(0, size - 1)]
    chunk = 10 * 1024 * 1024
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < size:
        end = min(start + chunk, size) - 1
        ranges.append((start, end))
        start = end + 1
    if len(ranges) >= 2:
        last_len = ranges[-1][1] - ranges[-1][0] + 1
        if last_len < _MIN_CHUNK:
            previous = ranges[-2]
            ranges[-2] = (previous[0], ranges[-1][1])
            ranges.pop()
    return ranges


def privacy_level(privacy: str, *, audited: bool, mode: str) -> str:
    """Map the studio privacy choice onto TikTok's levels.

    Unaudited apps may only direct-post as SELF_ONLY. TikTok has no unlisted
    state, so unlisted is sent as a private post.
    """

    if mode == "inbox":
        return "SELF_ONLY"
    if not audited:
        if privacy == "public":
            raise PublishError(
                "This TikTok app is not audited, so a direct post cannot be public. "
                "TikTok only allows SELF_ONLY until the app is approved. "
                "Use a private post, send it to the TikTok inbox instead, or apply for "
                "the Content Posting API audit and then set TIKTOK_AUDITED=true."
            )
        return "SELF_ONLY"
    if privacy == "public":
        return "PUBLIC_TO_EVERYONE"
    return "SELF_ONLY"


def _json(response) -> dict:
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _data(response) -> dict:
    body = _json(response)
    data = body.get("data")
    return data if isinstance(data, dict) else {}


def _token_body(response) -> dict:
    body = _json(response)
    error = body.get("error")
    code = ""
    if isinstance(error, dict):
        code = str(error.get("code") or "")
    description = str(body.get("error_description") or "")
    if response.status_code == 401 or code in {"invalid_grant", "access_token_invalid"} or "invalid_grant" in description:
        raise AuthExpired("TikTok refused the token. Link this account again.")
    if response.status_code in _RETRYABLE:
        raise TransientPublishError(f"TikTok token request failed: {error_text(response)}")
    if response.status_code >= 400 or (code and code not in {"ok", "0"}):
        raise PublishError(f"TikTok token request failed: {error_text(response) or description or code}")
    if not body.get("access_token"):
        raise PublishError("TikTok did not return an access token.")
    return body


def _raise_for_status(response) -> None:
    body = _json(response)
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = str(error.get("code") or "")
    if response.status_code in _RETRYABLE or code in {"rate_limit_exceeded", "internal_error"}:
        raise TransientPublishError(f"TikTok request failed: {error_text(response)}")
    if response.status_code == 401 or code in {"access_token_invalid", "scope_not_authorized"}:
        raise AuthExpired("TikTok rejected the access token. Link this account again.")
    if code in {"unaudited_client_can_only_post_to_private_accounts", "privacy_level_option_mismatch"}:
        raise PublishError(
            "TikTok refused a public post because this app is not audited. "
            "Direct posts must use SELF_ONLY until TikTok approves video.publish. "
            "Apply in the developer portal, or post to the inbox."
        )
    if response.status_code >= 400 or (code and code not in {"ok", "0"}):
        raise PublishError(f"TikTok request failed: {error_text(response)}")

