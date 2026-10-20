"""Resolve a finished task's video file inside that task's directory."""

from __future__ import annotations

import os

from app.utils import file_security, utils


def task_video_path(task_id: str) -> str | None:
    """Return a real video file for this task, or None when it is not ready."""

    from app.services import state as sm

    task = sm.state.get_task(task_id)
    if not task:
        return None

    tasks_root = os.path.realpath(utils.task_dir())
    task_root = os.path.realpath(utils.task_dir(task_id))
    candidates: list[str] = []
    for key in ("combined_videos", "videos"):
        values = task.get(key) or []
        if isinstance(values, list):
            candidates.extend(value for value in values if isinstance(value, str))

    for candidate in candidates:
        if candidate.startswith(("http://", "https://")):
            continue
        try:
            resolved = file_security.resolve_path_within_directory(tasks_root, candidate)
        except ValueError:
            continue
        if os.path.commonpath([task_root, resolved]) != task_root:
            continue
        if os.path.isfile(resolved):
            return resolved

    if not os.path.isdir(task_root):
        return None
    for name in sorted(os.listdir(task_root)):
        if name.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
            return os.path.join(task_root, name)
    return None
