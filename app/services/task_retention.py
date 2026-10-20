"""Delete finished task directories that have sat on disk too long.

Stock footage and rendered videos accumulate under storage/tasks. The video
cache already has its own cleaner. This one covers the per-task output
directories and skips anything that is still rendering or uploading.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from typing import Callable

from loguru import logger


def cleanup_expired_tasks(
    *,
    task_root: str,
    max_age_hours: float,
    now: float | None = None,
    is_busy: Callable[[str], bool] | None = None,
    delete_state: Callable[[str], None] | None = None,
) -> list[str]:
    """Remove task folders older than ``max_age_hours``.

    ``max_age_hours <= 0`` disables cleanup. Symlinks that point outside the
    task root are left untouched. Returns the task ids that were removed.
    """

    if max_age_hours <= 0:
        return []
    if not task_root or not os.path.isdir(task_root):
        return []

    now = time.time() if now is None else now
    is_busy = is_busy or (lambda _task_id: False)
    delete_state = delete_state or (lambda _task_id: None)
    root = os.path.realpath(task_root)
    max_age_seconds = max_age_hours * 3600
    removed: list[str] = []

    try:
        names = os.listdir(root)
    except OSError as exc:
        logger.warning(f"could not list task directory {root}: {exc}")
        return []

    for name in names:
        candidate = os.path.join(root, name)
        try:
            resolved = os.path.realpath(candidate)
            if os.path.dirname(resolved) != root or not os.path.isdir(resolved):
                continue
            age = now - os.path.getmtime(resolved)
        except OSError as exc:
            logger.warning(f"skip task cleanup candidate {name}: {exc}")
            continue
        if age < max_age_seconds:
            continue
        try:
            if is_busy(name):
                logger.info(f"keep busy task during cleanup: {name}")
                continue
        except Exception as exc:
            # If we cannot tell whether a task is running, keep the files.
            logger.warning(f"could not check task {name} before cleanup: {exc}")
            continue
        try:
            shutil.rmtree(resolved)
        except OSError as exc:
            logger.warning(f"could not delete expired task {name}: {exc}")
            continue
        try:
            delete_state(name)
        except Exception as exc:
            logger.warning(f"deleted task files for {name} but not its state: {exc}")
        removed.append(name)
        logger.info(f"deleted expired task files: {name}")

    return removed


def retention_hours_from_config(app_config: dict, default: float = 72) -> float:
    raw = app_config.get("task_retention_hours", default)
    try:
        if isinstance(raw, bool):
            return default
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            f"task_retention_hours must be a number of hours, got {raw!r}; "
            f"using {default}"
        )
        return default


def start_retention_worker(interval_seconds: int = 3600) -> threading.Thread | None:
    """Sweep old tasks now and then once per interval. A non-positive retention disables it."""

    from app.config import config
    from app.services import state as sm
    from app.services import task as task_service
    from app.utils import utils

    hours = retention_hours_from_config(config.app)
    if hours <= 0:
        logger.info("task file cleanup is disabled (task_retention_hours <= 0)")
        return None

    def _busy(task_id: str) -> bool:
        task = sm.state.get_task(task_id)
        if not task:
            return False
        return task_service.is_task_busy(task)

    def _delete_state(task_id: str) -> None:
        sm.state.delete_task(task_id)

    def _loop() -> None:
        while True:
            try:
                removed = cleanup_expired_tasks(
                    task_root=utils.task_dir(),
                    max_age_hours=retention_hours_from_config(config.app),
                    is_busy=_busy,
                    delete_state=_delete_state,
                )
                if removed:
                    logger.info(f"task cleanup removed {len(removed)} expired task(s)")
            except Exception as exc:
                logger.exception(f"task cleanup failed: {exc}")
            time.sleep(interval_seconds)

    # Run one pass immediately so a restarted container does not wait an hour
    # to reclaim disk, then keep sweeping from the background thread.
    try:
        cleanup_expired_tasks(
            task_root=utils.task_dir(),
            max_age_hours=hours,
            is_busy=_busy,
            delete_state=_delete_state,
        )
    except Exception as exc:
        logger.exception(f"initial task cleanup failed: {exc}")

    thread = threading.Thread(target=_loop, name="task-retention", daemon=True)
    thread.start()
    logger.info(f"task cleanup will delete finished tasks older than {hours:g} hours")
    return thread
