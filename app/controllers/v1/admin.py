"""Disk usage for the operator. Videos are disposable. OAuth tokens are not."""

from __future__ import annotations

from fastapi import Body, Depends

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.services.storage_hygiene import format_report, purge_storage, report_storage
from app.utils import utils

router = new_router(dependencies=[Depends(base.verify_token)])


@router.get(
    "/admin/storage",
    summary="Show storage usage",
)
def read_storage():
    report = report_storage()
    report["summary"] = format_report(report)
    return utils.get_response(200, report, "success")


@router.post(
    "/admin/storage",
    summary="Purge rendered videos, clips, temp files, and finished publish records",
)
def purge(body: dict = Body(default=None)):
    action = ""
    if isinstance(body, dict):
        action = str(body.get("action") or "").strip().lower()
    if action not in {"", "purge"}:
        raise HttpException(task_id="", status_code=400, message="action must be 'purge'")
    report = purge_storage()
    report["summary"] = format_report(report)
    return utils.get_response(200, report, "purged")
