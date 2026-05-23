"""Phase 7 substep 1 (7-reqid-stream): request_id surfacing on terminal frames."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_polling_read_response_carries_request_id(session_http_client: TestClient) -> None:
    """PollingReadResponse.request_id should be a non-empty UUID — the GET request's id."""
    sid = session_http_client.post("/sessions").json()["session_id"]
    eid = session_http_client.post(
        f"/sessions/{sid}/execute/polling",
        json={"code": "print('hi')"},
    ).json()["execution_id"]

    body = session_http_client.get(
        f"/sessions/{sid}/executions/{eid}",
        params={"since": 0, "wait": 5},
    ).json()

    assert "request_id" in body
    assert body["request_id"] != ""
    # The X-Request-ID response header echoes the same id the middleware bound.
    # (middleware already echoes via response headers — see app.py)


def test_polling_read_response_reflects_x_request_id_header(session_http_client: TestClient) -> None:
    """If the GET supplies X-Request-ID, the response should carry it back."""
    sid = session_http_client.post("/sessions").json()["session_id"]
    eid = session_http_client.post(
        f"/sessions/{sid}/execute/polling",
        json={"code": "print('hi')"},
    ).json()["execution_id"]

    custom = "test-rid-abc123"
    resp = session_http_client.get(
        f"/sessions/{sid}/executions/{eid}",
        params={"since": 0, "wait": 5},
        headers={"X-Request-ID": custom},
    )
    body = resp.json()
    assert body["request_id"] == custom
    assert resp.headers.get("x-request-id") == custom


def test_polling_terminal_frames_carry_post_request_id(session_http_client: TestClient) -> None:
    """Terminal StreamResult / StreamError frames in the buffer should carry the POST's request_id, not the GET's."""
    custom_post_rid = "test-post-rid-xyz789"
    sid = session_http_client.post("/sessions").json()["session_id"]
    eid = session_http_client.post(
        f"/sessions/{sid}/execute/polling",
        json={"code": "print('hi')"},
        headers={"X-Request-ID": custom_post_rid},
    ).json()["execution_id"]

    # Drain the buffer — one GET only returns the first batch (the stdout chunk);
    # the result frame may not have arrived yet.
    collected: list[dict] = []
    cursor = 0
    last_body: dict = {}
    for _ in range(20):
        last_body = session_http_client.get(
            f"/sessions/{sid}/executions/{eid}",
            params={"since": cursor, "wait": 5},
        ).json()
        collected.extend(last_body["messages"])
        cursor = last_body["next_cursor"]
        if last_body["done"]:
            break
    else:
        raise AssertionError("polling did not reach done within 20 GETs")

    # The result message exists and carries the POST's request_id.
    result_msgs = [m for m in collected if m["type"] == "result"]
    assert result_msgs, "expected a result message in the drained polling output"
    assert result_msgs[0]["request_id"] == custom_post_rid

    # Top-level request_id of the FINAL GET is the GET's, NOT the POST's.
    assert last_body["request_id"] != custom_post_rid


def test_websocket_result_frame_carries_request_id(session_http_client: TestClient) -> None:
    """The result frame sent over the WebSocket stream should carry a non-empty request_id."""
    sid = session_http_client.post("/sessions").json()["session_id"]
    with session_http_client.websocket_connect(f"/sessions/{sid}/execute/stream") as ws:
        ws.send_json({"code": "print('hello stream')"})
        messages: list[dict] = []
        while True:
            msg = ws.receive_json()
            messages.append(msg)
            if msg["type"] == "result":
                break

    result_msg = next(m for m in messages if m["type"] == "result")
    assert "request_id" in result_msg
    assert result_msg["request_id"] != ""


def test_websocket_result_frame_reflects_header_request_id(session_http_client: TestClient) -> None:
    """If the WebSocket upgrade carries X-Request-ID, the terminal frames should echo it."""
    custom = "test-ws-rid-456"
    sid = session_http_client.post("/sessions").json()["session_id"]
    with session_http_client.websocket_connect(
        f"/sessions/{sid}/execute/stream",
        headers={"X-Request-ID": custom},
    ) as ws:
        ws.send_json({"code": "print('hi')"})
        while True:
            msg = ws.receive_json()
            if msg["type"] == "result":
                assert msg["request_id"] == custom
                break