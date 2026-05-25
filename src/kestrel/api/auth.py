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