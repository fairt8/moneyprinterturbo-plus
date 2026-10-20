"""In-process publisher used when the bot and the API share a process."""

from __future__ import annotations

from app.publish.service import (
    connect_url,
    delete_target,
    enqueue,
    preview_copy,
    rename_account,
    save_target,
    set_default_target,
    unlink_account,
)
from app.publish.store import get_store


class LocalPublisher:
    def __init__(self, secret: str):
        self.secret = secret

    def accounts(self, owner: str) -> list[dict]:
        return get_store().accounts_for(owner)

    def targets(self, owner: str) -> list[dict]:
        return get_store().targets_for(owner)

    def connect_url(self, owner: str, platform: str, label: str = "", project_id: str = "default", account_id: str = "") -> str:
        return connect_url(
            owner,
            platform,
            label=label,
            project_id=project_id,
            account_id=account_id,
            secret=self.secret,
        )

    def rename(self, owner: str, account_id: str, label: str) -> dict:
        return rename_account(owner, account_id, label)

    def unlink(self, owner: str, account_id: str) -> None:
        unlink_account(owner, account_id)

    def save_target(self, owner: str, name: str, account_ids: list[str], is_default: bool = False) -> dict:
        return save_target(owner, name, account_ids, is_default=is_default)

    def set_default_target(self, owner: str, target_id: str) -> dict:
        return set_default_target(owner, target_id)

    def delete_target(self, owner: str, target_id: str) -> None:
        delete_target(owner, target_id)

    def preview(self, owner: str, task_id: str, platform: str) -> dict:
        return preview_copy(owner, task_id, platform)

    def enqueue(self, owner: str, payload: dict) -> list[dict]:
        return enqueue(owner, payload)

    def job(self, owner: str, job_id: str) -> dict | None:
        return get_store().job_for(owner, job_id)
