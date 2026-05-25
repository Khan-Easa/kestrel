from __future__ import annotations

"""SQLAlchemy table models for Kestrel's Postgres-backed persistence.

Two-class split per decision 7-audit-schema-split:
- ``AuditEventRow`` (this file): the persisted row shape. SQLAlchemy-bound, has
a primary key, server-side timestamp default, indexed columns for the
queries operators actually run (request_id lookups, time-range scans).
- ``AuditEvent`` (``kestrel.audit``): the in-process payload that routes hand
to the sink. Pydantic, framework-free, lives on the asyncio queue.

The Postgres sink translates pydantic AuditEvent -> SA AuditEventRow at the
moment of insert. Doing the split this way means non-Postgres sinks
(``NullAuditSink`` today, possibly file/HTTP sinks later) don't pull
SQLAlchemy in.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    api_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    route: Mapped[str] = mapped_column(String(128), nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    code_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timed_out: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_audit_events_ts", "ts"),
        Index("ix_audit_events_request_id", "request_id"),
        Index("ix_audit_events_api_key_id", "api_key_id"),
    )