from __future__ import annotations
import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from kestrel.api.routes import router
from kestrel.api.sessions import router as sessions_router
from kestrel.api.sessions_stream import router as sessions_stream_router
from kestrel.api.sessions_polling import router as sessions_polling_router
from kestrel.config import get_settings
from kestrel.execution.docker_executor import sweep_orphan_containers
from kestrel.execution import build_session_registry
from kestrel.audit import build_audit_sink
from kestrel.db.session import build_engine
from sqlalchemy.ext.asyncio import AsyncEngine
from kestrel.execution.session_registry import (
    RegistryUnavailable,
    SessionBusy,
    SessionNotFound,
)
from kestrel.execution.session_runtime import (
    SessionProtocolError,
    SessionTerminated,
)
from kestrel.logging import configure_logging
from kestrel.observability import HTTP_REQUESTS, HTTP_REQUEST_DURATION

logger = structlog.get_logger()

def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.executor_backend == "docker":
            await sweep_orphan_containers()
        registry = build_session_registry(settings)
        await registry.start()
        app.state.registry = registry

        engine: AsyncEngine | None = None
        if settings.audit_backend == "postgres":
            engine = build_engine(settings)

        try:
            audit_sink = build_audit_sink(settings, engine=engine)
            await audit_sink.start()
        except Exception:
            if engine is not None:
                await engine.dispose()
            await registry.aclose()
            raise

        app.state.audit_sink = audit_sink

        try:
            yield
        finally:
            await audit_sink.aclose()
            if engine is not None:
                await engine.dispose()
            await registry.aclose()

    app = FastAPI(title="Kestrel", lifespan=lifespan)
    app.include_router(router)
    app.include_router(sessions_router)
    app.include_router(sessions_stream_router)
    app.include_router(sessions_polling_router)

    @app.exception_handler(SessionNotFound)
    async def _session_not_found(request: Request, exc: SessionNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "session not found"})

    @app.exception_handler(SessionBusy)
    async def _session_busy(request: Request, exc: SessionBusy) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": "session_busy"})

    @app.exception_handler(SessionTerminated)
    async def _session_terminated(request: Request, exc: SessionTerminated) -> JSONResponse:
        return JSONResponse(status_code=410, content={"detail": "session is no longer running"})

    @app.exception_handler(SessionProtocolError)
    async def _session_protocol(request: Request, exc: SessionProtocolError) -> JSONResponse:
        logger.exception("session_protocol_error")
        return JSONResponse(status_code=500, content={"detail": "internal protocol error"})
    
    @app.exception_handler(RegistryUnavailable)
    async def _registry_unavailable(request: Request, exc: RegistryUnavailable) -> JSONResponse:
        logger.warning("registry_unavailable")
        return JSONResponse(status_code=503, content={"detail": "session store unavailable"})

    def _route_label(request: Request) -> str:
        route = request.scope.get("route")
        return getattr(route, "path", None) or "unmatched"

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
            duration = time.perf_counter() - start
            route_label = _route_label(request)
            HTTP_REQUESTS.labels(route=route_label, method=request.method, status="500").inc()
            HTTP_REQUEST_DURATION.labels(route=route_label, method=request.method).observe(duration)
            logger.exception("request_failed", duration_ms=int(duration * 1000))
            raise

        duration = time.perf_counter() - start
        route_label = _route_label(request)
        HTTP_REQUESTS.labels(
            route=route_label,
            method=request.method,
            status=str(response.status_code),
        ).inc()
        HTTP_REQUEST_DURATION.labels(route=route_label, method=request.method).observe(duration)

        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_finished",
            status_code=response.status_code,
            duration_ms=int(duration * 1000),
        )
        return response

    return app