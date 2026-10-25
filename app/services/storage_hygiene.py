"""Keep rendered videos, stock clips, temp files, and publish logs bounded.

OAuth account tokens in ``publish/accounts.json`` are never deleted. A hard
storage cap removes the oldest eligible files first. Age-based task cleanup
still skips a render that is in progress.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime

from loguru import logger

from app.services.task_retention import cleanup_expired_tasks
from app.telegram.store import JsonStore


_CACHE_DIRS = ("cache_videos", "cache_images", "cache_material_search")
_KEPT_TOP_LEVEL = {"bgm", "local_videos", "telegram", "publish"}
_GRACE_SECONDS = 600


@dataclass(frozen=True)
class StoragePolicy:
    retention_hours: float = 24
    cache_max_bytes: int = 512 * 1024 * 1024
    cap_bytes: int = 2048 * 1024 * 1024
    delete_after_delivery: bool = False
    temp_max_age_hours: float = 6


def _number(value, default: float) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _flag(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def policy_from_mapping(app: dict | None) -> StoragePolicy:
    app = app or {}
    retention = _number(app.get("task_retention_hours", 24), 24)
    cache_mb = _number(app.get("material_cache_max_mb", 512), 512)
    cap_mb = _number(app.get("storage_cap_mb", 2048), 2048)
    return StoragePolicy(
        retention_hours=retention,
        cache_max_bytes=int(cache_mb * 1024 * 1024) if cache_mb > 0 else 0,
        cap_bytes=int(cap_mb * 1024 * 1024) if cap_mb > 0 else 0,
        delete_after_delivery=_flag(app.get("delete_after_delivery"), False),
        temp_max_age_hours=_number(app.get("temp_max_age_hours", 6), 6),
    )


def policy_from_config() -> StoragePolicy:
    from app.config import config

    return policy_from_mapping(config.app)


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f}"


def _walk_files(directory: str):
    if not directory or not os.path.isdir(directory):
        return
    root = os.path.realpath(directory)
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(current, name))]
        for name in filenames:
            path = os.path.join(current, name)
            if os.path.islink(path):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if not os.path.isfile(path):
                continue
            yield path, stat.st_size, stat.st_mtime


def _dir_usage(directory: str) -> tuple[int, int]:
    total = 0
    count = 0
    for _path, size, _mtime in _walk_files(directory):
        total += size
        count += 1
    return total, count


def storage_report(root: str, policy: StoragePolicy | None = None) -> dict:
    policy = policy or StoragePolicy()
    categories = {}
    total = 0
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if os.path.islink(path):
                continue
            if os.path.isdir(path):
                size, count = _dir_usage(path)
            elif os.path.isfile(path):
                try:
                    size, count = os.path.getsize(path), 1
                except OSError:
                    continue
            else:
                continue
            categories[name] = {"bytes": size, "files": count}
            total += size
    return {
        "total_bytes": total,
        "cap_bytes": policy.cap_bytes,
        "retention_hours": policy.retention_hours,
        "material_cache_max_bytes": policy.cache_max_bytes,
        "delete_after_delivery": policy.delete_after_delivery,
        "over_cap": bool(policy.cap_bytes and total > policy.cap_bytes),
        "categories": categories,
        "protected": ["publish/accounts.json"],
    }


def format_report(report: dict) -> str:
    cap = report.get("cap_bytes") or 0
    cap_text = f"{_mb(cap)} MB" if cap else "no hard cap"
    lines = [
        f"Storage uses {_mb(report.get('total_bytes') or 0)} MB of {cap_text}.",
        f"Rendered videos older than {report.get('retention_hours')} hours are removed.",
    ]
    for name, item in sorted((report.get("categories") or {}).items()):
        lines.append(f"{name}: {_mb(item.get('bytes') or 0)} MB, {item.get('files') or 0} files")
    if report.get("delete_after_delivery"):
        lines.append("Videos are deleted after a successful delivery and publish.")
    lines.append("OAuth account tokens are kept.")
    lines.append("Send /storage purge to delete videos, clips, temp files, and finished publish records now.")
    return "\n".join(lines)


def _inside(root: str, path: str) -> bool:
    real_root = os.path.realpath(root)
    real_path = os.path.realpath(path)
    return real_path == real_root or real_path.startswith(real_root + os.sep)


def _protected(root: str, path: str) -> bool:
    accounts = os.path.realpath(os.path.join(root, "publish", "accounts.json"))
    return os.path.realpath(path) == accounts


def _remove_path(root: str, path: str) -> int:
    if not _inside(root, path) or _protected(root, path) or os.path.islink(path):
        return 0
    try:
        size = os.path.getsize(path) if os.path.isfile(path) else 0
    except OSError:
        size = 0
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.isfile(path):
            os.remove(path)
        else:
            return 0
    except OSError as exc:
        logger.warning(f"could not delete {path}: {exc}")
        return 0
    return size


def _task_dirs(root: str) -> list[tuple[str, str, float, int]]:
    task_root = os.path.join(root, "tasks")
    rows = []
    if not os.path.isdir(task_root):
        return rows
    for name in os.listdir(task_root):
        path = os.path.join(task_root, name)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        if os.path.dirname(os.path.realpath(path)) != os.path.realpath(task_root):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        size, _count = _dir_usage(path)
        rows.append((name, path, mtime, size))
    rows.sort(key=lambda item: item[2])
    return rows


def _cache_files(root: str) -> list[tuple[str, float, int]]:
    rows = []
    for name in _CACHE_DIRS:
        for path, size, mtime in _walk_files(os.path.join(root, name)):
            rows.append((path, mtime, size))
    rows.sort(key=lambda item: item[1])
    return rows


def _parse_stamp(value) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def prune_finished_jobs(jobs_path: str, *, max_age_seconds: float | None, now: float) -> int:
    """Drop finished publish jobs. ``max_age_seconds is None`` drops every finished job.

    The accounts file is a different path and is not opened here.
    """

    if not os.path.isfile(jobs_path):
        return 0
    removed = {"count": 0}

    def mutate(data: dict) -> None:
        jobs = data.get("jobs")
        if not isinstance(jobs, dict):
            return
        for job_id, job in list(jobs.items()):
            if not isinstance(job, dict) or job.get("state") not in {"complete", "failed"}:
                continue
            if max_age_seconds is not None:
                stamp = _parse_stamp(job.get("updated_at") or job.get("created_at"))
                if stamp is None or now - stamp < max_age_seconds:
                    continue
            jobs.pop(job_id, None)
            removed["count"] += 1

    JsonStore(jobs_path).update(mutate)
    return removed["count"]


def cleanup_storage(
    root: str,
    policy: StoragePolicy,
    *,
    now: float | None = None,
    is_busy=None,
    delete_state=None,
    purge: bool = False,
) -> dict:
    """Remove expired outputs. ``purge`` ignores the age limit and the cache budget."""

    now = now if now is not None else datetime.now().timestamp()
    is_busy = is_busy or (lambda _task_id: False)
    os.makedirs(root, exist_ok=True)
    removed_bytes = 0
    removed_tasks: list[str] = []

    if purge:
        for task_id, path, mtime, _size in _task_dirs(root):
            if now - mtime < _GRACE_SECONDS:
                continue
            try:
                if is_busy(task_id):
                    continue
            except Exception:
                continue
            removed_bytes += _remove_path(root, path)
            removed_tasks.append(task_id)
            if delete_state is not None:
                try:
                    delete_state(task_id)
                except Exception as exc:
                    logger.warning(f"deleted task files for {task_id} but not its state: {exc}")
    elif policy.retention_hours > 0:
        removed_tasks = cleanup_expired_tasks(
            task_root=os.path.join(root, "tasks"),
            max_age_hours=policy.retention_hours,
            now=now,
            is_busy=is_busy,
            delete_state=delete_state,
        )
        for task_id in removed_tasks:
            # The directory is already gone. Count is reported by name only.
            pass

    grace = 0 if purge else _GRACE_SECONDS
    cache_budget = 0 if purge else policy.cache_max_bytes
    cache_rows = [row for row in _cache_files(root) if now - row[1] >= grace]
    cache_total = sum(size for _path, _mtime, size in cache_rows)
    if cache_budget and cache_total > cache_budget or purge:
        for path, _mtime, size in cache_rows:
            if not purge and cache_total <= cache_budget:
                break
            removed_bytes += _remove_path(root, path)
            cache_total -= size

    temp_age = 0 if purge else policy.temp_max_age_hours * 3600
    for path, size, mtime in _walk_files(os.path.join(root, "temp")):
        if now - mtime < max(temp_age, grace):
            continue
        removed_bytes += _remove_path(root, path)

    log_rows = sorted(_walk_files(os.path.join(root, "logs")), key=lambda item: item[2])
    for path, _size, _mtime in log_rows[:-3]:
        removed_bytes += _remove_path(root, path)

    jobs_path = os.path.join(root, "publish", "jobs.json")
    if purge:
        prune_finished_jobs(jobs_path, max_age_seconds=None, now=now)
    elif policy.retention_hours > 0:
        prune_finished_jobs(
            jobs_path,
            max_age_seconds=policy.retention_hours * 3600,
            now=now,
        )

    if policy.cap_bytes:
        removed_bytes += _enforce_cap(
            root,
            policy.cap_bytes,
            now=now,
            is_busy=is_busy,
            delete_state=delete_state,
        )

    report = storage_report(root, policy)
    report["removed_tasks"] = removed_tasks
    report["removed_bytes"] = removed_bytes
    return report


def _enforce_cap(root: str, cap_bytes: int, *, now: float, is_busy, delete_state=None) -> int:
    removed = 0
    report = storage_report(root, StoragePolicy(cap_bytes=cap_bytes))
    if report["total_bytes"] <= cap_bytes:
        return 0

    for task_id, path, mtime, _size in _task_dirs(root):
        if storage_report(root, StoragePolicy(cap_bytes=cap_bytes))["total_bytes"] <= cap_bytes:
            return removed
        if now - mtime < _GRACE_SECONDS:
            continue
        try:
            if is_busy(task_id):
                continue
        except Exception:
            continue
        removed += _remove_path(root, path)
        if delete_state is not None and not os.path.exists(path):
            try:
                delete_state(task_id)
            except Exception as exc:
                logger.warning(f"deleted task files for {task_id} but not its state: {exc}")

    for path, mtime, _size in _cache_files(root):
        if storage_report(root, StoragePolicy(cap_bytes=cap_bytes))["total_bytes"] <= cap_bytes:
            return removed
        if now - mtime < _GRACE_SECONDS:
            continue
        removed += _remove_path(root, path)

    for name in ("temp", "logs"):
        for path, _size, mtime in _walk_files(os.path.join(root, name)):
            if storage_report(root, StoragePolicy(cap_bytes=cap_bytes))["total_bytes"] <= cap_bytes:
                return removed
            if now - mtime < _GRACE_SECONDS:
                continue
            removed += _remove_path(root, path)

    leftover = storage_report(root, StoragePolicy(cap_bytes=cap_bytes))
    if leftover["total_bytes"] > cap_bytes:
        logger.warning(
            "storage is still over the cap after deleting videos and clips. "
            "OAuth tokens, local uploads, and background music were kept. "
            f"used={leftover['total_bytes']} cap={cap_bytes}"
        )
    return removed


def _default_root() -> str:
    from app.utils import utils

    return utils.storage_dir("", create=True)


def _default_busy(task_id: str) -> bool:
    try:
        from app.services import state as sm
        from app.services import task as task_service

        task = sm.state.get_task(task_id)
        if not task:
            return False
        return bool(task_service.is_task_busy(task))
    except Exception:
        return True


def _default_delete_state(task_id: str) -> None:
    from app.services import state as sm

    sm.state.delete_task(task_id)


def run_scheduled_cleanup() -> dict:
    policy = policy_from_config()
    report = cleanup_storage(
        _default_root(),
        policy,
        is_busy=_default_busy,
        delete_state=_default_delete_state,
        purge=False,
    )
    if report.get("removed_tasks") or report.get("removed_bytes"):
        logger.info(
            "storage cleanup removed "
            f"{len(report.get('removed_tasks') or [])} task(s), "
            f"{report.get('removed_bytes') or 0} bytes"
        )
    return report


def purge_storage() -> dict:
    policy = policy_from_config()
    report = cleanup_storage(
        _default_root(),
        policy,
        is_busy=_default_busy,
        delete_state=_default_delete_state,
        purge=True,
    )
    logger.info(
        "manual storage purge removed "
        f"{len(report.get('removed_tasks') or [])} task(s)"
    )
    return report


def report_storage() -> dict:
    root = _default_root()
    return storage_report(root, policy_from_config())


def _jobs_for_task(task_id: str) -> list[dict] | None:
    try:
        from app.publish.store import get_store

        jobs = get_store().jobs.read().get("jobs", {})
    except Exception as exc:
        logger.warning(f"could not read publish jobs before deleting {task_id}: {exc}")
        return None
    if not isinstance(jobs, dict):
        return []
    return [
        job
        for job in jobs.values()
        if isinstance(job, dict) and job.get("task_id") == task_id
    ]


def release_task_media(
    task_id: str,
    *,
    jobs: list[dict] | None,
    policy: StoragePolicy,
    root: str,
    require_all_published: bool,
    is_busy=None,
) -> bool:
    """Delete one rendered task when delivery and publishing are finished."""

    if not task_id or not policy.delete_after_delivery:
        return False
    if jobs is None:
        return False
    is_busy = is_busy or (lambda _task_id: False)
    try:
        if is_busy(task_id):
            return False
    except Exception:
        return False
    if jobs:
        states = {str(job.get("state") or "") for job in jobs}
        if states != {"complete"}:
            return False
    elif require_all_published:
        return False
    path = os.path.join(root, "tasks", task_id)
    if not os.path.isdir(path):
        return False
    _remove_path(root, path)
    try:
        from app.services import state as sm

        sm.state.delete_task(task_id)
    except Exception:
        pass
    logger.info(f"deleted rendered video after delivery: {task_id}")
    return True


def release_task_after_delivery(task_id: str) -> bool:
    policy = policy_from_config()
    if not policy.delete_after_delivery:
        return False
    return release_task_media(
        task_id,
        jobs=_jobs_for_task(task_id),
        policy=policy,
        root=_default_root(),
        require_all_published=False,
        is_busy=_default_busy,
    )


def release_task_after_publish(task_id: str) -> bool:
    policy = policy_from_config()
    if not policy.delete_after_delivery:
        return False
    return release_task_media(
        task_id,
        jobs=_jobs_for_task(task_id),
        policy=policy,
        root=_default_root(),
        require_all_published=True,
        is_busy=_default_busy,
    )
