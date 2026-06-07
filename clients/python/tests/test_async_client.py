from __future__ import annotations

import json

import httpx
import pytest

from kestrel_client import (
    AuthenticationError,
    DataFrameOutput,
    KestrelAPIError,
    ResultMessage,
    SessionBusyError,
    SessionGoneError,
    SessionNotFoundError,
    StdoutChunk,
)
from conftest import make_async_client

_EXECUTE_BODY = {
    "stdout": "4\n",
    "stderr": "",
    "exit_code": 0,
    "duration_ms": 12,
    "timed_out": False,
    "stdout_truncated": False,
    "stderr_truncated": False,
}


async def test_async_execute_builds_request_and_parses_response() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json=_EXECUTE_BODY)

    async with make_async_client(handler) as client:
        result = await client.execute("print(2 + 2)")

    assert seen["method"] == "POST"
    assert seen["path"] == "/execute"
    assert result.stdout == "4\n"


async def test_async_sends_bearer_header() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_EXECUTE_BODY)

    async with make_async_client(handler, api_key="kestrel_abc") as client:
        await client.execute("print(1)")

    assert seen["auth"] == "Bearer kestrel_abc"


async def test_async_session_execute_parses_rich_outputs() -> None:
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

    async with make_async_client(handler) as client:
        result = await client.session_execute("s1", "df")

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
async def test_async_http_errors_map_to_typed_exceptions(status: int, exc: type) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "boom"})

    async with make_async_client(handler) as client:
        with pytest.raises(exc):
            await client.execute("print(1)")


@pytest.mark.parametrize(
    "code,exc",
    [
        (4401, AuthenticationError),
        (4404, SessionNotFoundError),
        (4409, SessionBusyError),
        (4410, SessionGoneError),
        (1011, KestrelAPIError),
    ],
)
def test_ws_close_code_maps_to_exception(code: int, exc: type) -> None:
    from kestrel_client._async_client import _raise_for_ws_close

    with pytest.raises(exc):
        _raise_for_ws_close(code, "")


async def test_async_stream_yields_messages_over_websocket(monkeypatch) -> None:
    import kestrel_client._async_client as ac

    frames = [
        json.dumps({"type": "stdout", "data": "hi"}),
        json.dumps(
            {
                "type": "result",
                **_EXECUTE_BODY,
                "outputs": [],
                "dropped_outputs": [],
                "request_id": "r1",
            }
        ),
    ]

    class FakeWS:
        def __init__(self) -> None:
            self.sent = []

        async def send(self, data: str) -> None:
            self.sent.append(data)

        def __aiter__(self):
            self._it = iter(frames)
            return self

        async def __anext__(self) -> str:
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration

    fake_ws = FakeWS()

    class FakeConnect:
        async def __aenter__(self):
            return fake_ws

        async def __aexit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(ac, "ws_connect", lambda *a, **k: FakeConnect())

    client = ac.AsyncKestrelClient("http://test", api_key="k")
    messages = [m async for m in client.stream("s1", "print('hi')")]
    await client.aclose()

    assert fake_ws.sent == [json.dumps({"code": "print('hi')"})]
    assert isinstance(messages[0], StdoutChunk)
    assert messages[0].data == "hi"
    assert isinstance(messages[-1], ResultMessage)


async def test_async_execute_includes_timeout_seconds_when_set() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read().decode()
        return httpx.Response(200, json=_EXECUTE_BODY)

    async with make_async_client(handler) as client:
        await client.execute("print(1)", timeout_seconds=2.5)

    assert '"timeout_seconds"' in seen["body"]
    assert "2.5" in seen["body"]


async def test_async_execute_omits_timeout_seconds_when_unset() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read().decode()
        return httpx.Response(200, json=_EXECUTE_BODY)

    async with make_async_client(handler) as client:
        await client.execute("print(1)")

    assert "timeout_seconds" not in seen["body"]