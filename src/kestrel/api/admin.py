"""Phase 7 substep 6 slice 1: admin endpoints.

Brief §6.7 calls for operator-facing endpoints to inspect API keys,
running sessions, and the audit log. Slice 1 ships the read-only GET
routes; slice 2 adds the POST/DELETE mutation routes for key management.

All routes here are gated by router-level
``dependencies=[Depends(require_admin_scope), Depends(require_rate_limit_admin)]``:
the scope check (decision 7-admin-dev-shim) returns 403 on missing scope,
and the rate-limit dep (already shipped in substep 5 slice 3) caps the
endpoint at ``KESTREL_RATE_LIMIT_ADMIN_PER_MINUTE`` (default 60/min).
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.api.auth import require_admin_scope, require_rate_limit_admin
from kestrel.api.schemas import (
    ApiKeyListResponse,
    ApiKeyResponse,
    AuditEventResponse,
    AuditListResponse,
    SessionListResponse,
    SessionResponse,
)
from kestrel.api.sessions import get_session_registry
from kestrel.api_keys import ApiKeyStore, get_api_key_store
from kestrel.db.queries import list_audit_events
from kestrel.execution.session_registry import SessionRegistry

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_scope), Depends(require_rate_limit_admin)],
)


def get_sessionmaker(
    request: Request,
) -> async_sessionmaker[AsyncSession] | None:
    """FastAPI dep: the sessionmaker bound to ``app.state`` by the lifespan,
    or ``None`` when no engine was built (i.e. ``audit_backend == 'null'``
    AND ``api_key_backend == 'null'``). ``GET /admin/audit`` returns 503
    when this is ``None`` — there is no Postgres to read from."""
    return getattr(request.app.state, "sessionmaker", None)


@router.get("/keys", response_model=ApiKeyListResponse)
async def list_keys(
    store: ApiKeyStore | None = Depends(get_api_key_store),
) -> ApiKeyListResponse:
    """List all API keys (active + revoked), newest first.

    Returns an empty list when ``api_key_backend == "null"`` — no store to
    query in that mode, but an empty list is still the right wire shape
    for clients that handle both modes uniformly."""
    if store is None:
        return ApiKeyListResponse(keys=[])
    infos = await store.list()
    return ApiKeyListResponse(
        keys=[
            ApiKeyResponse(
                id=str(info.id),
                label=info.label,
                created_at=info.created_at,
                revoked_at=info.revoked_at,
                scopes=list(info.scopes),
            )
            for info in infos
        ]
    )


@router.get("/sessions", response_model=SessionListResponse)
async def list_admin_sessions(
    registry: SessionRegistry = Depends(get_session_registry),
) -> SessionListResponse:
    """List sessions across all keys.

    Knowledge of a ``session_id`` is the access right for that session
    (decision 4-scope) — Phase 7 does not introduce per-key ownership of
    sessions, so this returns the same data as ``GET /sessions/`` does
    for the caller's bearer. The admin route exists as the operator-
    facing surface; rate-limited under the ``admin`` bucket, separate
    from the per-key ``session_lifecycle`` budget on ``GET /sessions/``.
    """
    infos = await registry.list()
    return SessionListResponse(
        sessions=[
            SessionResponse(
                session_id=info.session_id,
                created_at=info.created_at,
                last_used=info.last_used,
            )
            for info in infos
        ]
    )


@router.get("/audit", response_model=AuditListResponse)
async def list_audit(
    limit: int = Query(default=50, ge=1, le=500),
    before_ts: datetime | None = Query(default=None),
    sessionmaker: async_sessionmaker[AsyncSession] | None = Depends(get_sessionmaker),
) -> AuditListResponse:
    """Cursor-paginated read of ``audit_events`` per ``7.6-audit-pagination``.

    First call omits ``before_ts``; subsequent calls pass the response's
    ``next_before_ts`` until it returns ``null`` (last page). ``limit``
    is clamped at the route layer via ``Query(ge=1, le=500)``.

    Returns HTTP 503 when no sessionmaker is bound to app.state — that
    means no Postgres engine was built, so there is nothing to read.
    """
    if sessionmaker is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="audit backend not configured",
        )
    rows = await list_audit_events(
        sessionmaker, limit=limit, before_ts=before_ts
    )
    events = [
        AuditEventResponse(
            id=str(row.id),
            ts=row.ts,
            request_id=row.request_id,
            api_key_id=row.api_key_id,
            route=row.route,
            method=row.method,
            status=row.status,
            session_id=row.session_id,
            execution_id=row.execution_id,
            code_length=row.code_length,
            exit_code=row.exit_code,
            timed_out=row.timed_out,
            duration_ms=row.duration_ms,
            error_kind=row.error_kind,
        )
        for row in rows
    ]
    next_before_ts = rows[-1].ts if len(rows) == limit else None
    return AuditListResponse(events=events, next_before_ts=next_before_ts)