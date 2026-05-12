from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import structlog

from kestrel.config import Settings
from kestrel.execution.session_runtime import SessionRuntime

_logger = structlog.get_logger()


class SessionNotFound(KeyError):
    """Raised when a session_id is not present in the registry."""


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Public metadata about a session — what list() and create() return."""

    session_id: str
    created_at: datetime
    last_used: datetime


@dataclass(slots=True)
class _Entry:
    """Private — pairs the public info with the live runtime handle."""

    info: SessionInfo
    runtime: SessionRuntime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionRegistry:
    """In-memory map of session_id → SessionRuntime, with background idle eviction.

    Lifecycle: ``__init__`` is cheap and sync. Call ``await start()`` to
    spin up the background sweeper task; ``await aclose()`` to cancel the
    sweeper and close every live runtime. Both are idempotent.

    Concurrency: callers do not need locks — asyncio is single-threaded,
    mutations happen at well-defined await boundaries, and the sweeper
    iterates a *snapshot* of the dict so create/delete during a pass
    cannot raise ``RuntimeError``. ``SessionRuntime.close()`` is
    idempotent (substep 3 contract), so a delete/sweep race is safe.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sessions: dict[str, _Entry] = {}
        self._sweeper_task: asyncio.Task[None] | None = None
        self._closed = False

    # ──────────────────── public API ────────────────────

    async def create(self) -> SessionInfo:
        """Spawn a fresh SessionRuntime, register it, return its public info."""
        if self._closed:
            raise RuntimeError("registry is closed")

        runtime = await SessionRuntime.start(
            image_tag=self._settings.executor_docker_image,
            timeout_seconds=self._settings.execute_timeout_seconds,
        )
        now = _utcnow()
        session_id = uuid.uuid4().hex
        info = SessionInfo(session_id=session_id, created_at=now, last_used=now)
        self._sessions[session_id] = _Entry(info=info, runtime=runtime)
        _logger.info(
            "session_created",
            session_id_prefix=session_id[:8],
            total_sessions=len(self._sessions),
        )
        return info

    def get_runtime(self, session_id: str) -> SessionRuntime:
        """Look up the live runtime and bump ``last_used``.

        Raises ``SessionNotFound`` if the id is unknown.
        """
        entry = self._sessions.get(session_id)
        if entry is None:
            raise SessionNotFound(session_id)
        entry.info = replace(entry.info, last_used=_utcnow())
        return entry.runtime

    def get_info(self, session_id: str) -> SessionInfo:
        """Return the public metadata without touching ``last_used``."""
        entry = self._sessions.get(session_id)
        if entry is None:
            raise SessionNotFound(session_id)
        return entry.info

    def list(self) -> list[SessionInfo]:
        """Snapshot of every live session's metadata. Order is unspecified."""
        return [entry.info for entry in self._sessions.values()]

    async def delete(self, session_id: str) -> None:
        """Close the runtime and drop the entry. Raises ``SessionNotFound`` if absent."""
        entry = self._sessions.pop(session_id, None)
        if entry is None:
            raise SessionNotFound(session_id)
        await entry.runtime.close()
        _logger.info(
            "session_deleted",
            session_id_prefix=session_id[:8],
            remaining_sessions=len(self._sessions),
        )

    # ──────────────────── lifecycle ────────────────────

    async def start(self) -> None:
        """Spawn the background sweeper task. Idempotent on already-running.

        Raises ``RuntimeError`` if called after ``aclose()``.
        """
        if self._closed:
            raise RuntimeError("registry is closed")
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        self._sweeper_task = asyncio.create_task(self._sweep_loop())

    async def aclose(self) -> None:
        """Cancel the sweeper, close every live runtime concurrently. Idempotent."""
        if self._closed:
            return
        self._closed = True

        if self._sweeper_task is not None and not self._sweeper_task.done():
            self._sweeper_task.cancel()
            try:
                await self._sweeper_task
            except asyncio.CancelledError:
                pass

        runtimes = [entry.runtime for entry in self._sessions.values()]
        self._sessions.clear()
        if runtimes:
            await asyncio.gather(
                *(rt.close() for rt in runtimes),
                return_exceptions=True,
            )

    # ──────────────────── private ────────────────────

    async def _sweep_loop(self) -> None:
        """Periodic eviction loop. Cancelled by ``aclose()``."""
        interval = self._settings.session_sweep_interval_seconds
        timeout = self._settings.session_idle_timeout_seconds
        try:
            while True:
                await asyncio.sleep(interval)
                await self._sweep_once(timeout_seconds=timeout)
        except asyncio.CancelledError:
            return

    async def _sweep_once(self, timeout_seconds: float) -> None:
        """One eviction pass: snapshot the dict, close entries idle past the threshold."""
        now = _utcnow()
        expired: list[tuple[str, SessionRuntime]] = []
        for session_id, entry in list(self._sessions.items()):
            idle = (now - entry.info.last_used).total_seconds()
            if idle > timeout_seconds:
                expired.append((session_id, entry.runtime))

        for session_id, runtime in expired:
            self._sessions.pop(session_id, None)
            try:
                await runtime.close()
            except Exception:
                pass
            _logger.info(
                "session_evicted_idle",
                session_id_prefix=session_id[:8],
                remaining_sessions=len(self._sessions),
            )