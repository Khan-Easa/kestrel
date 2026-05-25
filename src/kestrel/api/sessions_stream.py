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
import time
import uuid

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from kestrel.api.schemas import ExecuteRequest, StreamError, StreamHeartbeat, StreamResult
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
from kestrel.observability import EXECUTIONS, EXECUTION_DURATION, STREAM_ACTIVE
from kestrel.audit import AuditEvent, AuditSink


logger = structlog.get_logger()

router = APIRouter(prefix="/sessions", tags=["sessions-stream"])


def get_session_registry(websocket: WebSocket) -> SessionRegistry:
    """DI provider — pulls the registry attached to ``app.state`` in the lifespan.

    WebSocket variant of the HTTP provider in ``sessions.py``. FastAPI's
    dependency-injection system fills the parameter based on its type
    annotation; the HTTP one takes ``Request``, this one takes ``WebSocket``,
    each is fed by FastAPI in its respective handler context.
    """
    return websocket.app.state.registry


def get_audit_sink(websocket: WebSocket) -> AuditSink:
    """WebSocket variant of the HTTP audit-sink provider in ``kestrel.audit``."""
    return websocket.app.state.audit_sink


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
    audit: AuditSink = Depends(get_audit_sink),
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
    
    request_id = websocket.headers.get("x-request-id") or str(uuid.uuid4())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        path=f"/sessions/{session_id}/execute/stream",
    )

    try:
        await registry.get_info(session_id)
    except SessionNotFound:
        await _safe_close(websocket, 4404, "session_not_found")
        return

    await websocket.accept()
    STREAM_ACTIVE.inc()
    audit_status = 500
    audit_error_kind: str | None = "stream_aborted"
    audit_exit_code: int | None = None
    audit_timed_out: bool | None = None
    audit_code_length: int | None = None
    audit_start = time.perf_counter()
    try:
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
            audit_status = 400
            audit_error_kind = "bad_request"
            await websocket.send_json(
                StreamError(
                    code="bad_request",
                    detail=f"invalid execute request: {exc}",
                    request_id=request_id,
                ).model_dump()
            )
            await _safe_close(websocket, 1011, "bad_request")
            return
        audit_code_length = len(execute_request.code)

        # Shared state for the main loop + heartbeat task.
        # - send_lock serializes WebSocket sends (the main loop and the heartbeat
        #   task both call send_json; starlette doesn't support concurrent sends
        #   on one connection).
        # - heartbeat_reset signals "another message was just sent" so the
        #   heartbeat task can restart its silence timer instead of firing.
        # - start_perf is the execute start time for the elapsed_ms field on
        #   heartbeat messages.
        start_perf = time.perf_counter()
        send_lock = asyncio.Lock()
        heartbeat_reset = asyncio.Event()
        backpressure_timeout = settings.stream_backpressure_timeout_seconds
        heartbeat_seconds = settings.stream_heartbeat_seconds

        # Background task: detect client disconnect.
        # starlette's send_*() doesn't reliably raise WebSocketDisconnect when the
        # client has closed (the close frame may not have been processed yet at the
        # moment of send). receive_text() DOES raise WebSocketDisconnect cleanly.
        # We race this against each send with asyncio.wait(FIRST_COMPLETED).
        async def _wait_for_disconnect() -> None:
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                return

        # Background task: emit StreamHeartbeat during silent intervals.
        # Per design lock 6-progress: send a heartbeat every stream_heartbeat_seconds
        # IF no other message has been sent in that window. The reset event is set
        # by the main loop after each successful send, which causes this task's
        # wait_for to return (instead of timing out + sending). stream_heartbeat_seconds
        # == 0 disables heartbeats entirely.
        async def _heartbeat_loop() -> None:
            if heartbeat_seconds <= 0:
                return
            while True:
                try:
                    await asyncio.wait_for(
                        heartbeat_reset.wait(),
                        timeout=heartbeat_seconds,
                    )
                    # Another message was sent — clear the reset flag and loop.
                    heartbeat_reset.clear()
                except asyncio.TimeoutError:
                    # Silent interval — send a heartbeat, then loop.
                    elapsed_ms = int((time.perf_counter() - start_perf) * 1000)
                    try:
                        async with send_lock:
                            await asyncio.wait_for(
                                websocket.send_json(
                                    StreamHeartbeat(elapsed_ms=elapsed_ms).model_dump()
                                ),
                                timeout=backpressure_timeout,
                            )
                    except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError):
                        # Connection is dead/closing/back-pressured beyond limit.
                        # The main loop's next send will trigger the proper cleanup
                        # (kill runtime + close WebSocket); we just exit silently.
                        return

        disconnect_task = asyncio.create_task(_wait_for_disconnect())
        heartbeat_task = asyncio.create_task(_heartbeat_loop())

        try:
            try:
                async with registry.acquire_for_execute(session_id) as runtime:
                    exec_start = time.perf_counter()
                    final_result: StreamResult | None = None
                    execute_completed = False
                    try:
                        async for message in runtime.execute_stream(execute_request.code):
                            if disconnect_task.done():
                                logger.info(
                                    "session_stream_client_disconnect_mid_execute",
                                    session_id_prefix=session_id[:8],
                                )
                                return
                            if isinstance(message, (StreamResult, StreamError)):
                                message = message.model_copy(update={"request_id": request_id})
                            if isinstance(message, StreamResult):
                                final_result = message
                                audit_exit_code = message.exit_code
                                audit_timed_out = message.timed_out
                                audit_status = 200
                                audit_error_kind = None

                            async with send_lock:
                                send_task = asyncio.create_task(
                                    websocket.send_json(message.model_dump(mode="json"))
                                )
                                done, _pending = await asyncio.wait(
                                    [send_task, disconnect_task],
                                    timeout=backpressure_timeout,
                                    return_when=asyncio.FIRST_COMPLETED,
                                )

                                if not done:
                                    send_task.cancel()
                                    audit_status = 500
                                    audit_error_kind = "backpressure_timeout"
                                    logger.warning(
                                        "session_stream_backpressure_timeout",
                                        session_id_prefix=session_id[:8],
                                        timeout_seconds=backpressure_timeout,
                                    )
                                    await _safe_close(websocket, 1011, "backpressure_timeout")
                                    return

                                if disconnect_task in done:
                                    send_task.cancel()
                                    audit_status = 200
                                    audit_error_kind = "client_disconnect"
                                    logger.info(
                                        "session_stream_client_disconnect_mid_execute",
                                        session_id_prefix=session_id[:8],
                                    )
                                    return

                                # send_task completed — propagate any exception it raised.
                                send_task.result()

                            # Reset heartbeat AFTER successful send (outside the lock
                            # so the heartbeat task can promptly observe the reset).
                            heartbeat_reset.set()
                        if final_result is not None:
                            if final_result.timed_out:
                                outcome = "timed_out"
                            elif final_result.exit_code == 0:
                                outcome = "ok"
                            else:
                                outcome = "error"
                            EXECUTIONS.labels(backend="docker", outcome=outcome).inc()
                            EXECUTION_DURATION.labels(backend="docker").observe(time.perf_counter() - exec_start)
                        execute_completed = True
                    except WebSocketDisconnect:
                        audit_status = 200
                        audit_error_kind = "client_disconnect"
                        logger.info(
                            "session_stream_client_disconnect_mid_execute",
                            session_id_prefix=session_id[:8],
                        )
                        return
                    except SessionTimeout:
                        EXECUTIONS.labels(backend="docker", outcome="timed_out").inc()
                        EXECUTION_DURATION.labels(backend="docker").observe(time.perf_counter() - exec_start)
                        audit_status = 200
                        audit_error_kind = None
                        audit_timed_out = True
                        audit_exit_code = -1
                        logger.info(
                            "session_stream_timeout",
                            session_id_prefix=session_id[:8],
                        )
                        await _safe_close(websocket, 4410, "session_timeout")
                        return
                    except SessionTerminated:
                        EXECUTIONS.labels(backend="docker", outcome="error").inc()
                        EXECUTION_DURATION.labels(backend="docker").observe(time.perf_counter() - exec_start)
                        audit_status = 410
                        audit_error_kind = "session_terminated"
                        await _safe_close(websocket, 4410, "session_terminated")
                        return
                    except SessionProtocolError:
                        EXECUTIONS.labels(backend="docker", outcome="error").inc()
                        EXECUTION_DURATION.labels(backend="docker").observe(time.perf_counter() - exec_start)
                        audit_status = 500
                        audit_error_kind = "protocol_error"
                        logger.exception(
                            "session_stream_protocol_error",
                            session_id_prefix=session_id[:8],
                        )
                        await _safe_close(websocket, 1011, "protocol_error")
                        return
                    finally:
                        # Decision 6-disconnect: any abnormal exit from the execute
                        # loop — client disconnect, send failure, backpressure,
                        # timeout, protocol error — must kill the kernel. An
                        # abandoned mid-execute kernel keeps running and leaves a
                        # stale reply in the stdout pipe that poisons the next
                        # execute on the session. Normal completion skips this so
                        # the session stays alive for reuse. close() is idempotent.
                        if not execute_completed:
                            await runtime.close()
            except SessionBusy:
                audit_status = 409
                audit_error_kind = "session_busy"
                await websocket.send_json(
                    StreamError(
                        code="session_busy",
                        detail="another execute is in progress",
                        request_id=request_id,
                    ).model_dump()
                )
                await _safe_close(websocket, 4409, "session_busy")
                return
            except SessionNotFound:
                audit_status = 410
                audit_error_kind = "session_gone"
                await _safe_close(websocket, 4404, "session_not_found")
                return
        finally:
            for task in (disconnect_task, heartbeat_task):
                if not task.done():
                    task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, WebSocketDisconnect):
                    pass

        await _safe_close(websocket, 1000, "")
        logger.info("session_stream_closed", session_id_prefix=session_id[:8], code=1000)

    finally:
        STREAM_ACTIVE.dec()
        await audit.emit(
            AuditEvent(
                request_id=request_id,
                route="/sessions/{session_id}/execute/stream",
                method="WS",
                status=audit_status,
                session_id=session_id,
                code_length=audit_code_length,
                exit_code=audit_exit_code,
                timed_out=audit_timed_out,
                error_kind=audit_error_kind,
                duration_ms=int((time.perf_counter() - audit_start) * 1000),
            )
        )