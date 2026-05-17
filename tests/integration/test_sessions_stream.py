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
    """Happy path: connect, send execute request, drain chunks until result, close cleanly."""
    sid = session_http_client.post("/sessions").json()["session_id"]

    with session_http_client.websocket_connect(
        f"/sessions/{sid}/execute/stream"
    ) as ws:
        ws.send_text('{"code": "print(42)"}')
        while True:
            message = ws.receive_json()
            if message["type"] == "result":
                break

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
        while True:
            message = ws.receive_json()
            if message["type"] == "result":
                break

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
        while True:
            message = ws.receive_json()
            if message["type"] == "result":
                break

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


def test_stream_emits_multiple_chunks_then_result(session_http_client):
    """User code that prints multiple times produces multiple stdout messages + one result."""
    sid = session_http_client.post("/sessions").json()["session_id"]

    messages: list[dict] = []
    with session_http_client.websocket_connect(
        f"/sessions/{sid}/execute/stream"
    ) as ws:
        ws.send_text('{"code": "print(\\"one\\")\\nprint(\\"two\\")\\nprint(\\"three\\")"}')
        while True:
            msg = ws.receive_json()
            messages.append(msg)
            if msg["type"] == "result":
                break

    types = [m["type"] for m in messages]
    assert types[-1] == "result"
    # At least one stdout chunk per print; print() typically yields a "one" + "\n" pair.
    stdout_chunks = [m for m in messages if m["type"] == "stdout"]
    assert len(stdout_chunks) >= 3
    # The result message carries the full coalesced stdout — verify it contains all three lines.
    result = messages[-1]
    assert result["exit_code"] == 0
    assert "one" in result["stdout"]
    assert "two" in result["stdout"]
    assert "three" in result["stdout"]


def test_stream_concurrent_execute_returns_4409(session_http_client):
    """A second WebSocket connection on the same session while the first is running gets 4409."""
    import threading

    sid = session_http_client.post("/sessions").json()["session_id"]

    fast_close_code: dict = {}
    slow_done = threading.Event()

    def slow_stream() -> None:
        with session_http_client.websocket_connect(
            f"/sessions/{sid}/execute/stream"
        ) as ws:
            ws.send_text('{"code": "import time; time.sleep(2); print(\\"done\\")"}')
            while True:
                msg = ws.receive_json()
                if msg["type"] == "result":
                    break
        slow_done.set()

    def fast_stream() -> None:
        # Give the slow stream a head start so the lock is held when we connect.
        import time
        time.sleep(0.5)
        try:
            with session_http_client.websocket_connect(
                f"/sessions/{sid}/execute/stream"
            ) as ws:
                ws.send_text('{"code": "print(\\"fast\\")"}')
                # Expect the StreamError message before the close.
                err_msg = ws.receive_json()
                assert err_msg["type"] == "error"
                err_msg = ws.receive_json()
                assert err_msg["type"] == "error"
                assert err_msg["code"] == "session_busy"
                # Receiving past the error should raise WebSocketDisconnect with 4409.
                ws.receive_json()
        except WebSocketDisconnect as exc:
            fast_close_code["code"] = exc.code

    t1 = threading.Thread(target=slow_stream)
    t2 = threading.Thread(target=fast_stream)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert slow_done.is_set(), "slow stream did not finish in time"
    assert fast_close_code.get("code") == 4409


def test_stream_disconnect_mid_execute_terminates_session(session_http_client):
    """Client disconnect mid-stream kills the kernel; subsequent HTTP execute on the session returns 410."""
    sid = session_http_client.post("/sessions").json()["session_id"]

    # Start a long-running execute, then drop the connection by exiting the context early.
    with session_http_client.websocket_connect(
        f"/sessions/{sid}/execute/stream"
    ) as ws:
        ws.send_text('{"code": "import time\\nfor i in range(50):\\n    print(i); time.sleep(0.1)"}')
        # Consume one chunk to ensure the kernel is actually streaming, then disconnect.
        first = ws.receive_json()
        assert first["type"] in ("stdout", "result")
        # Context exit closes the WebSocket — should trigger 6-disconnect on the server.

    # Give the server a moment to process the disconnect + kill the kernel.
    import time
    time.sleep(0.5)

    # Subsequent HTTP execute on the same session_id should now return 410 Gone.
    follow_up = session_http_client.post(
        f"/sessions/{sid}/execute",
        json={"code": "print(1)"},
    )
    assert follow_up.status_code == 410


def test_stream_emits_heartbeat_during_silent_interval(session_http_client_factory):
    """With a short heartbeat interval and a sleeping kernel, at least one
    heartbeat message arrives before the kernel's print + result."""
    client = session_http_client_factory(stream_heartbeat_seconds=0.5)
    sid = client.post("/sessions").json()["session_id"]

    messages: list[dict] = []
    with client.websocket_connect(f"/sessions/{sid}/execute/stream") as ws:
        ws.send_text('{"code": "import time; time.sleep(2); print(\\"done\\")"}')
        while True:
            msg = ws.receive_json()
            messages.append(msg)
            if msg["type"] == "result":
                break

    heartbeats = [m for m in messages if m["type"] == "heartbeat"]
    assert len(heartbeats) >= 1, f"expected at least 1 heartbeat, got messages={messages}"
    # elapsed_ms field is monotonically non-decreasing across heartbeats.
    elapsed = [h["elapsed_ms"] for h in heartbeats]
    assert elapsed == sorted(elapsed)
    assert all(e >= 0 for e in elapsed)



def test_stream_no_heartbeat_during_active_streaming(session_http_client_factory):
    """When the kernel is emitting chunks faster than the heartbeat interval,
    no heartbeat fires (the reset event keeps restarting the silence timer)."""
    client = session_http_client_factory(stream_heartbeat_seconds=0.5)
    sid = client.post("/sessions").json()["session_id"]

    messages: list[dict] = []
    with client.websocket_connect(f"/sessions/{sid}/execute/stream") as ws:
        # 10 prints over ~1s, each well under the 0.5s heartbeat interval.
        ws.send_text(
            '{"code": "import time\\nfor i in range(10):\\n    print(i); time.sleep(0.1)"}'
        )
        while True:
            msg = ws.receive_json()
            messages.append(msg)
            if msg["type"] == "result":
                break

    heartbeats = [m for m in messages if m["type"] == "heartbeat"]
    # Allow at most 1 to account for kernel startup latency racing the first
    # heartbeat tick; without the reset mechanism we'd expect ~2.
    assert len(heartbeats) <= 1, f"expected 0-1 heartbeats during active streaming, got {len(heartbeats)}: {messages}"


def test_stream_heartbeat_disabled_when_seconds_zero(session_http_client_factory):
    """stream_heartbeat_seconds=0 disables heartbeats entirely."""
    client = session_http_client_factory(stream_heartbeat_seconds=0.0)
    sid = client.post("/sessions").json()["session_id"]

    messages: list[dict] = []
    with client.websocket_connect(f"/sessions/{sid}/execute/stream") as ws:
        ws.send_text('{"code": "import time; time.sleep(2); print(\\"done\\")"}')
        while True:
            msg = ws.receive_json()
            messages.append(msg)
            if msg["type"] == "result":
                break

    heartbeats = [m for m in messages if m["type"] == "heartbeat"]
    assert heartbeats == [], f"expected 0 heartbeats with stream_heartbeat_seconds=0, got {heartbeats}"