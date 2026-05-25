"""Phase 6 substep 6: HTTP polling fallback for streaming session executes.

The WebSocket route (``sessions_stream.py``) is the primary streaming
transport. Some clients can't use it — corporate proxies routinely block the
WebSocket upgrade handshake. This module is the fallback: the same streamed
output, delivered over plain HTTP request/response.

Two endpoints (Decision 6.6-shape):

* ``POST /sessions/{id}/execute/polling`` starts an execute on a background
task and returns ``{execution_id}`` immediately — fire-and-forget.
* ``GET  /sessions/{id}/executions/{execution_id}?since=&wait=`` reads the
next batch of stream messages from the per-execution buffer, long-polling
up to ``wait`` seconds for new output (Decision 6.6-mech).

Why its own file (not ``sessions.py`` or ``sessions_stream.py``): Decision
6.6-loc. ``sessions.py`` holds synchronous request/response routes;
``sessions_stream.py`` holds the WebSocket lifecycle; this holds the
async-execute-plus-cursor-read lifecycle — three different mental models.

The buffer itself lives on the registry (``registry._polling_buffers``,
Decision 6.6-buffer); this module is the only code that reads and writes it.
"""
from __future__ import annotations

import time
import asyncio
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from kestrel.api.auth import require_api_key
from kestrel.api.schemas import (
    ExecuteRequest,
    PollingExecuteResponse,
    PollingReadResponse,
    StreamError,
    StreamResult,
)
from kestrel.api.sessions import get_session_registry
from kestrel.config import Settings, get_settings
from kestrel.execution.session_registry import (
    PollingBuffer,
    RegistryUnavailable,
    SessionBusy,
    SessionNotFound,
    SessionRegistry,
)
from kestrel.execution.session_runtime import (
    SessionProtocolError,
    SessionTerminated,
    SessionTimeout,
)
from kestrel.observability import EXECUTIONS, EXECUTION_DURATION
from kestrel.audit import AuditEvent, AuditSink, get_audit_sink
from kestrel.api_keys import ApiKeyInfo, audit_id_for

logger = structlog.get_logger()

router = APIRouter(
    prefix="/sessions",
    tags=["sessions-polling"],
    dependencies=[Depends(require_api_key)],
)


async def _run_polling_execute(
    registry: SessionRegistry,
    session_id: str,
    code: str,
    buffer: PollingBuffer,
    request_id: str,
    audit: AuditSink,
    execution_id: str,
    api_key_id: str | None,
) -> None:
    """Background orchestrator — wraps ``execute_stream`` for the polling path.

    Decision 6.6-wrap: the polling routes don't call ``execute_stream``
    directly. This task does — it acquires the session lock, iterates the
    streaming runtime, and appends each StreamMessage to ``buffer``. Every
    execute-time failure is converted into a terminal ``StreamError`` message
    in the buffer rather than an exception nobody can catch (the POST handler
    that spawned this task has long since returned). ``mark_done`` always
    runs, so a polling client's ``done`` flag is guaranteed to flip.

    Phase 7 substep 1: increments EXECUTIONS + EXECUTION_DURATION when the
    execute actually starts (i.e. acquire_for_execute succeeds). SessionBusy /
    SessionNotFound / RegistryUnavailable failures are not execution events —
    they leave ``outcome=None`` and skip the counter.

    Phase 7 substep 2 slice 3: emits an audit row on completion (one row per
    execution; see decision 7.3-emit-locus). The audit status reflects the
    outcome of the EXECUTE, not the HTTP status the polling client sees
    (which is always 200 — output is delivered via StreamError messages,
    not HTTP error codes).
    """
    exec_start = time.perf_counter()
    outcome: str | None = None
    audit_status = 200
    audit_error_kind: str | None = None
    audit_exit_code: int | None = None
    audit_timed_out: bool | None = None
    try:
        async with registry.acquire_for_execute(session_id) as runtime:
            outcome = "error"  # default if execute_stream raises mid-flight
            audit_status = 500
            audit_error_kind = "internal"
            async for message in runtime.execute_stream(code):
                if isinstance(message, (StreamResult, StreamError)):
                    message = message.model_copy(update={"request_id": request_id})
                await buffer.append(message)
                if isinstance(message, StreamResult):
                    audit_exit_code = message.exit_code
                    audit_timed_out = message.timed_out
                    audit_status = 200
                    audit_error_kind = None
                    if message.timed_out:
                        outcome = "timed_out"
                    elif message.exit_code == 0:
                        outcome = "ok"
                    else:
                        outcome = "error"
    except SessionBusy:
        audit_status = 409
        audit_error_kind = "session_busy"
        await buffer.append(
            StreamError(code="session_busy", detail="another execute is in progress", request_id=request_id)
        )
    except SessionNotFound:
        audit_status = 404
        audit_error_kind = "session_not_found"
        await buffer.append(
            StreamError(code="session_not_found", detail="session not found", request_id=request_id)
        )
    except SessionTimeout:
        outcome = "timed_out"
        audit_status = 200
        audit_error_kind = None
        audit_timed_out = True
        audit_exit_code = -1
        logger.info("polling_execute_timeout", session_id_prefix=session_id[:8])
        await buffer.append(
            StreamError(code="session_timeout", detail="execution exceeded the time limit", request_id=request_id)
        )
    except SessionTerminated:
        outcome = "error"
        audit_status = 410
        audit_error_kind = "session_terminated"
        await buffer.append(
            StreamError(code="session_terminated", detail="session is no longer running", request_id=request_id)
        )
    except SessionProtocolError:
        outcome = "error"
        audit_status = 500
        audit_error_kind = "protocol_error"
        logger.exception("polling_execute_protocol_error", session_id_prefix=session_id[:8])
        await buffer.append(
            StreamError(code="protocol_error", detail="internal protocol error", request_id=request_id)
        )
    except RegistryUnavailable:
        audit_status = 503
        audit_error_kind = "registry_unavailable"
        logger.warning("polling_execute_registry_unavailable", session_id_prefix=session_id[:8])
        await buffer.append(
            StreamError(code="registry_unavailable", detail="session store unavailable", request_id=request_id)
        )
    except Exception:
        if outcome is None:
            outcome = "error"
        audit_status = 500
        audit_error_kind = "internal"
        logger.exception("polling_execute_failed", session_id_prefix=session_id[:8])
        await buffer.append(StreamError(code="internal", detail="internal error", request_id=request_id))
    finally:
        if outcome is not None:
            EXECUTIONS.labels(backend="docker", outcome=outcome).inc()
            EXECUTION_DURATION.labels(backend="docker").observe(time.perf_counter() - exec_start)
        await audit.emit(
            AuditEvent(
                request_id=request_id,
                route="/sessions/{session_id}/execute/polling",
                method="POST",
                status=audit_status,
                api_key_id=api_key_id,
                session_id=session_id,
                execution_id=execution_id,
                code_length=len(code),
                exit_code=audit_exit_code,
                timed_out=audit_timed_out,
                error_kind=audit_error_kind,
                duration_ms=int((time.perf_counter() - exec_start) * 1000),
            )
        )
        await buffer.mark_done()


@router.post(
    "/{session_id}/execute/polling",
    response_model=PollingExecuteResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_polling_execute(
    session_id: str,
    req: ExecuteRequest,
    registry: SessionRegistry = Depends(get_session_registry),
    audit: AuditSink = Depends(get_audit_sink),
    api_key_info: ApiKeyInfo | str | None = Depends(require_api_key),
) -> PollingExecuteResponse:
    """Start an execute on a background task; return its execution_id at once.

    Validates only that the session exists (404 via the SessionNotFound
    handler). Busy / timeout / terminated outcomes can't be known yet — the
    execute hasn't run — so they surface later as a StreamError in the buffer,
    read via the GET endpoint.

    Phase 7 substep 2 slice 3: this handler does NOT audit. The audit row for
    a polling execute is emitted by ``_run_polling_execute`` when it
    completes — one row per execution, regardless of how it ended.
    POST-time 404s (session not found before spawn) are an audit gap; see
    decision 7.3-emit-locus.
    """
    await registry.get_info(session_id)  # raises SessionNotFound -> HTTP 404

    execution_id = uuid.uuid4().hex
    buffer = PollingBuffer()
    registry._polling_buffers.setdefault(session_id, {})[execution_id] = buffer
    registry._refresh_metrics()
    request_id = structlog.contextvars.get_contextvars().get("request_id", "")
    buffer.task = asyncio.create_task(
        _run_polling_execute(
            registry, session_id, req.code, buffer, request_id, audit,
            execution_id, audit_id_for(api_key_info),
        )
    )
    logger.info(
        "polling_execute_started",
        session_id_prefix=session_id[:8],
        execution_id_prefix=execution_id[:8],
        code_length=len(req.code),
    )
    return PollingExecuteResponse(execution_id=execution_id)


@router.get(
    "/{session_id}/executions/{execution_id}",
    response_model=PollingReadResponse,
)
async def read_polling_execute(
    session_id: str,
    execution_id: str,
    registry: SessionRegistry = Depends(get_session_registry),
    settings: Settings = Depends(get_settings),
    since: int = Query(default=0, ge=0, description="Cursor: return messages with index >= this."),
    wait: float = Query(default=0.0, ge=0.0, description="Long-poll: hold the request up to this many seconds for new output. 0 = return immediately."),
) -> PollingReadResponse:
    """Read the next batch of stream messages from an execution's buffer.

    Long-polls up to ``min(wait, polling_max_wait_seconds)`` for output past
    ``since`` to appear. Returns 404 if the execution buffer is unknown
    (never started, or already TTL-evicted / session-deleted).
    """
    by_execution = registry._polling_buffers.get(session_id)
    buffer = by_execution.get(execution_id) if by_execution else None
    if buffer is None:
        raise HTTPException(status_code=404, detail="execution not found")

    capped_wait = min(wait, settings.polling_max_wait_seconds)
    await buffer.wait_for_messages(since, capped_wait)

    new_messages = buffer.messages[since:]
    next_cursor = since + len(new_messages)
    done = buffer.done and next_cursor >= len(buffer.messages)
    return PollingReadResponse(
        messages=new_messages,
        next_cursor=next_cursor,
        done=done,
        request_id=structlog.contextvars.get_contextvars().get("request_id", ""),
    )