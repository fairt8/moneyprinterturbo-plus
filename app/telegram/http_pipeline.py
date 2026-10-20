"""Bot-side client for the Mini App API running in another process."""

from __future__ import annotations

from urllib.parse import urlsplit

import requests

from app.telegram.logic import JobOptions, PipelineError
from app.telegram.settings import TelegramSettings
from app.utils.http_retry import request_with_retries


class HttpPipeline:
    def __init__(self, settings: TelegramSettings):
        self.settings = settings

    @classmethod
    def from_env(cls) -> "HttpPipeline":
        from app.telegram.settings import load_telegram_settings

        return cls(load_telegram_settings())

    def _headers(self, user_id: int | None = None, owner: str = "") -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        secret = self.settings.service_secret
        if secret:
            headers["X-Bot-Service-Secret"] = secret
        if user_id is None and owner.startswith("tg:"):
            user_id_text = owner.split(":", 1)[1]
            if user_id_text.isdigit():
                user_id = int(user_id_text)
        if user_id is not None:
            headers["X-Telegram-User-Id"] = str(user_id)
        if self.settings.api_key:
            headers["X-API-Key"] = self.settings.api_key
        return headers

    def submit(self, owner: str, topic: str, options: JobOptions) -> str:
        payload = {
            "video_subject": topic,
            "video_aspect": options.aspect,
            "voice_name": options.voice,
            "paragraph_number": options.paragraphs,
            "video_language": options.language,
            "subtitle_enabled": options.subtitles,
        }
        response = self._request(
            "POST",
            "/api/v1/miniapp/videos",
            owner=owner,
            json=payload,
        )
        data = response.get("data") or {}
        task_id = data.get("task_id")
        if not task_id:
            raise PipelineError("The video service did not return a task id.")
        return task_id

    def snapshot(self, task_id: str, owner: str) -> dict:
        response = self._request(
            "GET",
            f"/api/v1/miniapp/tasks/{task_id}",
            owner=owner,
        )
        data = response.get("data") or {}
        data.setdefault("files", [])
        download_url = data.get("download_url")
        if isinstance(download_url, str) and download_url:
            data["files"] = [self._internal_url(download_url)]
        return data

    def _internal_url(self, url: str) -> str:
        """Fetch media through the API container, even if the public host differs."""

        parsed = urlsplit(url)
        if parsed.path.startswith("/api/"):
            rewritten = self.settings.api_base_url + parsed.path
            if parsed.query:
                rewritten += "?" + parsed.query
            return rewritten
        if url.startswith("/"):
            return self.settings.api_base_url + url
        return url

    def _request(self, method: str, path: str, *, owner: str, json: dict | None = None) -> dict:
        url = self.settings.api_base_url + path

        def _call():
            return requests.request(
                method,
                url,
                headers=self._headers(owner=owner),
                json=json,
                timeout=60,
            )

        try:
            response = request_with_retries(_call, attempts=3, backoff=0.4)
        except requests.RequestException as exc:
            raise PipelineError(
                "Could not reach the video API. Check that it is running and "
                f"MPT_API_BASE_URL is correct. ({type(exc).__name__})"
            ) from exc

        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            message = body.get("message") if isinstance(body, dict) else ""
            raise PipelineError(message or f"The video API returned HTTP {response.status_code}.")
        if not isinstance(body, dict):
            raise PipelineError("The video API returned an unexpected response.")
        return body


def download_to(url: str, dest: str, headers: dict[str, str], *, limit: int) -> tuple[int, bool]:
    """Stream a remote video to disk. The bool is True when the file exceeds ``limit``."""

    with requests.get(url, headers=headers, stream=True, timeout=120) as response:
        response.raise_for_status()
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > limit:
            return int(length), True
        size = 0
        with open(dest, "wb") as handle:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > limit:
                    return size, True
                handle.write(chunk)
    return size, False
