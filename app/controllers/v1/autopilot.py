"""Operator endpoints for the unattended Shorts worker."""

from __future__ import annotations

from fastapi import Body, Depends

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.services import autopilot
from app.utils import utils

router = new_router(dependencies=[Depends(base.verify_token)])


@router.get("/autopilot", summary="Autopilot schedule, queue and recent runs")
def autopilot_status():
    return utils.get_response(200, autopilot.status(), "success")


@router.post("/autopilot/run", summary="Queue unattended Shorts now")
def autopilot_run(body: dict = Body(default=None)):
    body = body if isinstance(body, dict) else {}
    try:
        count = int(body.get("count") or 1)
    except (TypeError, ValueError):
        raise HttpException(task_id="", status_code=400, message="count must be a number") from None
    topics = body.get("topics") or []
    if not isinstance(topics, list):
        raise HttpException(task_id="", status_code=400, message="topics must be a list")
    if count < 1 or count > 10 or len(topics) > 10:
        raise HttpException(task_id="", status_code=400, message="queue 1 to 10 videos at a time")
    items = autopilot.enqueue(count, topics, reason="manual", keep=bool(body.get("keep")))
    return utils.get_response(200, {"queued": items}, "queued")
