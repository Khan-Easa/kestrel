from __future__ import annotations

from fastapi import APIRouter, Depends

from kestrel.api.schemas import ExecuteRequest, ExecuteResponse
from kestrel.config import Settings, get_settings
from kestrel.execution.manager import run_code

router = APIRouter()

@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Returns a simple JSON body."""
    return {"status": "ok"}


@router.post("/execute", response_model=ExecuteResponse)
async def execute(
    req: ExecuteRequest,
    settings: Settings = Depends(get_settings),
) -> ExecuteResponse:
    """Run user-supplied Python code in a subprocess; return captured output."""
    return await run_code(req.code, settings)