from __future__ import annotations

import httpx
import pytest

from kestrel_client import (
    AuthenticationError,
    DataFrameOutput,
    KestrelAPIError,
    RateLimitedError,
    ResultMessage,
    SessionBusyError,
    SessionGoneError,
    SessionNotFoundError,
    StdoutChunk,
)
from conftest import make_client

_EXECUTE_BODY = {
    "stdout": "4\n",
    "stderr": "",
    "exit_code": 0,
    "duration_ms": 12,
    "timed_out": False,
    "stdout_truncated": False,
    "stderr_truncated": False,
}


def test_execute_builds_request_and_parses_response() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.read().decode()
        return httpx.Response(200, json=_EXECUTE_BODY)

    with make_client(handler) as client:
        result = client.execute("print(2 + 2)")

    assert seen["method"] == "POST"
    assert seen["path"] == "/execute"
    assert '"code"' in seen["body"]
    assert result.stdout == "4\n"
    assert result.exit_code == 0
    assert result.timed_out is False


def test_execute_sends_bearer_header_when_api_key_set() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_EXECUTE_BODY)

    with make_client(handler, api_key="kestrel_abc") as client:
        client.execute("print(1)")

    assert seen["auth"] == "Bearer kestrel_abc"


def test_timeout_is_data_not_exception() -> None:
    body = {**_EXECUTE_BODY, "timed_out": True, "exit_code": -1, "stdout": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with make_client(handler) as client:
        result = client.execute("while True: pass")

    assert result.timed_out is True
    assert result.exit_code == -1


def test_create_and_list_and_get_and_delete_session() -> None:
    session_json = {
        "session_id": "abc123",
        "created_at": "2026-06-01T08:00:00+00:00",
        "last_used": "2026-06-01T08:00:01+00:00",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/sessions" and method == "POST":
            return httpx.Response(201, json=session_json)
        if path == "/sessions" and method == "GET":
            return httpx.Response(200, json={"sessions": [session_json]})
        if path == "/sessions/abc123" and method == "GET":
            return httpx.Response(200, json=session_json)
        if path == "/sessions/abc123" and method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(500)

    with make_client(handler) as client:
        created = client.create_session()
        assert created.session_id == "abc123"
        assert created.created_at.year == 2026

        listed = client.list_sessions()
        assert len(listed) == 1 and listed[0].session_id == "abc123"

        got = client.get_session("abc123")
        assert got.session_id == "abc123"

        client.delete_session("abc123")  # no raise = success


def test_session_execute_parses_rich_outputs() -> None:
    body = {
        **_EXECUTE_BODY,
        "outputs": [
            {
                "type": "dataframe",
                "mime_type": "application/json",
                "data": {"index": [0], "columns": ["a"], "data": [[1]]},
                "shape": [1, 1],
            }
        ],
        "dropped_outputs": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sessions/s1/execute"
        return httpx.Response(200, json=body)

    with make_client(handler) as client:
        result = client.session_execute("s1", "df")

    assert len(result.outputs) == 1
    assert isinstance(result.outputs[0], DataFrameOutput)
    assert result.outputs[0].shape == (1, 1)


@pytest.mark.parametrize(
    "status,exc",
    [
        (401, AuthenticationError),
        (404, SessionNotFoundError),
        (409, SessionBusyError),
        (410, SessionGoneError),
        (500, KestrelAPIError),
    ],
)
def test_http_errors_map_to_typed_exceptions(status: int, exc: type) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "boom"})

    with make_client(handler) as client:
        with pytest.raises(exc):
            client.execute("print(1)")


def test_rate_limited_carries_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "slow down"}, headers={"Retry-After": "7"})

    with make_client(handler) as client:
        with pytest.raises(RateLimitedError) as excinfo:
            client.execute("print(1)")

    assert excinfo.value.retry_after == 7


def test_sync_stream_yields_messages_via_polling() -> None:
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/execute/polling"):
            return httpx.Response(200, json={"execution_id": "e1"})
        # GET read: first poll returns a chunk (not done), second returns result (done)
        polls["n"] += 1
        if polls["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "messages": [{"type": "stdout", "data": "hello"}],
                    "next_cursor": 1,
                    "done": False,
                    "request_id": "r1",
                },
            )
        return httpx.Response(
            200,
            json={
                "messages": [
                    {
                        "type": "result",
                        **_EXECUTE_BODY,
                        "outputs": [],
                        "dropped_outputs": [],
                        "request_id": "r1",
                    }
                ],
                "next_cursor": 2,
                "done": True,
                "request_id": "r1",
            },
        )

    with make_client(handler) as client:
        messages = list(client.stream("s1", "print('hello')"))

    assert isinstance(messages[0], StdoutChunk)
    assert messages[0].data == "hello"
    assert isinstance(messages[-1], ResultMessage)
    assert messages[-1].result.stdout == "4\n"
