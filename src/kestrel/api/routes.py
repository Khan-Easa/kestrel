from __future__ import annotations

import time
import structlog
from fastapi import APIRouter, Depends, Response  # NEW: + Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest  # NEW

from kestrel.api.auth import require_api_key
from kestrel.api.schemas import ExecuteRequest, ExecuteResponse
from kestrel.config import Settings, get_settings
from kestrel.execution import Executor, get_executor
from kestrel.observability import EXECUTIONS, EXECUTION_DURATION

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
    backend = settings.executor_backend
    start = time.perf_counter()
    try:
        result = await executor.run(req.code, settings)
    except Exception:
        EXECUTIONS.labels(backend=backend, outcome="error").inc()
        EXECUTION_DURATION.labels(backend=backend).observe(time.perf_counter() - start)
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


@router.get("/metrics")
async def metrics() -> Response:
    """Prometheus scrape endpoint. Public, like /health (see 7-metrics-auth)."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)