"""Application configuration - root APIRouter

Defines all FastAPI application endpoints.

Resources:
    1. https://fastapi.tiangolo.com/tutorial/bigger-applications

"""

from fastapi import APIRouter

from app.controllers import ping, telegram_http
from app.controllers.v1 import llm, miniapp, video

root_api_router = APIRouter()
root_api_router.include_router(ping.router)
root_api_router.include_router(telegram_http.router)

# v1
root_api_router.include_router(video.router)
root_api_router.include_router(llm.router)
root_api_router.include_router(miniapp.router)
