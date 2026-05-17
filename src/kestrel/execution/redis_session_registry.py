from __future__ import annotations

import asyncio
import functools
import uuid
from collections.abc import Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from typing import Any, ParamSpec, TypeVar

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from kestrel.config import Settings
from kestrel.execution.session_registry import (
    RegistryUnavailable,
    SessionBusy,
    SessionInfo,
    SessionNotFound,
    _Entry,
    _utcnow,
)
from kestrel.execution.session_runtime import SessionRuntime

_logger = structlog.get_logger()

_P = ParamSpec("_P")
_R = TypeVar("_R")

_INDEX_KEY = "sessions"  # Redis SET holding every live session id


def _session_key(session_id: str) -> str:
    """Redis key for one session's metadata hash."""
    return f"session:{session_id}"


def _redis_errors_to_unavailable(
      method: Callable[_P, Coroutine[Any, Any, _R]],
  ) -> Callable[_P, Coroutine[Any, Any, _R]]:
    """Decorator — convert any ``RedisError`` from ``method`` into
    ``RegistryUnavailable``.

    Keeps the locked Redis-down → HTTP 503 contract (substep-7 Decision 3) in
    one place instead of repeating the same try/except in every method that
    talks to Redis.
    """

    @functools.wraps(method)
    async def _wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return await method(*args, **kwargs)
        except RedisError as exc:
            raise RegistryUnavailable(f"redis unavailable: {exc}") from exc

    return _wrapper


class RedisSessionRegistry:
    """Multi-worker session registry: a shared directory in Redis, with each
    worker holding the live runtimes for the sessions it created.

    Architecture (substep-7 Decision A — sticky routing):

    * Redis stores only *serializable* metadata — a hash per session
    (``session:<id>`` → created_at / last_used / owner_worker_id) plus a
    ``sessions`` SET as the listing index. It never holds a ``SessionRuntime``;
    a runtime is OS pipes to a container and cannot leave the process that
    opened them.
    * Each worker keeps ``self._sessions`` — an in-process map of the sessions
    *it* owns. ``acquire_for_execute`` / ``get_runtime`` only ever touch this
    local map; an execute for a session owned elsewhere raises
    ``SessionNotFound`` (sticky routing is expected to send it to the owner).
    * ``list`` / ``get_info`` read Redis, so they see *every* worker's sessions.
    * ``delete`` removes the Redis entry immediately (any worker may do this);
    the owning worker's sweeper later reconciles by closing the now-orphaned
    container.
    * A Redis TTL on each hash, refreshed on create/execute, is a backstop that
    garbage-collects the directory entries of a worker that crashed.

    The per-session ``asyncio.Lock`` and the warm pool stay in-process exactly
    as in ``InMemorySessionRegistry`` — sticky routing means only the owning
    worker executes a session, so neither needs to be distributed.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._worker_id = uuid.uuid4().hex
        self._sessions: dict[str, _Entry] = {}
        self._client: Redis | None = None
        self._sweeper_task: asyncio.Task[None] | None = None
        self._closed = False
        self._pool: list[SessionRuntime] = []
        self._refill_tasks: set[asyncio.Task[None]] = set()
        # TTL backstop: comfortably longer than the idle timeout so a live but
        # idle session is never dropped from Redis before the sweeper evicts it.
        self._redis_ttl_seconds = max(
            int(settings.session_idle_timeout_seconds * 2),
            int(settings.session_sweep_interval_seconds * 2),
        )

    # ──────────────────── public API ────────────────────

    @_redis_errors_to_unavailable
    async def create(self) -> SessionInfo:
        """Spawn (or check out from the pool) a runtime, write the directory
        entry to Redis stamped with this worker's id, keep the runtime locally."""
        if self._closed:
            raise RuntimeError("registry is closed")
        assert self._client is not None

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
        key = _session_key(session_id)
        mapping = {
            "created_at": now.isoformat(),
            "last_used": now.isoformat(),
            "owner_worker_id": self._worker_id,
        }

        try:
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.hset(key, mapping=mapping)
                pipe.sadd(_INDEX_KEY, session_id)
                pipe.expire(key, self._redis_ttl_seconds)
                await pipe.execute()
        except RedisError:
            # Don't leak the container we just spawned if the directory write
            # fails — close it, then let the decorator surface RegistryUnavailable.
            await runtime.close()
            raise

        self._sessions[session_id] = _Entry(info=info, runtime=runtime)
        _logger.info(
            "session_created",
            session_id_prefix=session_id[:8],
            owner_worker_id_prefix=self._worker_id[:8],
            local_sessions=len(self._sessions),
            from_pool=from_pool,
        )
        return info

    def get_runtime(self, session_id: str) -> SessionRuntime:
        """Look up a runtime this worker owns and bump its *local* last_used.

        Sync + local-only on purpose: a runtime always lives in the worker that
        created it. The Redis copy of last_used is refreshed by
        ``acquire_for_execute`` (the real execute path); ``get_runtime`` is a
        convenience/test accessor and does not round-trip to Redis.
        """
        entry = self._sessions.get(session_id)
        if entry is None:
            raise SessionNotFound(session_id)
        entry.info = replace(entry.info, last_used=_utcnow())
        return entry.runtime

    @_redis_errors_to_unavailable
    async def get_info(self, session_id: str) -> SessionInfo:
        """Read one session's metadata from Redis — works for any worker's session."""
        assert self._client is not None
        data = await self._client.hgetall(_session_key(session_id)) # type: ignore[misc]
        if not data:
            raise SessionNotFound(session_id)
        return SessionInfo(
            session_id=session_id,
            created_at=datetime.fromisoformat(data["created_at"]),
            last_used=datetime.fromisoformat(data["last_used"]),
        )

    @_redis_errors_to_unavailable
    async def list(self) -> list[SessionInfo]:
        """Snapshot of every live session across every worker (reads Redis)."""
        assert self._client is not None
        session_ids = list(await self._client.smembers(_INDEX_KEY)) # type: ignore[misc]
        if not session_ids:
            return []
        async with self._client.pipeline(transaction=False) as pipe:
            for sid in session_ids:
                pipe.hgetall(_session_key(sid))
            results = await pipe.execute()
        infos: list[SessionInfo] = []
        for sid, data in zip(session_ids, results):
            if not data:
                continue  # id in the index but hash already gone — harmless race
            infos.append(
                SessionInfo(
                    session_id=sid,
                    created_at=datetime.fromisoformat(data["created_at"]),
                    last_used=datetime.fromisoformat(data["last_used"]),
                )
            )
        return infos

    @asynccontextmanager
    async def acquire_for_execute(self, session_id: str):
        """Acquire the per-session execute lock and yield the live runtime.

        The session must be owned by *this* worker (sticky routing). Refreshes
        last_used + the TTL backstop in Redis before yielding; raises
        ``SessionNotFound`` if not owned here, ``SessionBusy`` if an execute is
        already in flight, ``RegistryUnavailable`` if Redis is unreachable.
        """
        entry = self._sessions.get(session_id)
        if entry is None:
            raise SessionNotFound(session_id)
        if entry.lock.locked():
            raise SessionBusy(session_id)

        now = _utcnow()
        assert self._client is not None
        key = _session_key(session_id)
        try:
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.hset(key, "last_used", now.isoformat())
                pipe.expire(key, self._redis_ttl_seconds)
                await pipe.execute()
        except RedisError as exc:
            raise RegistryUnavailable(f"redis unavailable: {exc}") from exc

        entry.info = replace(entry.info, last_used=now)
        async with entry.lock:
            yield entry.runtime

    @_redis_errors_to_unavailable
    async def delete(self, session_id: str) -> None:
        """Remove the directory entry from Redis (any worker may do this). If
        this worker owns the runtime, close its container now; otherwise the
        owning worker's sweeper will reconcile."""
        assert self._client is not None
        key = _session_key(session_id)
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.exists(key)
            pipe.delete(key)
            pipe.srem(_INDEX_KEY, session_id)
            existed, _, _ = await pipe.execute()
        if not existed:
            raise SessionNotFound(session_id)

        entry = self._sessions.pop(session_id, None)
        if entry is not None:
            await entry.runtime.close()
        _logger.info(
            "session_deleted",
            session_id_prefix=session_id[:8],
            owned_locally=entry is not None,
        )

    # ──────────────────── lifecycle ────────────────────

    async def start(self) -> None:
        """Connect to Redis, verify reachability, spawn the sweeper, warm the pool.

        Raises ``RuntimeError`` if called after ``aclose()``;
        ``RegistryUnavailable`` if Redis cannot be reached. Idempotent on an
        already-running registry.
        """
        if self._closed:
            raise RuntimeError("registry is closed")
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return

        self._client = Redis.from_url(self._settings.redis_url, decode_responses=True)
        try:
            await self._client.ping() # type: ignore[misc]
        except RedisError as exc:
            await self._client.aclose()
            self._client = None
            raise RegistryUnavailable(f"cannot reach redis: {exc}") from exc

        self._sweeper_task = asyncio.create_task(self._sweep_loop())
        for _ in range(self._settings.session_pool_size):
            self._schedule_refill()
        _logger.info(
            "redis_registry_started",
            worker_id_prefix=self._worker_id[:8],
            ttl_seconds=self._redis_ttl_seconds,
        )

    async def aclose(self) -> None:
        """Cancel the sweeper, wait for in-flight refills, remove this worker's
        directory entries from Redis (best-effort), close every live runtime,
        and disconnect from Redis. Idempotent."""
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

        # Best-effort: drop our directory entries so we don't leave ghosts.
        if self._client is not None and self._sessions:
            try:
                async with self._client.pipeline(transaction=False) as pipe:
                    for sid in self._sessions:
                        pipe.delete(_session_key(sid))
                        pipe.srem(_INDEX_KEY, sid)
                    await pipe.execute()
            except RedisError:
                _logger.warning("redis_aclose_cleanup_failed")

        runtimes = [e.runtime for e in self._sessions.values()] + list(self._pool)
        self._sessions.clear()
        self._pool.clear()
        if runtimes:
            await asyncio.gather(
                *(rt.close() for rt in runtimes), return_exceptions=True
            )

        if self._client is not None:
            try:
                await self._client.aclose()
            except RedisError:
                pass
            self._client = None

    # ──────────────────── private ────────────────────

    def _schedule_refill(self) -> None:
        """Fire-and-forget: schedule one background pool-refill task.

        No-op when closing or when the pool feature is disabled (size 0).
        Identical in spirit to ``InMemorySessionRegistry`` — the pool is
        per-worker and never touches Redis.
        """
        if self._closed:
            return
        if self._settings.session_pool_size == 0:
            return
        task = asyncio.create_task(self._refill_one())
        self._refill_tasks.add(task)
        task.add_done_callback(self._refill_tasks.discard)

    async def _refill_one(self) -> None:
        """Spawn one runtime into the pool, unless the pool is already full or
        the registry is closing. Double-checks around the spawn await to handle
        shutdown / overshoot races."""
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
        if self._closed or len(self._pool) >= self._settings.session_pool_size:
            await runtime.close()
            return
        self._pool.append(runtime)
        _logger.info("pool_refilled", pool_size=len(self._pool))

    async def _sweep_loop(self) -> None:
        """Periodic eviction + reconciliation loop. Cancelled by ``aclose()``."""
        interval = self._settings.session_sweep_interval_seconds
        timeout = self._settings.session_idle_timeout_seconds
        try:
            while True:
                await asyncio.sleep(interval)
                await self._sweep_once(timeout_seconds=timeout)
        except asyncio.CancelledError:
            return

    async def _sweep_once(self, timeout_seconds: float) -> None:
        """One pass over this worker's local sessions:

        * **reconcile** — a session whose Redis entry has vanished (another
        worker called ``delete``, or the TTL expired) → close the orphaned
        container, drop it locally.
        * **evict** — a session still in Redis but idle past the threshold →
        close the container *and* remove its Redis directory entry.

        Best-effort: a Redis hiccup logs and ends the pass rather than killing
        the background task.
        """
        if self._client is None:
            return
        local = list(self._sessions.items())
        if not local:
            return

        try:
            async with self._client.pipeline(transaction=False) as pipe:
                for sid, _ in local:
                    pipe.exists(_session_key(sid))
                exists_flags = await pipe.execute()
        except RedisError:
            _logger.warning("redis_sweep_check_failed")
            return

        now = _utcnow()
        reconcile_gone: list[tuple[str, SessionRuntime]] = []
        evict_idle: list[tuple[str, SessionRuntime]] = []
        for (sid, entry), exists in zip(local, exists_flags):
            if not exists:
                reconcile_gone.append((sid, entry.runtime))
                continue
            idle = (now - entry.info.last_used).total_seconds()
            if idle > timeout_seconds:
                evict_idle.append((sid, entry.runtime))

        for sid, runtime in reconcile_gone:
            self._sessions.pop(sid, None)
            try:
                await runtime.close()
            except Exception:
                pass
            _logger.info("session_reconciled_gone", session_id_prefix=sid[:8])

        for sid, runtime in evict_idle:
            self._sessions.pop(sid, None)
            try:
                await runtime.close()
            except Exception:
                pass
            try:
                async with self._client.pipeline(transaction=True) as pipe:
                    pipe.delete(_session_key(sid))
                    pipe.srem(_INDEX_KEY, sid)
                    await pipe.execute()
            except RedisError:
                _logger.warning(
                    "redis_evict_cleanup_failed", session_id_prefix=sid[:8]
                )
            _logger.info("session_evicted_idle", session_id_prefix=sid[:8])