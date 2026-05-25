from __future__ import annotations

import time
import structlog
from fastapi import APIRouter, Depends, Request, status

from kestrel.api.auth import require_api_key
from kestrel.api.schemas import (
    ExecuteRequest,
    SessionExecuteResponse,
    SessionListResponse,
    SessionResponse,
)
from kestrel.execution.session_registry import SessionRegistry
from kestrel.execution.session_runtime import SessionTimeout
from kestrel.observability import EXECUTIONS, EXECUTION_DURATION
from kestrel.audit import AuditEvent, AuditSink, get_audit_sink, http_status_for_exception
from kestrel.api_keys import ApiKeyInfo, audit_id_for

logger = structlog.get_logger()


def get_session_registry(request: Request) -> SessionRegistry:
    """DI provider — pulls the registry attached to ``app.state`` in the lifespan."""
    return request.app.state.registry


router = APIRouter(
    prefix="/sessions",
    tags=["sessions"],
    dependencies=[Depends(require_api_key)],
)


@router.post(
    "",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    registry: SessionRegistry = Depends(get_session_registry),
    audit: AuditSink = Depends(get_audit_sink),
    api_key_info: ApiKeyInfo | str | None = Depends(require_api_key),
) -> SessionResponse:
    request_id = structlog.contextvars.get_contextvars().get("request_id", "")
    audit_api_key_id = audit_id_for(api_key_info)
    start = time.perf_counter()
    try:
        info = await registry.create()
    except Exception as e:
        await audit.emit(
            AuditEvent(
                request_id=request_id,
                route="/sessions",
                method="POST",
                status=http_status_for_exception(e),
                api_key_id=audit_api_key_id,
                error_kind=type(e).__name__,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        )
        raise
    await audit.emit(
        AuditEvent(
            request_id=request_id,
            route="/sessions",
            method="POST",
            status=201,
            api_key_id=audit_api_key_id,
            session_id=info.session_id,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
    )
    return SessionResponse.model_validate(info, from_attributes=True)


@router.get(
    "",
    response_model=SessionListResponse,
)
async def list_sessions(
    registry: SessionRegistry = Depends(get_session_registry),
    audit: AuditSink = Depends(get_audit_sink),
    api_key_info: ApiKeyInfo | str | None = Depends(require_api_key),
) -> SessionListResponse:
    request_id = structlog.contextvars.get_contextvars().get("request_id", "")
    audit_api_key_id = audit_id_for(api_key_info)
    start = time.perf_counter()
    try:
        infos = await registry.list()
    except Exception as e:
        await audit.emit(
            AuditEvent(
                request_id=request_id,
                route="/sessions",
                method="GET",
                status=http_status_for_exception(e),
                api_key_id=audit_api_key_id,
                error_kind=type(e).__name__,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        )
        raise
    await audit.emit(
        AuditEvent(
            request_id=request_id,
            route="/sessions",
            method="GET",
            status=200,
            api_key_id=audit_api_key_id,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
    )
    return SessionListResponse(
        sessions=[SessionResponse.model_validate(info, from_attributes=True) for info in infos]
    )


@router.get(
    "/{session_id}",
    response_model=SessionResponse,
)
async def get_session(
    session_id: str,
    registry: SessionRegistry = Depends(get_session_registry),
    audit: AuditSink = Depends(get_audit_sink),
    api_key_info: ApiKeyInfo | str | None = Depends(require_api_key),
) -> SessionResponse:
    request_id = structlog.contextvars.get_contextvars().get("request_id", "")
    audit_api_key_id = audit_id_for(api_key_info)
    start = time.perf_counter()
    try:
        info = await registry.get_info(session_id)
    except Exception as e:
        await audit.emit(
            AuditEvent(
                request_id=request_id,
                route="/sessions/{session_id}",
                method="GET",
                status=http_status_for_exception(e),
                api_key_id=audit_api_key_id,
                session_id=session_id,
                error_kind=type(e).__name__,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        )
        raise
    await audit.emit(
        AuditEvent(
            request_id=request_id,
            route="/sessions/{session_id}",
            method="GET",
            status=200,
            api_key_id=audit_api_key_id,
            session_id=session_id,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
    )
    return SessionResponse.model_validate(info, from_attributes=True)


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_session(
    session_id: str,
    registry: SessionRegistry = Depends(get_session_registry),
    audit: AuditSink = Depends(get_audit_sink),
    api_key_info: ApiKeyInfo | str | None = Depends(require_api_key),
) -> None:
    request_id = structlog.contextvars.get_contextvars().get("request_id", "")
    audit_api_key_id = audit_id_for(api_key_info)
    start = time.perf_counter()
    try:
        await registry.delete(session_id)
    except Exception as e:
        await audit.emit(
            AuditEvent(
                request_id=request_id,
                route="/sessions/{session_id}",
                method="DELETE",
                status=http_status_for_exception(e),
                api_key_id=audit_api_key_id,
                session_id=session_id,
                error_kind=type(e).__name__,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        )
        raise
    await audit.emit(
        AuditEvent(
            request_id=request_id,
            route="/sessions/{session_id}",
            method="DELETE",
            status=204,
            api_key_id=audit_api_key_id,
            session_id=session_id,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
    )


@router.post(
    "/{session_id}/execute",
    response_model=SessionExecuteResponse,
)
async def execute_in_session(
    session_id: str,
    req: ExecuteRequest,
    registry: SessionRegistry = Depends(get_session_registry),
    audit: AuditSink = Depends(get_audit_sink),
    api_key_info: ApiKeyInfo | str | None = Depends(require_api_key),
) -> SessionExecuteResponse:
    backend = "docker"  # session containers are always docker
    request_id = structlog.contextvars.get_contextvars().get("request_id", "")
    audit_api_key_id = audit_id_for(api_key_info)
    overall_start = time.perf_counter()
    try:
        async with registry.acquire_for_execute(session_id) as runtime:
            start = time.perf_counter()
            try:
                result = await runtime.execute(req.code)
            except SessionTimeout:
                EXECUTIONS.labels(backend=backend, outcome="timed_out").inc()
                EXECUTION_DURATION.labels(backend=backend).observe(time.perf_counter() - start)
                logger.info(
                    "session_execute_timed_out",
                    session_id_prefix=session_id[:8],
                )
                await audit.emit(
                    AuditEvent(
                        request_id=request_id,
                        route="/sessions/{session_id}/execute",
                        method="POST",
                        status=200,
                        api_key_id=audit_api_key_id,
                        session_id=session_id,
                        code_length=len(req.code),
                        exit_code=-1,
                        timed_out=True,
                        duration_ms=int((time.perf_counter() - overall_start) * 1000),
                    )
                )
                return SessionExecuteResponse(timed_out=True, exit_code=-1, outputs=[])
    except Exception as e:
        await audit.emit(
            AuditEvent(
                request_id=request_id,
                route="/sessions/{session_id}/execute",
                method="POST",
                status=http_status_for_exception(e),
                api_key_id=audit_api_key_id,
                session_id=session_id,
                code_length=len(req.code),
                error_kind=type(e).__name__,
                duration_ms=int((time.perf_counter() - overall_start) * 1000),
            )
        )
        raise

    duration = time.perf_counter() - start
    if result.timed_out:
        outcome = "timed_out"
    elif result.exit_code == 0:
        outcome = "ok"
    else:
        outcome = "error"
    EXECUTIONS.labels(backend=backend, outcome=outcome).inc()
    EXECUTION_DURATION.labels(backend=backend).observe(duration)

    await audit.emit(
        AuditEvent(
            request_id=request_id,
            route="/sessions/{session_id}/execute",
            method="POST",
            status=200,
            api_key_id=audit_api_key_id,
            session_id=session_id,
            code_length=len(req.code),
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_ms=int((time.perf_counter() - overall_start) * 1000),
        )
    )

    logger.info(
        "session_execute_completed",
        session_id_prefix=session_id[:8],
        code_length=len(req.code),
        duration_ms=result.duration_ms,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
    )
    return result