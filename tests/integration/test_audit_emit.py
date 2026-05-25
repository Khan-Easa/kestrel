"""Phase 7 substep 2 slice 3: end-to-end audit-emit tests.

Each test fires a request against a TestClient whose lifespan built a real
PostgresAuditSink, then polls audit_events for a row matching the test's
X-Request-ID header. The drain task runs in TestClient's internal event
loop; the polling query runs in the test's own loop and waits for the
write to land.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import text


async def _wait_for_audit_row(engine, request_id: str, timeout: float = 5.0):
    """Poll audit_events for a row with the given request_id. Returns the row
    or None on timeout. Both loops talk to the same DB; the drain task in
    TestClient's loop inserts, this function's loop reads."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT request_id, route, method, status, session_id, "
                    "execution_id, code_length, exit_code, timed_out, "
                    "duration_ms, error_kind "
                    "FROM audit_events WHERE request_id = :rid"
                ),
                {"rid": request_id},
            )
            row = result.first()
        if row is not None:
            return row
        await asyncio.sleep(0.05)
    return None


async def test_execute_emits_audit_row(audit_postgres_client, postgres_engine):
    rid = "test-req-execute-success"
    response = audit_postgres_client.post(
        "/execute",
        json={"code": "print('hi')"},
        headers={"X-Request-ID": rid},
    )
    assert response.status_code == 200

    row = await _wait_for_audit_row(postgres_engine, rid)
    assert row is not None, f"no audit row for {rid}"
    assert row.route == "/execute"
    assert row.method == "POST"
    assert row.status == 200
    assert row.code_length == len("print('hi')")
    assert row.exit_code == 0
    assert row.timed_out is False
    assert row.duration_ms >= 0


async def test_session_lifecycle_emits_audit_rows(
    audit_postgres_client, postgres_engine
):
    create_rid = "test-req-session-create"
    create_resp = audit_postgres_client.post(
        "/sessions", headers={"X-Request-ID": create_rid}
    )
    assert create_resp.status_code == 201
    session_id = create_resp.json()["session_id"]

    get_rid = "test-req-session-get"
    get_resp = audit_postgres_client.get(
        f"/sessions/{session_id}", headers={"X-Request-ID": get_rid}
    )
    assert get_resp.status_code == 200

    delete_rid = "test-req-session-delete"
    delete_resp = audit_postgres_client.delete(
        f"/sessions/{session_id}", headers={"X-Request-ID": delete_rid}
    )
    assert delete_resp.status_code == 204

    create_row = await _wait_for_audit_row(postgres_engine, create_rid)
    assert create_row is not None
    assert create_row.status == 201
    assert create_row.session_id == session_id

    get_row = await _wait_for_audit_row(postgres_engine, get_rid)
    assert get_row is not None
    assert get_row.status == 200
    assert get_row.session_id == session_id

    delete_row = await _wait_for_audit_row(postgres_engine, delete_rid)
    assert delete_row is not None
    assert delete_row.status == 204
    assert delete_row.session_id == session_id


async def test_session_execute_emits_audit_row(
    audit_postgres_client, postgres_engine
):
    create_resp = audit_postgres_client.post(
        "/sessions", headers={"X-Request-ID": "test-req-session-create-2"}
    )
    session_id = create_resp.json()["session_id"]

    rid = "test-req-session-execute"
    exec_resp = audit_postgres_client.post(
        f"/sessions/{session_id}/execute",
        json={"code": "print('hi')"},
        headers={"X-Request-ID": rid},
    )
    assert exec_resp.status_code == 200

    row = await _wait_for_audit_row(postgres_engine, rid)
    assert row is not None
    assert row.route == "/sessions/{session_id}/execute"
    assert row.method == "POST"
    assert row.status == 200
    assert row.session_id == session_id
    assert row.code_length == len("print('hi')")
    assert row.exit_code == 0


async def test_session_not_found_emits_audit_row(
    audit_postgres_client, postgres_engine
):
    rid = "test-req-session-not-found"
    resp = audit_postgres_client.get(
        "/sessions/00000000000000000000000000000000",
        headers={"X-Request-ID": rid},
    )
    assert resp.status_code == 404

    row = await _wait_for_audit_row(postgres_engine, rid)
    assert row is not None
    assert row.status == 404
    assert row.error_kind == "SessionNotFound"


async def test_polling_emits_audit_row(audit_postgres_client, postgres_engine):
    create_resp = audit_postgres_client.post(
        "/sessions", headers={"X-Request-ID": "test-req-create-polling"}
    )
    session_id = create_resp.json()["session_id"]

    rid = "test-req-polling"
    post_resp = audit_postgres_client.post(
        f"/sessions/{session_id}/execute/polling",
        json={"code": "print('hi')"},
        headers={"X-Request-ID": rid},
    )
    assert post_resp.status_code == 202
    execution_id = post_resp.json()["execution_id"]

    # Drain via repeated GETs until done.
    for _ in range(30):
        get_resp = audit_postgres_client.get(
            f"/sessions/{session_id}/executions/{execution_id}?wait=1.0",
        )
        if get_resp.json()["done"]:
            break

    row = await _wait_for_audit_row(postgres_engine, rid, timeout=10.0)
    assert row is not None
    assert row.route == "/sessions/{session_id}/execute/polling"
    assert row.session_id == session_id
    assert row.execution_id == execution_id
    assert row.code_length == len("print('hi')")
    assert row.status == 200
    assert row.exit_code == 0


async def test_stream_emits_audit_row(audit_postgres_client, postgres_engine):
    create_resp = audit_postgres_client.post(
        "/sessions", headers={"X-Request-ID": "test-req-create-stream"}
    )
    session_id = create_resp.json()["session_id"]

    rid = "test-req-stream"
    with audit_postgres_client.websocket_connect(
        f"/sessions/{session_id}/execute/stream",
        headers={"X-Request-ID": rid},
    ) as ws:
        ws.send_json({"code": "print('hi')"})
        while True:
            msg = ws.receive_json()
            if msg["type"] == "result":
                break

    row = await _wait_for_audit_row(postgres_engine, rid, timeout=10.0)
    assert row is not None
    assert row.method == "WS"
    assert row.session_id == session_id
    assert row.status == 200
    assert row.code_length == len("print('hi')")
    assert row.exit_code == 0