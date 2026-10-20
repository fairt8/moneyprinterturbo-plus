"""Submit and read tasks inside the API process."""

from __future__ import annotations

from app.telegram.jobs import task_video_path
from app.telegram.logic import JobOptions, PipelineError
from app.telegram.presets import describe_stage


def _local_request():
    from starlette.requests import Request

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/miniapp/videos",
        "raw_path": b"/api/v1/miniapp/videos",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 0),
        "server": ("127.0.0.1", 80),
    }
    return Request(scope)


class InProcessPipeline:
    def submit(self, owner: str, topic: str, options: JobOptions) -> str:
        from app.controllers.v1.video import create_task
        from app.models.exception import HttpException
        from app.models.schema import TaskVideoRequest

        try:
            body = TaskVideoRequest(
                video_subject=topic,
                video_aspect=options.aspect,
                voice_name=options.voice,
                paragraph_number=options.paragraphs,
                video_language=options.language or "",
                subtitle_enabled=options.subtitles,
                video_source=options.video_source or "pexels",
            )
            result = create_task(_local_request(), body, stop_at="video")
        except HttpException as exc:
            raise PipelineError(exc.message) from exc
        except ValueError as exc:
            raise PipelineError(str(exc)) from exc

        data = result.get("data") if isinstance(result, dict) else None
        task_id = data.get("task_id") if isinstance(data, dict) else None
        if not task_id:
            raise PipelineError("The video service did not return a task id.")
        from app.telegram.runtime import get_job_store

        get_job_store().bind(task_id, owner)
        return task_id

    def snapshot(self, task_id: str, owner: str) -> dict:
        from app.services import state as sm

        task = sm.state.get_task(task_id)
        if not task:
            raise PipelineError("No video with that id. It may have expired.")
        state = int(task.get("state") or 0)
        progress = int(task.get("progress") or 0)
        path = task_video_path(task_id) if state == 1 else None
        return {
            "task_id": task_id,
            "state": state,
            "progress": progress,
            "stage": describe_stage(progress, state),
            "error": task.get("error"),
            "failed_stage": task.get("failed_stage"),
            "files": [path] if path else [],
        }
