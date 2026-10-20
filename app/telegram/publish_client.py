"""Bot-process client for the publish API."""

from __future__ import annotations

import requests

from app.publish.errors import PublishRejected
from app.telegram.http_pipeline import HttpPipeline
from app.telegram.settings import TelegramSettings
from app.utils.http_retry import request_with_retries


class HttpPublisher:
    def __init__(self, settings: TelegramSettings):
        self.settings = settings
        self._http = HttpPipeline(settings)

    def accounts(self, owner: str) -> list[dict]:
        data = self._get("/api/v1/publish/accounts", owner)
        return list(data.get("accounts") or [])

    def targets(self, owner: str) -> list[dict]:
        data = self._get("/api/v1/publish/accounts", owner)
        return list(data.get("targets") or [])

    def connect_url(self, owner: str, platform: str, label: str = "", project_id: str = "default", account_id: str = "") -> str:
        data = self._post(
            "/api/v1/publish/connect",
            owner,
            {"platform": platform, "label": label, "project_id": project_id, "account_id": account_id},
        )
        return str(data.get("url") or "")

    def rename(self, owner: str, account_id: str, label: str) -> dict:
        return self._post(f"/api/v1/publish/accounts/{account_id}", owner, {"label": label})

    def unlink(self, owner: str, account_id: str) -> None:
        self._request("DELETE", f"/api/v1/publish/accounts/{account_id}", owner)

    def save_target(self, owner: str, name: str, account_ids: list[str], is_default: bool = False) -> dict:
        return self._post(
            "/api/v1/publish/targets",
            owner,
            {"name": name, "account_ids": account_ids, "is_default": is_default},
        )

    def set_default_target(self, owner: str, target_id: str) -> dict:
        return self._post(f"/api/v1/publish/targets/{target_id}/default", owner, {})

    def preview(self, owner: str, task_id: str, platform: str) -> dict:
        return self._post("/api/v1/publish/metadata", owner, {"task_id": task_id, "platform": platform})

    def enqueue(self, owner: str, payload: dict) -> list[dict]:
        data = self._post("/api/v1/publish", owner, payload)
        return list(data.get("jobs") or [])

    def job(self, owner: str, job_id: str) -> dict | None:
        try:
            return self._get(f"/api/v1/publish/jobs/{job_id}", owner)
        except PublishRejected as exc:
            if exc.status_code == 404:
                return None
            raise

    def _get(self, path: str, owner: str) -> dict:
        body = self._request("GET", path, owner)
        return body.get("data") or {}

    def _post(self, path: str, owner: str, payload: dict) -> dict:
        body = self._request("POST", path, owner, payload)
        return body.get("data") or {}

    def _request(self, method: str, path: str, owner: str, payload: dict | None = None) -> dict:
        url = self.settings.api_base_url + path

        def _call():
            return requests.request(
                method,
                url,
                headers=self._http._headers(owner=owner),
                json=payload,
                timeout=60,
            )

        try:
            response = request_with_retries(_call, attempts=3, backoff=0.4)
        except requests.RequestException as exc:
            raise PublishRejected(
                "Could not reach the publish API. Check that it is running.",
                status_code=503,
            ) from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            message = body.get("message") if isinstance(body, dict) else ""
            raise PublishRejected(message or f"Publish API returned HTTP {response.status_code}.", status_code=response.status_code)
        return body if isinstance(body, dict) else {}
