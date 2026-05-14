from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Request, status

from kestrel.api.auth import require_api_key
from kestrel.api.schemas import (
    ExecuteRequest,
    ExecuteResponse,
    SessionListResponse,
    SessionResponse,
)
from kestrel.execution.session_registry import SessionRegistry
from kestrel.execution.session_runtime import SessionTimeout

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
    response_model=ExecuteResponse,
)
async def execute_in_session(
    session_id: str,
    req: ExecuteRequest,
    registry: SessionRegistry = Depends(get_session_registry),
) -> ExecuteResponse:
    try:
        async with registry.acquire_for_execute(session_id) as runtime:
            result = await runtime.execute(req.code)
    except SessionTimeout:
        logger.info(
            "session_execute_timed_out",
            session_id_prefix=session_id[:8],
        )
        return ExecuteResponse(timed_out=True, exit_code=-1)

    logger.info(
        "session_execute_completed",
        session_id_prefix=session_id[:8],
        code_length=len(req.code),
        duration_ms=result.duration_ms,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
    )
    return result