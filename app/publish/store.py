"""On-disk accounts, targets, OAuth states, jobs, and daily quota.

Refresh tokens live only in ``accounts.json``. That file is mode 0600.
Public views never include tokens or upload URLs.
"""

from __future__ import annotations

import copy
import os
import secrets
import threading
from datetime import datetime, timezone

from app.telegram.store import JsonStore

_SECRET_FIELDS = {"refresh_token", "access_token", "upload_url"}
_lock = threading.Lock()
_singleton = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None = None) -> str:
    return (moment or utc_now()).replace(microsecond=0).isoformat()


def day_key(moment: datetime | None = None) -> str:
    return (moment or utc_now()).date().isoformat()


def new_id(prefix: str, length: int = 4) -> str:
    return prefix + secrets.token_hex(length)


class PublishStore:
    def __init__(self, directory: str):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        self.accounts = JsonStore(os.path.join(directory, "accounts.json"))
        self.jobs = JsonStore(os.path.join(directory, "jobs.json"))
        self.quota = JsonStore(os.path.join(directory, "quota.json"))

    def _secure(self, store: JsonStore) -> None:
        if os.path.isfile(store.path):
            try:
                os.chmod(store.path, 0o600)
            except OSError:
                pass

    def update_accounts(self, mutator) -> dict:
        data = self.accounts.update(mutator)
        self._secure(self.accounts)
        return data

    def update_jobs(self, mutator) -> dict:
        data = self.jobs.update(mutator)
        self._secure(self.jobs)
        return data

    def update_quota(self, mutator) -> dict:
        data = self.quota.update(mutator)
        self._secure(self.quota)
        return data

    def read_accounts(self) -> dict:
        data = self.accounts.read()
        data.setdefault("accounts", {})
        data.setdefault("targets", {})
        data.setdefault("oauth_states", {})
        return data

    def public_account(self, record: dict) -> dict:
        return {
            "id": record.get("id"),
            "platform": record.get("platform"),
            "label": record.get("label") or record.get("display_name") or "Account",
            "external_id": record.get("external_id") or "",
            "display_name": record.get("display_name") or "",
            "project_id": record.get("project_id") or "",
            "needs_reauth": bool(record.get("needs_reauth")),
            "linked_at": record.get("linked_at") or "",
            "updated_at": record.get("updated_at") or "",
        }

    def public_target(self, record: dict) -> dict:
        return {
            "id": record.get("id"),
            "name": record.get("name") or "Target",
            "account_ids": list(record.get("account_ids") or []),
            "is_default": bool(record.get("is_default")),
        }

    def public_job(self, record: dict) -> dict:
        hidden = _SECRET_FIELDS | {"upload_offset"}
        return {key: copy.deepcopy(value) for key, value in record.items() if key not in hidden}

    def accounts_for(self, owner: str) -> list[dict]:
        records = [
            self.public_account(item)
            for item in self.read_accounts()["accounts"].values()
            if isinstance(item, dict) and item.get("owner") == owner
        ]
        records.sort(key=lambda item: (item["platform"], item["label"].lower()))
        return records

    def account_secret(self, account_id: str) -> dict | None:
        record = self.read_accounts()["accounts"].get(account_id)
        if isinstance(record, dict):
            return copy.deepcopy(record)
        return None

    def targets_for(self, owner: str) -> list[dict]:
        records = [
            self.public_target(item)
            for item in self.read_accounts()["targets"].values()
            if isinstance(item, dict) and item.get("owner") == owner
        ]
        records.sort(key=lambda item: (not item["is_default"], item["name"].lower()))
        return records

    def save_oauth_state(self, state: str, payload: dict) -> None:
        def mutate(data: dict) -> None:
            states = data.setdefault("oauth_states", {})
            now = utc_now().timestamp()
            expired = [
                key
                for key, value in states.items()
                if not isinstance(value, dict) or float(value.get("expires_at") or 0) < now
            ]
            for key in expired:
                states.pop(key, None)
            states[state] = payload

        self.update_accounts(mutate)

    def pop_oauth_state(self, state: str) -> dict | None:
        found: dict = {}

        def mutate(data: dict) -> None:
            states = data.setdefault("oauth_states", {})
            payload = states.pop(state, None)
            if isinstance(payload, dict):
                found["payload"] = payload

        self.update_accounts(mutate)
        return found.get("payload")

    def upsert_account(self, record: dict) -> dict:
        def mutate(data: dict) -> None:
            accounts = data.setdefault("accounts", {})
            accounts[record["id"]] = record

        self.update_accounts(mutate)
        return self.public_account(record)

    def rename_account(self, owner: str, account_id: str, label: str) -> dict | None:
        updated: dict = {}

        def mutate(data: dict) -> None:
            record = data.setdefault("accounts", {}).get(account_id)
            if not isinstance(record, dict) or record.get("owner") != owner:
                return
            record["label"] = label
            record["updated_at"] = iso()
            updated["record"] = self.public_account(record)

        self.update_accounts(mutate)
        return updated.get("record")

    def mark_reauth(self, account_id: str, needed: bool) -> None:
        def mutate(data: dict) -> None:
            record = data.setdefault("accounts", {}).get(account_id)
            if isinstance(record, dict):
                record["needs_reauth"] = needed
                record["updated_at"] = iso()

        self.update_accounts(mutate)

    def save_tokens(self, account_id: str, *, access_token: str, expires_at: float, refresh_token: str = "", refresh_expires_at: float = 0) -> None:
        def mutate(data: dict) -> None:
            record = data.setdefault("accounts", {}).get(account_id)
            if not isinstance(record, dict):
                return
            record["access_token"] = access_token
            record["expires_at"] = expires_at
            if refresh_token:
                record["refresh_token"] = refresh_token
            if refresh_expires_at:
                record["refresh_expires_at"] = refresh_expires_at
            record["needs_reauth"] = False
            record["updated_at"] = iso()

        self.update_accounts(mutate)

    def delete_account(self, owner: str, account_id: str) -> bool:
        removed = {"ok": False}

        def mutate(data: dict) -> None:
            accounts = data.setdefault("accounts", {})
            record = accounts.get(account_id)
            if not isinstance(record, dict) or record.get("owner") != owner:
                return
            accounts.pop(account_id, None)
            for target in data.setdefault("targets", {}).values():
                if isinstance(target, dict) and target.get("owner") == owner:
                    target["account_ids"] = [
                        item for item in target.get("account_ids") or [] if item != account_id
                    ]
            removed["ok"] = True

        self.update_accounts(mutate)
        return removed["ok"]

    def save_target(self, record: dict) -> dict:
        def mutate(data: dict) -> None:
            targets = data.setdefault("targets", {})
            if record.get("is_default"):
                for other in targets.values():
                    if isinstance(other, dict) and other.get("owner") == record.get("owner"):
                        other["is_default"] = False
            targets[record["id"]] = record

        self.update_accounts(mutate)
        return self.public_target(record)

    def delete_target(self, owner: str, target_id: str) -> bool:
        removed = {"ok": False}

        def mutate(data: dict) -> None:
            targets = data.setdefault("targets", {})
            record = targets.get(target_id)
            if isinstance(record, dict) and record.get("owner") == owner:
                targets.pop(target_id, None)
                removed["ok"] = True

        self.update_accounts(mutate)
        return removed["ok"]

    def set_default_target(self, owner: str, target_id: str) -> dict | None:
        updated: dict = {}

        def mutate(data: dict) -> None:
            targets = data.setdefault("targets", {})
            chosen = targets.get(target_id)
            if not isinstance(chosen, dict) or chosen.get("owner") != owner:
                return
            for other in targets.values():
                if isinstance(other, dict) and other.get("owner") == owner:
                    other["is_default"] = other.get("id") == target_id
            updated["record"] = self.public_target(chosen)

        self.update_accounts(mutate)
        return updated.get("record")

    def add_jobs(self, jobs: list[dict]) -> list[dict]:
        def mutate(data: dict) -> None:
            stored = data.setdefault("jobs", {})
            for job in jobs:
                stored[job["id"]] = job

        self.update_jobs(mutate)
        return [self.public_job(job) for job in jobs]

    def job_for(self, owner: str, job_id: str) -> dict | None:
        record = self.jobs.read().get("jobs", {}).get(job_id)
        if isinstance(record, dict) and record.get("owner") == owner:
            return self.public_job(record)
        return None

    def job_secret(self, job_id: str) -> dict | None:
        record = self.jobs.read().get("jobs", {}).get(job_id)
        if isinstance(record, dict):
            return copy.deepcopy(record)
        return None

    def jobs_for_task(self, owner: str, task_id: str) -> list[dict]:
        records = [
            self.public_job(item)
            for item in self.jobs.read().get("jobs", {}).values()
            if isinstance(item, dict) and item.get("owner") == owner and item.get("task_id") == task_id
        ]
        records.sort(key=lambda item: item.get("created_at") or "")
        return records

    def replace_job(self, job: dict) -> None:
        def mutate(data: dict) -> None:
            data.setdefault("jobs", {})[job["id"]] = job

        self.update_jobs(mutate)

    def claim_next(self, now: float, max_concurrent: int) -> dict | None:
        claimed: dict = {}

        def mutate(data: dict) -> None:
            jobs = data.setdefault("jobs", {})
            active = sum(
                1
                for item in jobs.values()
                if isinstance(item, dict) and item.get("state") in {"uploading", "processing"}
            )
            if active >= max_concurrent:
                return
            ready = [
                item
                for item in jobs.values()
                if isinstance(item, dict)
                and item.get("state") == "queued"
                and float(item.get("not_before") or 0) <= now
            ]
            if not ready:
                return
            ready.sort(key=lambda item: (float(item.get("not_before") or 0), item.get("created_at") or ""))
            job = ready[0]
            job["state"] = "uploading"
            job["attempts"] = int(job.get("attempts") or 0) + 1
            job["updated_at"] = iso()
            claimed["job"] = copy.deepcopy(job)

        self.update_jobs(mutate)
        return claimed.get("job")

    def reserve_quota(self, kind: str, key: str, limit: int, moment: datetime | None = None) -> bool:
        """Count one post if the UTC-day cap is not already reached."""

        allowed = {"ok": False}

        def mutate(data: dict) -> None:
            bucket = data.setdefault("days", {}).setdefault(day_key(moment), {}).setdefault(kind, {})
            used = int(bucket.get(key) or 0)
            if used >= limit:
                return
            bucket[key] = used + 1
            allowed["ok"] = True

        self.update_quota(mutate)
        return allowed["ok"]

    def release_quota(self, kind: str, key: str, moment: datetime | None = None) -> None:
        def mutate(data: dict) -> None:
            bucket = data.setdefault("days", {}).setdefault(day_key(moment), {}).setdefault(kind, {})
            used = int(bucket.get(key) or 0)
            if used > 0:
                bucket[key] = used - 1

        self.update_quota(mutate)

    def quota_snapshot(self, owner: str, settings) -> dict:
        day = self.quota.read().get("days", {}).get(day_key(), {})
        youtube_used = day.get("youtube") if isinstance(day.get("youtube"), dict) else {}
        tiktok_used = day.get("tiktok") if isinstance(day.get("tiktok"), dict) else {}
        youtube = [
            {
                "project_id": project.id,
                "used": int(youtube_used.get(project.id) or 0),
                "limit": project.daily_upload_quota,
            }
            for project in settings.youtube_projects
        ]
        tiktok = []
        for account in self.accounts_for(owner):
            if account["platform"] != "tiktok":
                continue
            tiktok.append(
                {
                    "account_id": account["id"],
                    "label": account["label"],
                    "used": int(tiktok_used.get(account["id"]) or 0),
                    "limit": settings.tiktok_daily_posts,
                }
            )
        return {"youtube": youtube, "tiktok": tiktok}


def get_store() -> PublishStore:
    global _singleton
    with _lock:
        if _singleton is None:
            from app.utils import utils

            _singleton = PublishStore(utils.storage_dir("publish", create=True))
        return _singleton


def reset_store() -> None:
    global _singleton
    with _lock:
        _singleton = None
