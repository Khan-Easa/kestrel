from __future__ import annotations

import time
import structlog
from fastapi import APIRouter, Depends, Response 
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest 

from kestrel.api.auth import require_api_key, require_rate_limit_execute
from kestrel.api.schemas import ExecuteRequest, ExecuteResponse
from kestrel.config import Settings, get_settings
from kestrel.execution import Executor, get_executor
from kestrel.observability import EXECUTIONS, EXECUTION_DURATION
from kestrel.audit import AuditEvent, AuditSink, get_audit_sink
from kestrel.api_keys import ApiKeyInfo, audit_id_for

logger = structlog.get_logger()

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Returns a simple JSON body."""
    return {"status": "ok"}


@router.post(
    "/execute",
    response_model=ExecuteResponse,
    dependencies=[Depends(require_api_key), Depends(require_rate_limit_execute)],
)
async def execute(
    req: ExecuteRequest,
    settings: Settings = Depends(get_settings),
    executor: Executor = Depends(get_executor),
    audit: AuditSink = Depends(get_audit_sink),
    api_key_info: ApiKeyInfo | str | None = Depends(require_api_key),
) -> ExecuteResponse:
    """Run user-supplied Python code via the configured executor; return captured output."""
    backend = settings.executor_backend
    request_id = structlog.contextvars.get_contextvars().get("request_id", "")
    audit_api_key_id = audit_id_for(api_key_info)
    effective_timeout = (
        settings.execute_timeout_seconds
        if req.timeout_seconds is None
        else min(req.timeout_seconds, settings.execute_timeout_seconds)
    )
    start = time.perf_counter()
    try:
        result = await executor.run(req.code, settings, timeout_seconds=effective_timeout)
    except Exception as e:
        duration = time.perf_counter() - start
        EXECUTIONS.labels(backend=backend, outcome="error").inc()
        EXECUTION_DURATION.labels(backend=backend).observe(duration)
        await audit.emit(
            AuditEvent(
                request_id=request_id,
                route="/execute",
                method="POST",
                status=500,
                api_key_id=audit_api_key_id,
                code_length=len(req.code),
                error_kind=type(e).__name__,
                duration_ms=int(duration * 1000),
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
            route="/execute",
            method="POST",
            status=200,
            api_key_id=audit_api_key_id,
            code_length=len(req.code),
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_ms=int(duration * 1000),
        )
    )

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