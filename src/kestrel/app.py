from __future__ import annotations
import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request

from kestrel.api.routes import router
from kestrel.config import get_settings
from kestrel.execution.docker_executor import sweep_orphan_containers
from kestrel.logging import configure_logging

logger = structlog.get_logger()

def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.executor_backend == "docker":
            await sweep_orphan_containers()
        yield

    app = FastAPI(title="Kestrel", lifespan=lifespan)
    app.include_router(router)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )

        logger.info("request_started")
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.exception("request_failed", duration_ms=duration_ms)
            raise
        duration_ms = int((time.perf_counter() - start) * 1000)

        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_finished",
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response

    return app