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
) -> SessionResponse:
    info = await registry.create()
    return SessionResponse.model_validate(info, from_attributes=True)


@router.get(
    "",
    response_model=SessionListResponse,
)
async def list_sessions(
    registry: SessionRegistry = Depends(get_session_registry),
) -> SessionListResponse:
    return SessionListResponse(
        sessions=[
            SessionResponse.model_validate(info, from_attributes=True)
            for info in await registry.list()
        ]
    )


@router.get(
    "/{session_id}",
    response_model=SessionResponse,
)
async def get_session(
    session_id: str,
    registry: SessionRegistry = Depends(get_session_registry),
) -> SessionResponse:
    info = await registry.get_info(session_id)
    return SessionResponse.model_validate(info, from_attributes=True)


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_session(
    session_id: str,
    registry: SessionRegistry = Depends(get_session_registry),
) -> None:
    await registry.delete(session_id)


@router.post(
    "/{session_id}/execute",
    response_model=SessionExecuteResponse,
)
async def execute_in_session(
    session_id: str,
    req: ExecuteRequest,
    registry: SessionRegistry = Depends(get_session_registry),
) -> SessionExecuteResponse:
    backend = "docker"  # session containers are always docker
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
            return SessionExecuteResponse(timed_out=True, exit_code=-1, outputs=[])

    duration = time.perf_counter() - start
    if result.timed_out:
        outcome = "timed_out"
    elif result.exit_code == 0:
        outcome = "ok"
    else:
        outcome = "error"
    EXECUTIONS.labels(backend=backend, outcome=outcome).inc()
    EXECUTION_DURATION.labels(backend=backend).observe(duration)

    logger.info(
        "session_execute_completed",
        session_id_prefix=session_id[:8],
        code_length=len(req.code),
        duration_ms=result.duration_ms,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
    )
    return result