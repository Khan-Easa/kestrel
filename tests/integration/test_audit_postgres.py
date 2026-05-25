"""Phase 7 substep 2 slice 2: PostgresAuditSink integration tests.

Requires kestrel-postgres container at localhost:5432 (skips when unreachable).
Migrations applied once per pytest session; audit_events truncated per test.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from kestrel.audit import AuditEvent, PostgresAuditSink, build_audit_sink
from kestrel.config import Settings
from kestrel.observability import AUDIT_DROPPED


def _audit_dropped_count() -> float:
    """Read the current value of kestrel_audit_dropped_total."""
    for family in AUDIT_DROPPED.collect():
        for sample in family.samples:
            if sample.name.endswith("_total"):
                return sample.value
    return 0.0


async def test_emit_inserts_audit_row(postgres_audit_sink_factory, postgres_engine):
    sink = await postgres_audit_sink_factory()
    event = AuditEvent(
        request_id="req-emit-1",
        route="/execute",
        method="POST",
        status=200,
        code_length=42,
        exit_code=0,
        duration_ms=100,
    )
    await sink.emit(event)
    await asyncio.wait_for(sink._queue.join(), timeout=2.0)

    async with postgres_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT request_id, route, method, status, code_length, exit_code, duration_ms "
                "FROM audit_events WHERE request_id = 'req-emit-1'"
            )
        )
        row = result.first()
    assert row is not None
    assert row.request_id == "req-emit-1"
    assert row.route == "/execute"
    assert row.method == "POST"
    assert row.status == 200
    assert row.code_length == 42
    assert row.exit_code == 0
    assert row.duration_ms == 100


async def test_emit_after_aclose_silently_drops(postgres_audit_sink_factory):
    sink = await postgres_audit_sink_factory()
    await sink.aclose()
    event = AuditEvent(
        request_id="req-after-close",
        route="/x",
        method="GET",
        status=200,
    )
    await sink.emit(event)  # must not raise


async def test_drop_on_overflow_bumps_counter(postgres_audit_sink_factory):
    before = _audit_dropped_count()
    sink = await postgres_audit_sink_factory(audit_queue_max_size=2)

    # Block the drain so the queue fills.
    blocker = asyncio.Event()
    orig_insert = sink._insert_one

    async def slow_insert(event):
        await blocker.wait()
        await orig_insert(event)

    sink._insert_one = slow_insert

    # 1 event is grabbed by get() and parked at blocker; 2 fit in the queue;
    # events 4 and 5 should overflow.
    for i in range(5):
        await sink.emit(
            AuditEvent(
                request_id=f"req-overflow-{i}",
                route="/x",
                method="GET",
                status=200,
            )
        )

    after = _audit_dropped_count()
    assert after >= before + 2, f"expected ≥2 drops, saw {before} → {after}"

    blocker.set()  # let teardown drain


async def test_drain_survives_transient_error(
    postgres_audit_sink_factory, postgres_engine
):
    sink = await postgres_audit_sink_factory()
    orig_insert = sink._insert_one
    call_count = {"n": 0}

    async def failing_then_succeeding(event):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated DB error")
        await orig_insert(event)

    sink._insert_one = failing_then_succeeding

    await sink.emit(
        AuditEvent(request_id="req-fail", route="/x", method="GET", status=500)
    )
    await sink.emit(
        AuditEvent(request_id="req-ok", route="/x", method="GET", status=200)
    )
    await asyncio.wait_for(sink._queue.join(), timeout=2.0)

    async with postgres_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT request_id FROM audit_events "
                "WHERE request_id IN ('req-fail', 'req-ok') ORDER BY request_id"
            )
        )
        rows = result.fetchall()
    assert len(rows) == 1
    assert rows[0].request_id == "req-ok"


async def test_aclose_drains_pending_events(
    postgres_audit_sink_factory, postgres_engine
):
    sink = await postgres_audit_sink_factory(audit_shutdown_drain_seconds=5.0)
    for i in range(5):
        await sink.emit(
            AuditEvent(
                request_id=f"req-shutdown-{i}",
                route="/x",
                method="POST",
                status=200,
            )
        )
    await sink.aclose()

    async with postgres_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT COUNT(*) AS n FROM audit_events "
                "WHERE request_id LIKE 'req-shutdown-%'"
            )
        )
        row = result.first()
    assert row.n == 5


def test_build_audit_sink_with_engine_returns_postgres_sink(postgres_engine):
    settings = Settings(
        audit_backend="postgres",
        database_url="postgresql+asyncpg://x/y",  # ignored — engine wins
    )
    sink = build_audit_sink(settings, engine=postgres_engine)
    assert isinstance(sink, PostgresAuditSink)