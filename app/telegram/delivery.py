"""Decide how a finished video gets back to Telegram.

Bots can upload about 50 MB. Larger files are handed out as a signed download
link instead of a failed upload.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class UploadDelivery:
    path: str
    size: int
    as_document: bool = False


@dataclass(frozen=True)
class LinkDelivery:
    url: str
    size: int
    reason: str


@dataclass(frozen=True)
class UnavailableDelivery:
    reason: str


def plan_local_delivery(
    path: str,
    *,
    upload_limit: int,
    download_url: str | None,
) -> UploadDelivery | LinkDelivery | UnavailableDelivery:
    if not path or not os.path.isfile(path):
        return UnavailableDelivery("The video finished, but the file is no longer on disk.")
    size = os.path.getsize(path)
    if size <= upload_limit:
        return UploadDelivery(path=path, size=size)
    if download_url:
        return LinkDelivery(
            url=download_url,
            size=size,
            reason=f"The video is {size} bytes, which is over Telegram's upload limit.",
        )
    return UnavailableDelivery(
        f"The video is {size} bytes, over Telegram's upload limit, and no public "
        "download URL is configured. Set PUBLIC_BASE_URL and a bot token or "
        "MEDIA_TOKEN_SECRET."
    )


def plan_remote_delivery(
    *,
    content_length: int | None,
    upload_limit: int,
    download_url: str | None,
) -> str:
    """Return 'upload' or 'link' before downloading a remote file."""

    if content_length is None:
        return "upload"
    if content_length <= upload_limit:
        return "upload"
    if download_url:
        return "link"
    return "too_large"
