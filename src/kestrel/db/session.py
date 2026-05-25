from __future__ import annotations

"""Async SQLAlchemy engine + sessionmaker factory.

Single construction point for every DB-touching component (audit sink in
Phase 7 substep 2, API-key store in substep 3, rate-limit persistence if it
ever moves off Redis). Each create_app() call builds one engine and threads
it through the lifespan to the components that need it. The engine owns the
asyncpg pool; closing it on shutdown is what releases the pool.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from kestrel.config import Settings


def build_engine(settings: Settings) -> AsyncEngine:
    """Build an async SQLAlchemy engine pointed at ``settings.database_url``.

    The engine lazily opens an asyncpg connection pool on first use. Caller
    owns lifecycle: ``await engine.dispose()`` on shutdown.
    """
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        future=True,
    )


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build an ``async_sessionmaker`` bound to the given engine.

    ``expire_on_commit=False`` because audit rows are write-only — we never
    re-read attributes off a committed object so the default expiry would
    just trigger pointless reloads.
    """
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )