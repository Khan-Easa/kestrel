from __future__ import annotations

"""Phase 7 substep 3: API-key store.

Routes today gate on ``KESTREL_DEV_API_KEY`` (Phase 1's bearer-vs-fixed-string).
Substep 3 introduces a Postgres-backed store as the production source of
truth, with the dev key retained as an explicit opt-in shim
(decision 7-pg-required). Slice 1 ships the store machinery; slice 2 wires
it into ``require_api_key``.

Token format (decision 7-key-token-prefix): tokens look like
``kestrel_<43chars>`` — a 7-char identifier prefix plus 256 bits of entropy
from ``secrets.token_urlsafe(32)``. The prefix isn't secret; it lets leak
scanners (GitHub secret scanning, GitGuardian) match Kestrel tokens
without entropy.

Hashing (decision 7-key-hash): stored ``key_hash`` is the lowercase hex of
``hashlib.sha256(token.encode()).hexdigest()`` — unsalted, fast. Tokens are
already high-entropy random; a salt or slow hash buys nothing on a hot
auth path.

The store is selected at startup by ``build_api_key_store(settings, engine=...)``:

- ``api_key_backend = "null"`` (default): returns ``None``. ``require_api_key``
falls back to the legacy dev-key path; auth disabled if dev key empty.
- ``api_key_backend = "postgres"``: ``PostgresApiKeyStore(engine)`` returns
a real store. Slice 2 wires it into ``require_api_key``.

Decision 7-key-null-store-as-none: there is no ``NullApiKeyStore`` because
most operations (``create``, ``revoke``, ``list``) can't be no-ops sensibly.
Callers check ``store is None`` and branch.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

import structlog
from fastapi import Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from kestrel.config import Settings
from kestrel.db.models import ApiKey
from kestrel.db.session import build_sessionmaker

logger = structlog.get_logger()


TOKEN_PREFIX = "kestrel_"


def generate_token() -> str:
    """Mint a new API-key token: ``kestrel_<43 url-safe chars>`` (256-bit entropy).
    Caller is responsible for storing only the hash; the plaintext is returned
    once and never recoverable from the DB."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Return ``sha256(token).hexdigest()``. Used both at create time (to
    store) and at verify time (to look up). Deterministic + fast — 256-bit
    random tokens defeat rainbow tables without needing a salt."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApiKeyInfo:
    """Public-facing API-key shape — what callers of the store receive.
    Does NOT include ``key_hash``; that's persistence detail."""

    id: uuid.UUID
    label: str
    created_at: datetime
    revoked_at: datetime | None
    scopes: list[str]


@runtime_checkable
class ApiKeyStore(Protocol):
    """The API-key store contract.

    ``verify`` is the hot path called on every authenticated request — must be
    a single indexed lookup. The other methods are admin operations
    (substep 4's ``kestrel-keys`` CLI will call them)."""

    async def start(self) -> None: ...
    async def aclose(self) -> None: ...
    async def verify(self, token: str) -> ApiKeyInfo | None: ...
    async def create(
        self, label: str, scopes: list[str] | None = None
    ) -> tuple[str, ApiKeyInfo]: ...
    async def list(self) -> list[ApiKeyInfo]: ...
    async def revoke(self, key_id: uuid.UUID) -> bool: ...


class PostgresApiKeyStore:
    """Async-SQLAlchemy implementation of ``ApiKeyStore``.

    No background tasks, no queue — every method is a single DB round-trip
    on the injected engine. The engine is owned by the lifespan
    (decision 7.2-engine-owner) and shared with the audit pipeline."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessionmaker = build_sessionmaker(engine)

    async def start(self) -> None:
        logger.info("postgres_api_key_store_started")

    async def aclose(self) -> None:
        logger.info("postgres_api_key_store_stopped")

    async def verify(self, token: str) -> ApiKeyInfo | None:
        token_hash = hash_token(token)
        async with self._sessionmaker() as session:
            stmt = select(ApiKey).where(
                ApiKey.key_hash == token_hash,
                ApiKey.revoked_at.is_(None),
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return _to_info(row)

    async def create(
        self, label: str, scopes: list[str] | None = None
    ) -> tuple[str, ApiKeyInfo]:
        token = generate_token()
        token_hash = hash_token(token)
        effective_scopes = list(scopes) if scopes is not None else ["execute"]
        row = ApiKey(key_hash=token_hash, label=label, scopes=effective_scopes)
        async with self._sessionmaker() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return token, _to_info(row)

    async def list(self) -> list[ApiKeyInfo]:
        async with self._sessionmaker() as session:
            stmt = select(ApiKey).order_by(ApiKey.created_at.desc())
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_info(r) for r in rows]

    async def revoke(self, key_id: uuid.UUID) -> bool:
        async with self._sessionmaker() as session:
            stmt = (
                update(ApiKey)
                .where(ApiKey.id == key_id, ApiKey.revoked_at.is_(None))
                .values(revoked_at=func.now())
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0


def _to_info(row: ApiKey) -> ApiKeyInfo:
    return ApiKeyInfo(
        id=row.id,
        label=row.label,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        scopes=list(row.scopes),
    )


def build_api_key_store(
    settings: Settings, engine: AsyncEngine | None = None
) -> ApiKeyStore | None:
    """Build the API-key store named by ``settings.api_key_backend``.

    Returns ``None`` when backend is ``"null"`` — there is no zero-cost
    no-op store, so callers branch on ``None`` and fall back to the dev-key
    path (decision 7-key-null-store-as-none).
    """
    if settings.api_key_backend == "postgres":
        if engine is None:
            raise ValueError(
                "PostgresApiKeyStore requires an engine; the lifespan must call "
                "build_engine(settings) when api_key_backend='postgres'."
            )
        return PostgresApiKeyStore(engine)
    return None


def get_api_key_store(request: Request) -> ApiKeyStore | None:
    """FastAPI dependency: returns the store bound to ``app.state`` by the
    lifespan, or ``None`` when ``api_key_backend == "null"``. Slice 2's
    ``require_api_key`` consumes this dep and branches on ``None``."""
    return request.app.state.api_key_store


def audit_id_for(info: ApiKeyInfo | str | None) -> str | None:
    """Convert ``require_api_key``'s return value into the audit row's
    ``api_key_id`` field.

    - ``None`` (auth disabled) → ``None``.
    - ``"dev"`` (dev-shim sentinel) → ``"dev"`` unchanged.
    - ``ApiKeyInfo`` (store-verified) → ``str(info.id)``.
    """
    if info is None:
        return None
    if isinstance(info, str):
        return info
    return str(info.id)