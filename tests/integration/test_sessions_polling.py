from __future__ import annotations

import threading
import time


def _drain(client, session_id, execution_id, *, wait=5.0, max_polls=60):
    """Long-poll an execution to completion, returning every message in order.

    Mirrors a well-behaved polling client: GET with ?since=<cursor>, advance
    the cursor by the returned next_cursor, stop when done is True.
    """
    messages: list[dict] = []
    cursor = 0
    for _ in range(max_polls):
        resp = client.get(
            f"/sessions/{session_id}/executions/{execution_id}",
            params={"since": cursor, "wait": wait},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["next_cursor"] >= cursor  # cursor never goes backwards
        messages.extend(body["messages"])
        cursor = body["next_cursor"]
        if body["done"]:
            return messages
    raise AssertionError("polling did not reach done within max_polls")


def test_polling_post_returns_202_with_execution_id(session_http_client):
    sid = session_http_client.post("/sessions").json()["session_id"]

    response = session_http_client.post(
        f"/sessions/{sid}/execute/polling",
        json={"code": "print('hello')"},
    )

    assert response.status_code == 202
    body = response.json()
    assert "execution_id" in body
    assert len(body["execution_id"]) == 32  # uuid4().hex


def test_polling_streams_stdout_and_reaches_done(session_http_client):
    """The drained stream ends with a result message and carries stdout."""
    sid = session_http_client.post("/sessions").json()["session_id"]
    eid = session_http_client.post(
        f"/sessions/{sid}/execute/polling",
        json={"code": "print('hello polling')"},
    ).json()["execution_id"]

    messages = _drain(session_http_client, sid, eid)

    stdout = "".join(m["data"] for m in messages if m["type"] == "stdout")
    assert "hello polling" in stdout

    results = [m for m in messages if m["type"] == "result"]
    assert len(results) == 1
    assert results[0]["exit_code"] == 0


def test_polling_result_message_carries_full_response(session_http_client):
    """The result message is a full SessionExecuteResponse — exit_code,
    stdout, and the rich-output fields are all present."""
    sid = session_http_client.post("/sessions").json()["session_id"]
    eid = session_http_client.post(
        f"/sessions/{sid}/execute/polling",
        json={"code": "print(2 + 2)"},
    ).json()["execution_id"]

    messages = _drain(session_http_client, sid, eid)
    result = next(m for m in messages if m["type"] == "result")

    assert result["exit_code"] == 0
    assert "stdout" in result
    assert "outputs" in result
    assert "dropped_outputs" in result


def test_polling_cursor_is_consistent(session_http_client):
    """next_cursor equals the total messages delivered; re-reading past the
    end yields nothing and stays done."""
    sid = session_http_client.post("/sessions").json()["session_id"]
    eid = session_http_client.post(
        f"/sessions/{sid}/execute/polling",
        json={"code": "print('a'); print('b')"},
    ).json()["execution_id"]

    messages = _drain(session_http_client, sid, eid)

    final = session_http_client.get(
        f"/sessions/{sid}/executions/{eid}",
        params={"since": len(messages), "wait": 0},
    ).json()
    assert final["messages"] == []
    assert final["next_cursor"] == len(messages)
    assert final["done"] is True


def test_polling_short_poll_mode_completes(session_http_client):
    """wait=0 short-polling (Decision 6.6-mech) drains an execute just as
    long-polling does, one immediate-return GET at a time."""
    sid = session_http_client.post("/sessions").json()["session_id"]
    eid = session_http_client.post(
        f"/sessions/{sid}/execute/polling",
        json={"code": "print('short-poll')"},
    ).json()["execution_id"]

    collected: list[dict] = []
    cursor = 0
    for _ in range(100):
        body = session_http_client.get(
            f"/sessions/{sid}/executions/{eid}",
            params={"since": cursor, "wait": 0},
        ).json()
        collected.extend(body["messages"])
        cursor = body["next_cursor"]
        if body["done"]:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("short-poll did not complete")

    assert any(m["type"] == "result" for m in collected)


def test_polling_get_unknown_execution_returns_404(session_http_client):
    sid = session_http_client.post("/sessions").json()["session_id"]
    fake_eid = "0" * 32

    response = session_http_client.get(f"/sessions/{sid}/executions/{fake_eid}")
    assert response.status_code == 404


def test_polling_get_unknown_session_returns_404(session_http_client):
    fake = "0" * 32
    response = session_http_client.get(f"/sessions/{fake}/executions/{fake}")
    assert response.status_code == 404


def test_polling_post_unknown_session_returns_404(session_http_client):
    fake = "0" * 32
    response = session_http_client.post(
        f"/sessions/{fake}/execute/polling",
        json={"code": "print(1)"},
    )
    assert response.status_code == 404


def test_polling_busy_session_surfaces_session_busy_error(session_http_client):
    """A polling execute against a session already running one converts the
    SessionBusy into a single terminal error message (Decision 6.6-wrap)."""
    sid = session_http_client.post("/sessions").json()["session_id"]

    holder: dict[str, object] = {}

    def slow() -> None:
        holder["resp"] = session_http_client.post(
            f"/sessions/{sid}/execute",
            json={"code": "import time; time.sleep(3)"},
        )

    t = threading.Thread(target=slow)
    t.start()
    time.sleep(1.0)  # let the slow execute acquire the session lock

    eid = session_http_client.post(
        f"/sessions/{sid}/execute/polling",
        json={"code": "print('blocked')"},
    ).json()["execution_id"]
    messages = _drain(session_http_client, sid, eid)
    t.join(timeout=15)

    assert len(messages) == 1
    assert messages[0]["type"] == "error"
    assert messages[0]["code"] == "session_busy"


def test_polling_timeout_surfaces_session_timeout_error(session_http_client):
    """An execute that exceeds the timeout converts SessionTimeout into a
    single terminal error message in the buffer (default 5s timeout)."""
    sid = session_http_client.post("/sessions").json()["session_id"]
    eid = session_http_client.post(
        f"/sessions/{sid}/execute/polling",
        json={"code": "while True:\n    pass"},
    ).json()["execution_id"]

    messages = _drain(session_http_client, sid, eid)

    assert len(messages) == 1
    assert messages[0]["type"] == "error"
    assert messages[0]["code"] == "session_timeout"


def test_polling_delete_session_evicts_buffer(session_http_client):
    """Deleting the session drops its polling buffers — Decision 6.6-buffer."""
    sid = session_http_client.post("/sessions").json()["session_id"]
    eid = session_http_client.post(
        f"/sessions/{sid}/execute/polling",
        json={"code": "print('hi')"},
    ).json()["execution_id"]
    _drain(session_http_client, sid, eid)

    assert session_http_client.delete(f"/sessions/{sid}").status_code == 204

    # buffer is gone with the session
    assert (
        session_http_client.get(f"/sessions/{sid}/executions/{eid}").status_code
        == 404
    )


def test_polling_routes_require_auth_when_key_set(session_http_client_authed):
    """Bearer auth gates both polling routes (router-level dependency)."""
    client = session_http_client_authed
    fake = "0" * 32

    assert (
        client.post(
            f"/sessions/{fake}/execute/polling", json={"code": "print(1)"}
        ).status_code
        == 401
    )
    assert client.get(f"/sessions/{fake}/executions/{fake}").status_code == 401

    headers = {"Authorization": "Bearer test-secret-12345"}
    # auth passes -> handler runs -> 404 for the unknown session/execution
    assert (
        client.post(
            f"/sessions/{fake}/execute/polling",
            json={"code": "print(1)"},
            headers=headers,
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/sessions/{fake}/executions/{fake}", headers=headers
        ).status_code
        == 404
    )