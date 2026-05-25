"""Phase 7 substep 2 slice 1: audit scaffolding tests.

No Postgres required. Verifies factory selection, Null sink no-op behavior,
lifespan binding, and and that the postgres path requires an engine (lifespan owns it,\ndecision 7.2-engine-owner).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kestrel.app import create_app
from kestrel.audit import AuditEvent, AuditSink, NullAuditSink, build_audit_sink
from kestrel.config import Settings, get_settings


def test_build_audit_sink_defaults_to_null():
    settings = Settings()
    sink = build_audit_sink(settings)
    assert isinstance(sink, NullAuditSink)
    assert isinstance(sink, AuditSink)


def test_build_audit_sink_postgres_requires_engine():
    settings = Settings(audit_backend="postgres", database_url="postgresql+asyncpg://x/y")
    with pytest.raises(ValueError, match="engine"):
        build_audit_sink(settings)


async def test_null_audit_sink_emit_is_no_op():
    sink = NullAuditSink()
    await sink.start()
    event = AuditEvent(
        request_id="req-1",
        route="/execute",
        method="POST",
        status=200,
    )
    await sink.emit(event)
    await sink.aclose()


def test_audit_event_accepts_optional_fields_as_none():
    event = AuditEvent(request_id="r", route="/x", method="GET", status=200)
    assert event.api_key_id is None
    assert event.session_id is None
    assert event.code_length is None


def test_lifespan_binds_audit_sink_on_app_state():
    app = create_app()
    settings = Settings(executor_backend="subprocess")
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        assert hasattr(client.app.state, "audit_sink")
        assert isinstance(client.app.state.audit_sink, NullAuditSink)