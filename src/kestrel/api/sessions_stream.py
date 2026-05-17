"""Phase 6 substep 4: WebSocket route for streaming session executes.

Substep 3 locked the route shape, auth handshake, close-code semantics,
and the single-execute-per-connection lifecycle with a stub that sent
one empty result message. Substep 4 wires the real streaming runtime —
parses the inbound execute request, acquires the session lock via the
registry's acquire_for_execute context manager, iterates the runtime's
execute_stream async generator, sends each message over the WebSocket.

Why this lives in its own file (and not ``sessions.py``):
- WebSocket handlers have a different shape (no ``response_model``, no
``Depends(require_api_key)`` router-level dependency, ``WebSocket`` parameter)
- The streaming runtime client and the back-pressure / disconnect logic
here amount to ~100 lines of behaviour distinct from HTTP request/response
- Clean separation of HTTP request/response vs WebSocket lifecycle
"""
from __future__ import annotations

import asyncio
import json
import secrets

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from kestrel.api.schemas import ExecuteRequest, StreamError
from kestrel.config import Settings, get_settings
from kestrel.execution.session_registry import (
    SessionBusy,
    SessionNotFound,
    SessionRegistry,
)
from kestrel.execution.session_runtime import (
    SessionProtocolError,
    SessionTerminated,
    SessionTimeout,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/sessions", tags=["sessions-stream"])

# Substep 4 module constant; substep 5 promotes to Settings.stream_backpressure_timeout_seconds.
# Per design lock 6-backpressure: 30s cap on consecutive back-pressured send
# attempts before the safety disconnect closes the WebSocket.
_STREAM_BACKPRESSURE_TIMEOUT_SECONDS = 30.0


def get_session_registry(websocket: WebSocket) -> SessionRegistry:
    """DI provider — pulls the registry attached to ``app.state`` in the lifespan.

    WebSocket variant of the HTTP provider in ``sessions.py``. FastAPI's
    dependency-injection system fills the parameter based on its type
    annotation; the HTTP one takes ``Request``, this one takes ``WebSocket``,
    each is fed by FastAPI in its respective handler context.
    """
    return websocket.app.state.registry


def _extract_token(websocket: WebSocket) -> str | None:
    """Get bearer token from Authorization header OR ``token`` query param.

    HTTP-style ``Authorization: Bearer <token>`` is the preferred channel —
    works from curl, the Python ``websockets`` library, and most non-browser
    clients. Browsers can't set custom headers on the JavaScript ``WebSocket``
    API, so we accept ``?token=...`` as a fallback. Both are documented;
    clients pick whichever fits their environment.
    """
    auth_header = websocket.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header[7:]
    return websocket.query_params.get("token")


def _auth_ok(provided: str | None, expected: str) -> bool:
    """Constant-time bearer comparison. Empty expected = auth disabled."""
    if expected == "":
        return True
    if not provided:
        return False
    return secrets.compare_digest(provided, expected)


async def _safe_close(websocket: WebSocket, code: int, reason: str) -> None:
    """Close the WebSocket; swallow errors if it's already closed.

    Several code paths can race with each other (timeout firing while the
    client is also disconnecting, etc.). Calling close() on a closed
    WebSocket raises; this helper makes the call idempotent.
    """
    try:
        await websocket.close(code=code, reason=reason)
    except (RuntimeError, WebSocketDisconnect):
        pass


@router.websocket("/{session_id}/execute/stream")
async def execute_in_session_stream(
    websocket: WebSocket,
    session_id: str,
    registry: SessionRegistry = Depends(get_session_registry),
    settings: Settings = Depends(get_settings),
) -> None:
    """Phase 6 substep 4: streaming execute over WebSocket.

    Lifecycle:
    1. Validate auth (header bearer or ?token query param).
    2. Validate session_id exists in the registry.
    3. Accept the WebSocket upgrade.
    4. Receive one execute-request message (JSON {"code": "..."}).
    5. Acquire the per-session execute lock + runtime.
    6. Iterate runtime.execute_stream(code); send each StreamMessage on the wire.
    7. Close with 1000 on normal completion (StreamResult sent).

    Close codes:
    - 1000 normal completion
    - 1011 server error (backpressure timeout, protocol error, bad request)
    - 4401 auth failed (handshake)
    - 4404 session not found (handshake or race during acquire)
    - 4409 session busy (another execute in progress)
    - 4410 session terminated (kernel dead — SessionTerminated or SessionTimeout)
    """
    token = _extract_token(websocket)
    if not _auth_ok(token, settings.dev_api_key):
        await _safe_close(websocket, 4401, "auth_failed")
        return

    try:
        await registry.get_info(session_id)
    except SessionNotFound:
        await _safe_close(websocket, 4404, "session_not_found")
        return

    await websocket.accept()
    logger.info("session_stream_accepted", session_id_prefix=session_id[:8])

    try:
        request_text = await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info(
            "session_stream_client_disconnect_before_request",
            session_id_prefix=session_id[:8],
        )
        return

    try:
        payload = json.loads(request_text)
        execute_request = ExecuteRequest(**payload)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        await websocket.send_json(
            StreamError(code="bad_request", detail=f"invalid execute request: {exc}").model_dump()
        )
        await _safe_close(websocket, 1011, "bad_request")
        return

    # Background task that blocks on receive() and completes on client disconnect.
    # starlette's send_*() doesn't reliably raise WebSocketDisconnect when the
    # client has closed (the close frame may not have been processed yet at the
    # moment of send). receive() DOES raise WebSocketDisconnect cleanly. We race
    # this against each send with asyncio.wait(FIRST_COMPLETED) to honour
    # design lock 6-disconnect: kill the kernel as soon as the client is gone.
    async def _wait_for_disconnect() -> None:
        # receive_text() raises WebSocketDisconnect cleanly when the client
        # closes; the low-level receive() returns the websocket.disconnect
        # message and then errors on the NEXT call with RuntimeError, which
        # would escape the try/except. Substep 4 contract is one execute
        # request per connection, so any mid-execute client text is undefined
        # behavior — silently swallowing it is acceptable.
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            return

    disconnect_task = asyncio.create_task(_wait_for_disconnect())

    try:
        try:
            async with registry.acquire_for_execute(session_id) as runtime:
                try:
                    async for message in runtime.execute_stream(execute_request.code):
                        if disconnect_task.done():
                            # Client disconnected between iterations.
                            logger.info(
                                "session_stream_client_disconnect_mid_execute",
                                session_id_prefix=session_id[:8],
                            )
                            await runtime.close()
                            return

                        send_task = asyncio.create_task(
                            websocket.send_json(message.model_dump(mode="json"))
                        )
                        done, _pending = await asyncio.wait(
                            [send_task, disconnect_task],
                            timeout=_STREAM_BACKPRESSURE_TIMEOUT_SECONDS,
                            return_when=asyncio.FIRST_COMPLETED,
                        )

                        if not done:
                            # Back-pressure safety: neither send nor disconnect completed in window.
                            send_task.cancel()
                            logger.warning(
                                "session_stream_backpressure_timeout",
                                session_id_prefix=session_id[:8],
                                timeout_seconds=_STREAM_BACKPRESSURE_TIMEOUT_SECONDS,
                            )
                            await runtime.close()
                            await _safe_close(websocket, 1011, "backpressure_timeout")
                            return

                        if disconnect_task in done:
                            send_task.cancel()
                            logger.info(
                                "session_stream_client_disconnect_mid_execute",
                                session_id_prefix=session_id[:8],
                            )
                            await runtime.close()
                            return

                        # send_task completed — propagate any exception it raised.
                        send_task.result()
                except SessionTimeout:
                    logger.info(
                        "session_stream_timeout",
                        session_id_prefix=session_id[:8],
                    )
                    await _safe_close(websocket, 4410, "session_timeout")
                    return
                except SessionTerminated:
                    await _safe_close(websocket, 4410, "session_terminated")
                    return
                except SessionProtocolError:
                    logger.exception(
                        "session_stream_protocol_error",
                        session_id_prefix=session_id[:8],
                    )
                    await _safe_close(websocket, 1011, "protocol_error")
                    return
        except SessionBusy:
            await websocket.send_json(
                StreamError(code="session_busy", detail="another execute is in progress").model_dump()
            )
            await _safe_close(websocket, 4409, "session_busy")
            return
        except SessionNotFound:
            # Race: session was deleted between the handshake check and acquire.
            await _safe_close(websocket, 4404, "session_not_found")
            return
    finally:
        if not disconnect_task.done():
            disconnect_task.cancel()
        try:
            await disconnect_task
        except (asyncio.CancelledError, WebSocketDisconnect):
            pass

    await _safe_close(websocket, 1000, "")
    logger.info("session_stream_closed", session_id_prefix=session_id[:8], code=1000)