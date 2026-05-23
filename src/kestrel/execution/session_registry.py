from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field, replace
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

import structlog

from kestrel.observability import (
    POLLING_BUFFERS_ACTIVE,
    SESSIONS_ACTIVE,
    SESSION_POOL_SIZE,
)
from kestrel.config import Settings
from kestrel.execution.session_runtime import SessionRuntime

_logger = structlog.get_logger()


class SessionNotFound(KeyError):
    """Raised when a session_id is not present in the registry."""

class SessionBusy(Exception):
    """Raised when an execute is attempted on a session that's already running one."""

class RegistryUnavailable(Exception):
    """Raised when the registry's backing store (Redis) is unreachable.

    Maps to HTTP 503 (substep-7 Decision 3 — fail hard, no in-memory
    fallback). Only the Redis backend raises this; in-memory never does.
    """


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Public metadata about a session — what list() and create() return."""

    session_id: str
    created_at: datetime
    last_used: datetime


@dataclass(slots=True)
class _Entry:
    """Private — pairs the public info with the live runtime handle, plus a per-session execute lock."""

    info: SessionInfo
    runtime: SessionRuntime
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class PollingBuffer:
    """Append-only message buffer for one polling execute (Phase 6 substep 6).

    The polling POST handler creates one per execute; the polling orchestrator
    feeds it one entry per StreamMessage from execute_stream; long-poll GET
    handlers drain it from a caller-supplied cursor. The registry owns its
    lifecycle (TTL eviction in the sweep, bulk removal on session delete) but
    treats the messages as opaque objects — it never imports the api-layer
    StreamMessage type. See Decision 6.6-buffer.

    Cursor model (Decision 6.6-cursor): ``messages`` is append-only, the cursor
    is an integer index, a GET with ``since=N`` returns ``messages[N:]``.
    """

    messages: list = field(default_factory=list, repr=False)
    done: bool = False
    completed_at: datetime | None = None
    task: asyncio.Task[None] | None = None
    _cond: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)

    async def append(self, message: object) -> None:
        """Append one message and wake every waiting long-poll GET."""
        async with self._cond:
            self.messages.append(message)
            self._cond.notify_all()

    async def mark_done(self) -> None:
        """Flag the execute finished, stamp completion time, wake all GETs."""
        async with self._cond:
            self.done = True
            self.completed_at = _utcnow()
            self._cond.notify_all()

    async def wait_for_messages(self, since: int, timeout: float) -> None:
        """Block up to ``timeout`` s until ``messages[since:]`` is non-empty or
        the execute is done. Returns at once if either already holds or
        ``timeout <= 0`` (the short-poll degenerate case, Decision 6.6-mech)."""
        if timeout <= 0:
            return
        async with self._cond:
            try:
                await asyncio.wait_for(
                    self._cond.wait_for(
                        lambda: len(self.messages) > since or self.done
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                pass


def _evict_expired_polling_buffers(
    polling_buffers: dict[str, dict[str, PollingBuffer]],
    ttl_seconds: float,
) -> None:
    """Drop polling buffers whose execute completed more than ``ttl_seconds``
    ago (Decision 6.6-evict). Mutates ``polling_buffers`` in place; also drops
    a session's now-empty sub-dict. Shared by both registry backends' sweep
    passes — the polling buffer is purely in-process, identical logic
    regardless of session backend.
    """
    now = _utcnow()
    for session_id, by_execution in list(polling_buffers.items()):
        for execution_id, buffer in list(by_execution.items()):
            if (
                buffer.completed_at is not None
                and (now - buffer.completed_at).total_seconds() > ttl_seconds
            ):
                by_execution.pop(execution_id, None)
        if not by_execution:
            polling_buffers.pop(session_id, None)


def _drain_polling_buffers(
    polling_buffers: dict[str, dict[str, PollingBuffer]],
) -> None:
    """Cancel every in-flight polling orchestrator task and clear the buffer
    map. Called from a registry's ``aclose`` so shutdown leaves no pending
    tasks behind."""
    for by_execution in polling_buffers.values():
        for buffer in by_execution.values():
            if buffer.task is not None and not buffer.task.done():
                buffer.task.cancel()
    polling_buffers.clear()


@runtime_checkable
class SessionRegistry(Protocol):
    """Structural interface every session-registry backend satisfies.

    Two implementations: ``InMemorySessionRegistry`` (single-process, the
    default) and ``RedisSessionRegistry`` (shared directory + sticky routing
    across workers, Phase 4 substep 7). App wiring and routes depend on this
    Protocol, never on a concrete class — the implementation is chosen by
    ``Settings.session_backend`` via ``build_session_registry``.
    """

    async def create(self) -> SessionInfo: ...
    def get_runtime(self, session_id: str) -> SessionRuntime: ...
    async def get_info(self, session_id: str) -> SessionInfo: ...
    def acquire_for_execute(
        self, session_id: str
    ) -> AbstractAsyncContextManager[SessionRuntime]: ...
    async def list(self) -> list[SessionInfo]: ...
    async def delete(self, session_id: str) -> None: ...
    async def start(self) -> None: ...
    async def aclose(self) -> None: ...

    # Phase 6 substep 6 — per-execution polling buffers (Decision 6.6-buffer):
    # {session_id: {execution_id: PollingBuffer}}. In-process per worker
    # (Decision 6.6-sticky); the polling routes in sessions_polling.py read
    # and write it directly. Both backends initialise it to {} and clean it
    # up in delete(), the sweep, and aclose().
    _polling_buffers: dict[str, dict[str, PollingBuffer]]

class InMemorySessionRegistry:
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
        self._pool: list[SessionRuntime] = []
        self._refill_tasks: set[asyncio.Task[None]] = set()
        self._polling_buffers: dict[str, dict[str, PollingBuffer]] = {}

    def _refresh_metrics(self) -> None:
        """Phase 7 substep 1: refresh per-worker gauges from current state.

        Called at the tail of every method that mutates ``self._sessions``,
        ``self._pool``, or ``self._polling_buffers``. Cheap (three ``.set()``
        calls on float-backed gauges); avoids drift that hand-maintained
        inc/dec pairs would risk.
        """
        SESSIONS_ACTIVE.set(len(self._sessions))
        SESSION_POOL_SIZE.set(len(self._pool))
        POLLING_BUFFERS_ACTIVE.set(
            sum(len(by_execution) for by_execution in self._polling_buffers.values())
        )

    # ──────────────────── public API ────────────────────

    async def create(self) -> SessionInfo:
        """Spawn (or check out from the pool) a SessionRuntime, register it, return its public info."""
        if self._closed:
            raise RuntimeError("registry is closed")

        if self._pool:
            runtime = self._pool.pop()
            self._schedule_refill()
            from_pool = True
        else:
            runtime = await SessionRuntime.start(
                image_tag=self._settings.executor_docker_image,
                timeout_seconds=self._settings.execute_timeout_seconds,
                plot_max_bytes=self._settings.rich_output_plot_max_bytes,
                dataframe_max_bytes=self._settings.rich_output_dataframe_max_bytes,
                file_max_bytes=self._settings.rich_output_file_max_bytes,
                file_max_count=self._settings.rich_output_file_max_count,
                total_max_bytes=self._settings.rich_output_total_max_bytes,
            )
            from_pool = False

        now = _utcnow()
        session_id = uuid.uuid4().hex
        info = SessionInfo(session_id=session_id, created_at=now, last_used=now)
        self._sessions[session_id] = _Entry(info=info, runtime=runtime)
        _logger.info(
            "session_created",
            session_id_prefix=session_id[:8],
            total_sessions=len(self._sessions),
            from_pool=from_pool,
        )
        self._refresh_metrics()
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

    async def get_info(self, session_id: str) -> SessionInfo:
        """Return the public metadata without touching ``last_used``."""
        entry = self._sessions.get(session_id)
        if entry is None:
            raise SessionNotFound(session_id)
        return entry.info
    
    @asynccontextmanager
    async def acquire_for_execute(self, session_id: str):
        """Acquire the per-session execute lock and yield the live runtime.

        Bumps ``last_used`` (same as ``get_runtime``). Raises
        ``SessionNotFound`` if unknown; raises ``SessionBusy`` immediately
        if the lock is already held by another caller.
        """
        entry = self._sessions.get(session_id)
        if entry is None:
            raise SessionNotFound(session_id)
        if entry.lock.locked():
            raise SessionBusy(session_id)
        entry.info = replace(entry.info, last_used=_utcnow())
        async with entry.lock:
            yield entry.runtime

    async def list(self) -> list[SessionInfo]:
        """Snapshot of every live session's metadata. Order is unspecified."""
        return [entry.info for entry in self._sessions.values()]

    async def delete(self, session_id: str) -> None:
        """Close the runtime and drop the entry. Raises ``SessionNotFound`` if absent."""
        entry = self._sessions.pop(session_id, None)
        if entry is None:
            raise SessionNotFound(session_id)
        self._polling_buffers.pop(session_id, None)
        await entry.runtime.close()
        self._refresh_metrics()
        _logger.info(
            "session_deleted",
            session_id_prefix=session_id[:8],
            remaining_sessions=len(self._sessions),
        )

    # ──────────────────── lifecycle ────────────────────

    async def start(self) -> None:
        """Spawn the background sweeper task and schedule pool warm-fill. Idempotent on already-running.

        Raises ``RuntimeError`` if called after ``aclose()``. Returns immediately;
        pool refills happen in the background.
        """
        if self._closed:
            raise RuntimeError("registry is closed")
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        self._sweeper_task = asyncio.create_task(self._sweep_loop())

        for _ in range(self._settings.session_pool_size):
            self._schedule_refill()
        self._refresh_metrics()

    async def aclose(self) -> None:
        """Cancel the sweeper, wait for in-flight refills, close every live runtime
        (active sessions + pool) concurrently. Idempotent."""
        if self._closed:
            return
        self._closed = True

        if self._sweeper_task is not None and not self._sweeper_task.done():
            self._sweeper_task.cancel()
            try:
                await self._sweeper_task
            except asyncio.CancelledError:
                pass

        pending_refills = [t for t in self._refill_tasks if not t.done()]
        if pending_refills:
            await asyncio.gather(*pending_refills, return_exceptions=True)
        self._refill_tasks.clear()

        runtimes = (
            [entry.runtime for entry in self._sessions.values()]
            + list(self._pool)
        )
        self._sessions.clear()
        self._pool.clear()
        _drain_polling_buffers(self._polling_buffers)
        if runtimes:
            await asyncio.gather(
                *(rt.close() for rt in runtimes),
                return_exceptions=True,
            )
        self._refresh_metrics()

    # ──────────────────── private ────────────────────

    def _schedule_refill(self) -> None:
        """Fire-and-forget: spawn a background task to refill the pool by one entry.

        No-op when the registry is closing or the pool feature is disabled (size=0).
        """
        if self._closed:
            return
        if self._settings.session_pool_size == 0:
            return
        task = asyncio.create_task(self._refill_one())
        self._refill_tasks.add(task)
        task.add_done_callback(self._refill_tasks.discard)

    async def _refill_one(self) -> None:
        """Spawn one runtime and add to the pool, unless the pool is already full
        or the registry is closing. Used by both startup warm-fill and post-checkout refill."""
        if self._closed:
            return
        if len(self._pool) >= self._settings.session_pool_size:
            return
        try:
            runtime = await SessionRuntime.start(
                image_tag=self._settings.executor_docker_image,
                timeout_seconds=self._settings.execute_timeout_seconds,
                plot_max_bytes=self._settings.rich_output_plot_max_bytes,
                dataframe_max_bytes=self._settings.rich_output_dataframe_max_bytes,
                file_max_bytes=self._settings.rich_output_file_max_bytes,
                file_max_count=self._settings.rich_output_file_max_count,
                total_max_bytes=self._settings.rich_output_total_max_bytes,
            )
        except Exception:
            _logger.exception("pool_refill_failed")
            return
        if self._closed:
            await runtime.close()
            return
        if len(self._pool) >= self._settings.session_pool_size:
            await runtime.close()
            return
        self._pool.append(runtime)
        self._refresh_metrics()
        _logger.info("pool_refilled", pool_size=len(self._pool))

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
        """One eviction pass: snapshot the dict, close entries idle past the
        threshold, then drop polling buffers past their post-completion TTL."""
        _evict_expired_polling_buffers(
            self._polling_buffers, self._settings.polling_buffer_ttl_seconds
        )
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
        self._refresh_metrics()