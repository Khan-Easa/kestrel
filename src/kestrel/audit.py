from __future__ import annotations

"""Phase 7 substep 2: audit-event pipeline (Protocol + Null + Postgres backends).

Routes build an ``AuditEvent`` and call ``await sink.emit(event)``. The sink
is selected at startup by ``build_audit_sink(settings, engine=...)``:

- ``audit_backend = "null"`` (default): ``NullAuditSink``, a no-op. Dev mode
and the existing test suite stay Postgres-free.
- ``audit_backend = "postgres"``: ``PostgresAuditSink``. Fire-and-forget through
a bounded asyncio.Queue (decision 7-audit-sync); a background drain task
inserts events one at a time via SQLAlchemy async sessions. Queue overflow
and per-event insert failures both increment ``kestrel_audit_dropped_total``.
The engine is owned by the FastAPI lifespan (decision 7.2-engine-owner) and
must be passed into the factory when ``audit_backend='postgres'``.

Two ``AuditEvent`` shapes exist by design (decision 7-audit-schema-split):
this pydantic one (in-process / queue payload, framework-free) vs the SQLAlchemy
``AuditEventRow`` in ``kestrel.db.models`` (persisted row, indexed columns).
The Postgres sink translates between them at insert time.
"""

import asyncio
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

import structlog
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine
from fastapi import Request

from kestrel.config import Settings
from kestrel.db.models import AuditEventRow
from kestrel.db.session import build_sessionmaker
from kestrel.observability import AUDIT_DROPPED

logger = structlog.get_logger()


class AuditEvent(BaseModel):
    """An audited request/response event. Built by routes, drained by sinks."""

    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    request_id: str
    api_key_id: str | None = None
    route: str
    method: str
    status: int
    session_id: str | None = None
    execution_id: str | None = None
    code_length: int | None = None
    exit_code: int | None = None
    timed_out: bool | None = None
    duration_ms: int | None = None
    error_kind: str | None = None


@runtime_checkable
class AuditSink(Protocol):
    """The audit pipeline's contract. All sinks must be safe to call from
    request handlers — emit() must not block on slow I/O. Postgres-backed
    sinks queue and drain in the background; the null sink no-ops."""

    async def start(self) -> None: ...
    async def aclose(self) -> None: ...
    async def emit(self, event: AuditEvent) -> None: ...


class NullAuditSink:
    """No-op sink. The default when ``audit_backend = "null"`` (dev, tests,
    and any deployment that hasn't opted into Postgres audit)."""

    async def start(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def emit(self, event: AuditEvent) -> None:
        return None


class PostgresAuditSink:
    """Fire-and-forget Postgres sink. emit() queues; a background task drains.

    Lifecycle (driven by the FastAPI lifespan):
    - ``start()`` creates the bounded ``asyncio.Queue`` and launches the
    drain task. Must be awaited on a running event loop.
    - ``emit(event)`` is non-blocking: appends to the queue or drops + bumps
    ``AUDIT_DROPPED`` on overflow.
    - ``aclose()`` flags ``_stopping``, waits up to
    ``settings.audit_shutdown_drain_seconds`` for the queue to drain, then
    cancels the drain task. Any events still queued at the deadline are
    counted as dropped.

    The engine is injected (NOT built here) so the lifespan owns it — substep 3's
    API-key store will share the same engine.
    """

    def __init__(self, settings: Settings, engine: AsyncEngine) -> None:
        self._settings = settings
        self._engine = engine
        self._sessionmaker = build_sessionmaker(engine)
        self._queue: asyncio.Queue[AuditEvent] | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._queue = asyncio.Queue(maxsize=self._settings.audit_queue_max_size)
        self._drain_task = asyncio.create_task(self._drain_loop(), name="audit-drain")
        logger.info(
            "postgres_audit_sink_started",
            queue_max=self._settings.audit_queue_max_size,
        )

    async def emit(self, event: AuditEvent) -> None:
        if self._queue is None or self._stopping.is_set():
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            AUDIT_DROPPED.inc()
            logger.warning("audit_queue_full_dropped", request_id=event.request_id)

    async def _drain_loop(self) -> None:
        assert self._queue is not None
        while True:
            try:
                event = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                await self._insert_one(event)
            except Exception:
                # Belt-and-suspenders: _insert_one already swallows + counts its
                # own errors, but if anything escapes (programmer bug, unexpected
                # exception class), keep the drain task alive — the alternative
                # is silent permanent audit loss until restart.
                AUDIT_DROPPED.inc()
                logger.exception(
                    "audit_drain_unhandled", request_id=event.request_id
                )
            finally:
                self._queue.task_done()

    async def _insert_one(self, event: AuditEvent) -> None:
        try:
            async with self._sessionmaker() as session:
                session.add(
                    AuditEventRow(
                        request_id=event.request_id,
                        ts=event.ts,
                        api_key_id=event.api_key_id,
                        route=event.route,
                        method=event.method,
                        status=event.status,
                        session_id=event.session_id,
                        execution_id=event.execution_id,
                        code_length=event.code_length,
                        exit_code=event.exit_code,
                        timed_out=event.timed_out,
                        duration_ms=event.duration_ms,
                        error_kind=event.error_kind,
                    )
                )
                await session.commit()
        except Exception:
            AUDIT_DROPPED.inc()
            logger.exception(
                "audit_insert_failed",
                request_id=event.request_id,
                route=event.route,
            )

    async def aclose(self) -> None:
        if self._drain_task is None or self._queue is None:
            return
        self._stopping.set()
        try:
            await asyncio.wait_for(
                self._queue.join(),
                timeout=self._settings.audit_shutdown_drain_seconds,
            )
        except asyncio.TimeoutError:
            remaining = self._queue.qsize()
            for _ in range(remaining):
                AUDIT_DROPPED.inc()
            logger.warning("audit_shutdown_drain_timeout", dropped=remaining)
        self._drain_task.cancel()
        try:
            await self._drain_task
        except asyncio.CancelledError:
            pass
        self._drain_task = None
        self._queue = None
        logger.info("postgres_audit_sink_stopped")


def build_audit_sink(
    settings: Settings, engine: AsyncEngine | None = None
) -> AuditSink:
    """Build the audit sink named by ``settings.audit_backend``.

    Called once at app startup (FastAPI lifespan), alongside
    ``build_session_registry``. Returns the Protocol type so the caller never
    sees the concrete class. When ``audit_backend='postgres'``, ``engine``
    is required — the lifespan builds the engine and passes it in so the
    sink doesn't own it (decision 7.2-engine-owner).
    """
    if settings.audit_backend == "postgres":
        if engine is None:
            raise ValueError(
                "PostgresAuditSink requires an engine; the lifespan must call "
                "build_engine(settings) when audit_backend='postgres'."
            )
        return PostgresAuditSink(settings, engine)
    return NullAuditSink()


def get_audit_sink(request: Request) -> AuditSink:
    """FastAPI dependency: returns the audit sink bound to ``app.state`` by
    the lifespan. Routes inject via ``audit: AuditSink = Depends(get_audit_sink)``.

    WebSocket routes need their own provider (the parameter type would be
    ``WebSocket``, not ``Request``); see ``sessions_stream.py``.
    """
    return request.app.state.audit_sink


def http_status_for_exception(exc: BaseException) -> int:
    """Map Kestrel's known exception classes to the HTTP status the app
    returns for them, for use in audit row ``status`` fields.

    Routes wrap their work in try/except and call this on any caught
    exception to set the right audit status. Unknown exception classes map
    to 500 (the conservative default for unhandled errors).
    """
    from kestrel.execution.session_registry import (
        RegistryUnavailable,
        SessionBusy,
        SessionNotFound,
    )
    from kestrel.execution.session_runtime import (
        SessionProtocolError,
        SessionTerminated,
        SessionTimeout,
    )

    if isinstance(exc, SessionNotFound):
        return 404
    if isinstance(exc, SessionBusy):
        return 409
    if isinstance(exc, (SessionTerminated, SessionTimeout)):
        return 410
    if isinstance(exc, RegistryUnavailable):
        return 503
    if isinstance(exc, SessionProtocolError):
        return 500
    return 500