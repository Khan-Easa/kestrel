"""Asynchronous Kestrel client."""

from __future__ import annotations

import json
from typing import AsyncIterator, Optional

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosedError, InvalidStatus

from kestrel_client._client import DEFAULT_TIMEOUT, _raise_for_status
from kestrel_client._exceptions import (
    AuthenticationError,
    KestrelAPIError,
    SessionBusyError,
    SessionGoneError,
    SessionNotFoundError,
)
from kestrel_client._models import (
    ErrorMessage,
    ExecuteResult,
    PollingRead,
    ResultMessage,
    Session,
    SessionExecuteResult,
    StreamMessage,
    parse_stream_message,
)


class AsyncKestrelClient:
    """Asynchronous client for the Kestrel API.

    Usage::

        async with AsyncKestrelClient("http://localhost:8000", api_key="kestrel_...") as k:
            result = await k.execute("print(2+2)")
            async for message in k.stream(session_id, "for i in range(3): print(i)"):
                ...
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.AsyncClient(
            base_url=self._base_url, headers=headers, timeout=timeout, transport=transport
        )

    async def __aenter__(self) -> "AsyncKestrelClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── internals ──

    async def _request(self, method: str, path: str, *, json=None, params=None) -> httpx.Response:
        response = await self._http.request(method, path, json=json, params=params)
        _raise_for_status(response)
        return response

    # ── stateless execute ──

    async def execute(self, code: str, *, timeout_seconds: Optional[float] = None) -> ExecuteResult:
        body: dict = {"code": code}
        if timeout_seconds is not None:
            body["timeout_seconds"] = timeout_seconds
        response = await self._request("POST", "/execute", json=body)
        return ExecuteResult.from_dict(response.json())

    # ── sessions ──

    async def create_session(self) -> Session:
        response = await self._request("POST", "/sessions")
        return Session.from_dict(response.json())

    async def list_sessions(self) -> list:
        response = await self._request("GET", "/sessions")
        return [Session.from_dict(s) for s in response.json().get("sessions", [])]

    async def get_session(self, session_id: str) -> Session:
        response = await self._request("GET", f"/sessions/{session_id}")
        return Session.from_dict(response.json())

    async def delete_session(self, session_id: str) -> None:
        await self._request("DELETE", f"/sessions/{session_id}")

    async def session_execute(self, session_id: str, code: str) -> SessionExecuteResult:
        response = await self._request(
            "POST", f"/sessions/{session_id}/execute", json={"code": code}
        )
        return SessionExecuteResult.from_dict(response.json())

    # ── polling primitives ──

    async def start_polling(self, session_id: str, code: str) -> str:
        response = await self._request(
            "POST", f"/sessions/{session_id}/execute/polling", json={"code": code}
        )
        return response.json()["execution_id"]

    async def read_execution(
        self, session_id: str, execution_id: str, *, since: int = 0, wait: float = 0.0
    ) -> PollingRead:
        response = await self._request(
            "GET",
            f"/sessions/{session_id}/executions/{execution_id}",
            params={"since": since, "wait": wait},
        )
        return PollingRead.from_dict(response.json())

    # ── streaming (real WebSocket — decision 8-sdk-stream-transport) ──

    async def stream(self, session_id: str, code: str) -> AsyncIterator[StreamMessage]:
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with ws_connect(
                self._ws_url(session_id), additional_headers=headers
            ) as websocket:
                await websocket.send(json.dumps({"code": code}))
                async for raw in websocket:
                    message = parse_stream_message(json.loads(raw))
                    yield message
                    if isinstance(message, (ResultMessage, ErrorMessage)):
                        return
        except ConnectionClosedError as exc:
            _raise_for_ws_close(_close_code(exc), getattr(exc, "reason", "") or "")
        except InvalidStatus as exc:
            _raise_for_ws_handshake(exc)

    def _ws_url(self, session_id: str) -> str:
        base = self._base_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return f"{base}/sessions/{session_id}/execute/stream"


def _close_code(exc: ConnectionClosedError) -> Optional[int]:
    code = getattr(exc, "code", None)
    if code is not None:
        return code
    received = getattr(exc, "rcvd", None)
    return getattr(received, "code", None) if received is not None else None


def _raise_for_ws_close(code: Optional[int], reason: str = "") -> None:
    if code == 4401:
        raise AuthenticationError(reason or "auth failed")
    if code == 4404:
        raise SessionNotFoundError(reason or "session not found")
    if code == 4409:
        raise SessionBusyError(reason or "session busy")
    if code == 4410:
        raise SessionGoneError(reason or "session gone")
    raise KestrelAPIError(code or 500, reason or "stream closed unexpectedly")


def _raise_for_ws_handshake(exc: InvalidStatus) -> None:
    # Pre-accept WS rejections (auth, session-not-found) surface as a failed
    # handshake (HTTP status) rather than a WS close frame, because the server
    # closes before accepting. Map the HTTP status best-effort.
    status = None
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    if status in (401, 403):
        raise AuthenticationError("auth failed")
    if status == 404:
        raise SessionNotFoundError("session not found")
    raise KestrelAPIError(status or 500, "websocket handshake rejected")