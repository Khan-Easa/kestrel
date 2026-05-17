"""Phase 6 substep 3 tests: WebSocket route shape + auth + close codes.

Substep 3 scope: lock the route URL, the auth handshake, the close-code
semantics, and the single-execute-per-connection wire shape. Substep 4
will add tests for real streaming behavior (chunks, heartbeats, etc.).

Uses FastAPI's TestClient.websocket_connect() — synchronous context
manager that wraps an anyio portal under the hood.
"""
from __future__ import annotations

import pytest
from fastapi.websockets import WebSocketDisconnect


def test_stream_unknown_session_closes_4404(session_http_client):
    """An unknown session_id should close with application code 4404 at handshake."""
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with session_http_client.websocket_connect(
            "/sessions/00000000000000000000000000000000/execute/stream"
        ) as ws:
            ws.send_text('{"code": "print(1)"}')
            ws.receive_text()
    assert exc_info.value.code == 4404


def test_stream_valid_session_receives_result_and_closes_1000(session_http_client):
    """Happy path: connect, send execute request, receive a result message, close cleanly."""
    sid = session_http_client.post("/sessions").json()["session_id"]

    with session_http_client.websocket_connect(
        f"/sessions/{sid}/execute/stream"
    ) as ws:
        ws.send_text('{"code": "print(42)"}')
        message = ws.receive_json()

    assert message["type"] == "result"
    assert message["exit_code"] == 0
    assert message["outputs"] == []
    assert message["dropped_outputs"] == []
    assert "stdout" in message
    assert "stderr" in message


def test_stream_requires_auth_when_key_set(session_http_client_authed):
    """With KESTREL_DEV_API_KEY set, missing token closes with 4401 at handshake."""
    sid_response = session_http_client_authed.post(
        "/sessions",
        headers={"Authorization": "Bearer test-secret-12345"},
    )
    sid = sid_response.json()["session_id"]

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with session_http_client_authed.websocket_connect(
            f"/sessions/{sid}/execute/stream"
        ) as ws:
            ws.receive_text()
    assert exc_info.value.code == 4401


def test_stream_accepts_header_auth(session_http_client_authed):
    """Authorization: Bearer <token> at handshake works."""
    sid = session_http_client_authed.post(
        "/sessions",
        headers={"Authorization": "Bearer test-secret-12345"},
    ).json()["session_id"]

    with session_http_client_authed.websocket_connect(
        f"/sessions/{sid}/execute/stream",
        headers={"Authorization": "Bearer test-secret-12345"},
    ) as ws:
        ws.send_text('{"code": "print(1)"}')
        message = ws.receive_json()

    assert message["type"] == "result"


def test_stream_accepts_query_param_auth(session_http_client_authed):
    """?token=<token> query param is a fallback for clients that can't set headers (browsers)."""
    sid = session_http_client_authed.post(
        "/sessions",
        headers={"Authorization": "Bearer test-secret-12345"},
    ).json()["session_id"]

    with session_http_client_authed.websocket_connect(
        f"/sessions/{sid}/execute/stream?token=test-secret-12345"
    ) as ws:
        ws.send_text('{"code": "print(1)"}')
        message = ws.receive_json()

    assert message["type"] == "result"


def test_stream_wrong_token_closes_4401(session_http_client_authed):
    """A wrong bearer token closes with 4401 — same as missing."""
    sid = session_http_client_authed.post(
        "/sessions",
        headers={"Authorization": "Bearer test-secret-12345"},
    ).json()["session_id"]

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with session_http_client_authed.websocket_connect(
            f"/sessions/{sid}/execute/stream",
            headers={"Authorization": "Bearer wrong-token"},
        ) as ws:
            ws.receive_text()
    assert exc_info.value.code == 4401