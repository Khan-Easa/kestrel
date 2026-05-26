import secrets

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from kestrel.api_keys import ApiKeyInfo, ApiKeyStore, get_api_key_store
from kestrel.config import Settings, get_settings

logger = structlog.get_logger()

bearer_scheme = HTTPBearer(auto_error=False)


class AuthRejected(Exception):
    """Raised by ``verify_bearer`` when authentication fails. Callers
    convert to their transport's rejection shape (HTTP 401 in
    ``require_api_key``, WebSocket close 4401 in ``sessions_stream``)."""


async def verify_bearer(
    bearer: str | None,
    settings: Settings,
    store: ApiKeyStore | None,
) -> ApiKeyInfo | str | None:
    """Resolve a bearer token to an identity.

    Returns:
        None: auth disabled — empty ``settings.dev_api_key`` AND ``store is None``.
            Phase 1-6 backward-compat path.
        "dev": dev shim matched ``settings.dev_api_key``. Logs ``dev_api_key_in_use``
            per decision ``7-pg-required``.
        ApiKeyInfo: the store verified the token; this is the resolved identity.

    Raises:
        AuthRejected: a token was required but didn't match any path.
    """
    auth_disabled = settings.dev_api_key == "" and store is None
    if auth_disabled:
        return None

    if not bearer:
        raise AuthRejected("no bearer token provided")

    if settings.dev_api_key != "" and secrets.compare_digest(
        bearer, settings.dev_api_key
    ):
        logger.info("dev_api_key_in_use")
        return "dev"

    if store is not None:
        info = await store.verify(bearer)
        if info is not None:
            return info

    raise AuthRejected("invalid bearer token")


async def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
    store: ApiKeyStore | None = Depends(get_api_key_store),
) -> ApiKeyInfo | str | None:
    """FastAPI dependency. Resolves the bearer; raises 401 on failure;
    returns the resolved identity (``None`` / ``"dev"`` / ``ApiKeyInfo``)
    on success. Routes that need the identity (e.g. for audit ``api_key_id``)
    inject this positionally; FastAPI caches so the dep runs once per request
    even when also listed in router-level ``dependencies=[...]``."""
    bearer = credentials.credentials if credentials is not None else None
    try:
        return await verify_bearer(bearer, settings, store)
    except AuthRejected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── Phase 7 substep 5 slice 3: per-route-class rate-limit dependencies ──


from kestrel.api_keys import audit_id_for
from kestrel.observability import RATE_LIMITED, RATE_LIMIT_FAILURES
from kestrel.rate_limit import (
    RateLimiter,
    RateLimiterUnavailable,
    RouteClass,
    get_rate_limiter,
)


async def _enforce_rate_limit(
    api_key_info: ApiKeyInfo | str | None,
    route_class: RouteClass,
    limiter: RateLimiter,
) -> None:
    """Check the limiter; raise HTTP 429 with Retry-After on denial.

    - ``api_key_info is None`` (auth disabled) → skip per ``7.5-unauth-skip``.
    - ``RateLimiterUnavailable`` → fail-open per ``7.5-fail-policy``: log,
    bump ``RATE_LIMIT_FAILURES``, return without raising.
    - ``allowed=False`` → bump ``RATE_LIMITED``, raise 429 with the
    ``Retry-After`` header set to ``decision.retry_after_seconds``.
    """
    identity = audit_id_for(api_key_info)
    if identity is None:
        return
    try:
        decision = await limiter.check(identity, route_class)
    except RateLimiterUnavailable as exc:
        logger.warning(
            "rate_limit_check_failed",
            route_class=route_class,
            error=str(exc),
        )
        RATE_LIMIT_FAILURES.labels(route_class=route_class).inc()
        return
    if decision.allowed:
        return
    RATE_LIMITED.labels(route_class=route_class).inc()
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="rate_limited",
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )


async def require_rate_limit_execute(
    api_key_info: ApiKeyInfo | str | None = Depends(require_api_key),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> None:
    await _enforce_rate_limit(api_key_info, "execute", limiter)


async def require_rate_limit_session_lifecycle(
    api_key_info: ApiKeyInfo | str | None = Depends(require_api_key),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> None:
    await _enforce_rate_limit(api_key_info, "session_lifecycle", limiter)


async def require_rate_limit_admin(
    api_key_info: ApiKeyInfo | str | None = Depends(require_api_key),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> None:
    await _enforce_rate_limit(api_key_info, "admin", limiter)