from __future__ import annotations

from fastapi import APIRouter, Depends

from kestrel.api.schemas import ExecuteRequest, ExecuteResponse
from kestrel.config import Settings, get_settings
from kestrel.execution.manager import run_code
from kestrel.api.auth import require_api_key

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
    return await run_code(req.code, settings)