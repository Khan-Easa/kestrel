from __future__ import annotations

"""Phase 7 substep 2: audit-event pipeline (Protocol + Null backend).

Routes build an ``AuditEvent`` and call ``await sink.emit(event)``. The sink
is selected at startup by ``build_audit_sink(settings)``:

- ``audit_backend = "null"`` (default): ``NullAuditSink``, a no-op. Dev mode
and the existing test suite stay Postgres-free.
- ``audit_backend = "postgres"``: ``PostgresAuditSink`` (added in slice 2).
Fire-and-forget via a bounded asyncio.Queue per decision 7-audit-sync.

Two ``AuditEvent`` shapes exist by design (decision 7-audit-schema-split):
this pydantic one (in-process / queue payload, framework-free) vs the SQLAlchemy
``AuditEventRow`` in ``kestrel.db.models`` (persisted row, indexed columns).
The Postgres sink translates between them at insert time.
"""

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from kestrel.config import Settings


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


def build_audit_sink(settings: Settings) -> AuditSink:
    """Build the audit sink named by ``settings.audit_backend``.

    Called once at app startup (FastAPI lifespan), alongside
    ``build_session_registry``. Returns the Protocol type so the caller never
    sees the concrete class.
    """
    if settings.audit_backend == "postgres":
        # Slice 2 will wire PostgresAuditSink here. Until then, opting into
        # postgres is a configuration error rather than a silent downgrade.
        raise NotImplementedError(
            "audit_backend='postgres' lands in Phase 7 substep 2 slice 2"
        )
    return NullAuditSink()