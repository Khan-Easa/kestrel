from __future__ import annotations

"""Phase 7 substep 6 slice 1: read-side DB queries for admin routes.

Per decision ``7.6-audit-query-loc``, audit read queries live here rather
than on ``PostgresAuditSink``. The sink is conceptually a write-only
pipeline (bounded queue + drain task); reads belong elsewhere.

Free functions in this module take a sessionmaker and return SA rows.
The router layer wraps results into pydantic response models. Future
admin queries (key activity counters, session usage stats) add here.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from kestrel.db.models import AuditEventRow


async def list_audit_events(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    limit: int,
    before_ts: datetime | None = None,
) -> list[AuditEventRow]:
    """Return the most-recent audit rows in ``ts DESC`` order.

    Decision ``7.6-audit-pagination``: cursor pagination via ``before_ts``.
    First page omits ``before_ts``; subsequent pages pass the ``ts`` of the
    last row from the previous page. ``ix_audit_events_ts`` serves the
    range scan in O(log n + limit).

    The caller (the route) is responsible for clamping ``limit`` to a safe
    range; this function trusts what it's given.
    """
    stmt = select(AuditEventRow).order_by(AuditEventRow.ts.desc()).limit(limit)
    if before_ts is not None:
        stmt = stmt.where(AuditEventRow.ts < before_ts)
    async with sessionmaker() as session:
        result = await session.execute(stmt)
        return list(result.scalars().all())