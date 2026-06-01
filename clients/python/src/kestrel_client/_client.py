"""Synchronous Kestrel client."""

from __future__ import annotations

from typing import Iterator, Optional

import httpx

from kestrel_client._exceptions import (
    AuthenticationError,
    KestrelAPIError,
    RateLimitedError,
    SessionBusyError,
    SessionGoneError,
    SessionNotFoundError,
)
from kestrel_client._models import (
    ExecuteResult,
    PollingRead,
    Session,
    SessionExecuteResult,
    StreamMessage,
)

DEFAULT_TIMEOUT = 30.0


class KestrelClient:
    """Synchronous client for the Kestrel API.

    Usage::

        with KestrelClient("http://localhost:8000", api_key="kestrel_...") as k:
            print(k.execute("print(2+2)").stdout)
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.Client(
            base_url=self._base_url, headers=headers, timeout=timeout, transport=transport
        )

    def __enter__(self) -> "KestrelClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # ── internals ──

    def _request(self, method: str, path: str, *, json=None, params=None) -> httpx.Response:
        response = self._http.request(method, path, json=json, params=params)
        _raise_for_status(response)
        return response

    # ── stateless execute ──

    def execute(self, code: str) -> ExecuteResult:
        response = self._request("POST", "/execute", json={"code": code})
        return ExecuteResult.from_dict(response.json())

    # ── sessions ──

    def create_session(self) -> Session:
        response = self._request("POST", "/sessions")
        return Session.from_dict(response.json())

    def list_sessions(self) -> list:
        response = self._request("GET", "/sessions")
        return [Session.from_dict(s) for s in response.json().get("sessions", [])]

    def get_session(self, session_id: str) -> Session:
        response = self._request("GET", f"/sessions/{session_id}")
        return Session.from_dict(response.json())

    def delete_session(self, session_id: str) -> None:
        self._request("DELETE", f"/sessions/{session_id}")

    def session_execute(self, session_id: str, code: str) -> SessionExecuteResult:
        response = self._request("POST", f"/sessions/{session_id}/execute", json={"code": code})
        return SessionExecuteResult.from_dict(response.json())

    # ── polling primitives ──

    def start_polling(self, session_id: str, code: str) -> str:
        response = self._request(
            "POST", f"/sessions/{session_id}/execute/polling", json={"code": code}
        )
        return response.json()["execution_id"]

    def read_execution(
        self, session_id: str, execution_id: str, *, since: int = 0, wait: float = 0.0
    ) -> PollingRead:
        response = self._request(
            "GET",
            f"/sessions/{session_id}/executions/{execution_id}",
            params={"since": since, "wait": wait},
        )
        return PollingRead.from_dict(response.json())

    # ── streaming (via the polling fallback — decision 8-sdk-stream-transport) ──

    def stream(self, session_id: str, code: str, *, wait: float = 2.0) -> Iterator[StreamMessage]:
        execution_id = self.start_polling(session_id, code)
        cursor = 0
        while True:
            read = self.read_execution(session_id, execution_id, since=cursor, wait=wait)
            for message in read.messages:
                yield message
            cursor = read.next_cursor
            if read.done:
                return


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = body.get("detail", "") or ""
    except Exception:
        detail = response.text
    code = response.status_code
    if code == 401:
        raise AuthenticationError(detail or "authentication failed")
    if code == 404:
        raise SessionNotFoundError(detail or "not found")
    if code == 409:
        raise SessionBusyError(detail or "session busy")
    if code == 410:
        raise SessionGoneError(detail or "session gone")
    if code == 429:
        retry_after = response.headers.get("Retry-After")
        raise RateLimitedError(int(retry_after) if retry_after and retry_after.isdigit() else None)
    raise KestrelAPIError(code, detail)
