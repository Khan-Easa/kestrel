from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends

from kestrel.api.auth import require_api_key
from kestrel.api.schemas import ExecuteRequest, ExecuteResponse
from kestrel.config import Settings, get_settings
from kestrel.execution import Executor, get_executor

logger = structlog.get_logger()

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Returns a simple JSON body."""
    return {"status": "ok"}


@router.post(
    "/execute",
    response_model=ExecuteResponse,
    dependencies=[Depends(require_api_key)],
)
async def execute(
    req: ExecuteRequest,
    settings: Settings = Depends(get_settings),
    executor: Executor = Depends(get_executor),
) -> ExecuteResponse:
    """Run user-supplied Python code via the configured executor; return captured output."""
    result = await executor.run(req.code, settings)
    logger.info(
        "execute_completed",
        code_length=len(req.code),
        duration_ms=result.duration_ms,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        stdout_truncated=result.stdout_truncated,
        stderr_truncated=result.stderr_truncated,
    )
    return result