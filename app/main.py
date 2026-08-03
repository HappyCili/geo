from __future__ import annotations

from fastapi import FastAPI

from app.api import router
from app.logging_config import configure_app_logging


def create_app() -> FastAPI:
    configure_app_logging()
    app = FastAPI(title="媒体文章发布服务", version="2.0.0")
    app.include_router(router)
    return app


app = create_app()
