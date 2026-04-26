from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends

from kestrel.api.auth import require_api_key
from kestrel.api.schemas import ExecuteRequest, ExecuteResponse
from kestrel.config import Settings, get_settings
from kestrel.execution.manager import run_code

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
) -> ExecuteResponse:
    """Run user-supplied Python code in a subprocess; return captured output."""
    result = await run_code(req.code, settings)
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