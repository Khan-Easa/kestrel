from __future__ import annotations

import asyncio

import pytest

from kestrel.config import Settings
from kestrel.execution.redis_session_registry import RedisSessionRegistry
from kestrel.execution.session_registry import (
    RegistryUnavailable,
    SessionBusy,
    SessionInfo,
    SessionNotFound,
)


async def test_create_writes_directory_entry_to_redis(redis_session_registry_factory):
    """create() spawns a runtime locally AND writes a session:<id> hash + a
    `sessions` set member, stamped with this worker's id and given a TTL."""
    registry = await redis_session_registry_factory()
    info = await registry.create()
    sid = info.session_id

    client = registry._client
    assert await client.sismember("sessions", sid)
    data = await client.hgetall(f"session:{sid}")
    assert data["owner_worker_id"] == registry._worker_id
    assert data["created_at"] == info.created_at.isoformat()
    ttl = await client.ttl(f"session:{sid}")
    assert 0 < ttl <= registry._redis_ttl_seconds
    assert sid in registry._sessions  # runtime held locally


async def test_get_info_reads_from_redis(redis_session_registry_factory):
    """get_info() returns a SessionInfo built from the Redis hash."""
    registry = await redis_session_registry_factory()
    created = await registry.create()

    info = await registry.get_info(created.session_id)
    assert isinstance(info, SessionInfo)
    assert info.session_id == created.session_id
    assert info.created_at == created.created_at


async def test_list_returns_all_sessions(redis_session_registry_factory):
    """list() reads the `sessions` index and returns every live session."""
    registry = await redis_session_registry_factory()
    a = await registry.create()
    b = await registry.create()
    c = await registry.create()

    ids = {s.session_id for s in await registry.list()}
    assert ids == {a.session_id, b.session_id, c.session_id}


async def test_delete_removes_redis_entry_and_closes_runtime(redis_session_registry_factory):
    """delete() drops the Redis hash + index member and closes the local runtime."""
    registry = await redis_session_registry_factory()
    info = await registry.create()
    sid = info.session_id
    runtime = registry.get_runtime(sid)

    await registry.delete(sid)

    client = registry._client
    assert await client.exists(f"session:{sid}") == 0
    assert not await client.sismember("sessions", sid)
    assert runtime._terminated is True
    assert sid not in registry._sessions


async def test_unknown_id_raises_session_not_found(redis_session_registry_factory):
    """Every lookup path raises SessionNotFound for an id absent from Redis
    (or not owned locally)."""
    registry = await redis_session_registry_factory()

    with pytest.raises(SessionNotFound):
        await registry.get_info("does-not-exist")
    with pytest.raises(SessionNotFound):
        await registry.delete("does-not-exist")
    with pytest.raises(SessionNotFound):
        registry.get_runtime("does-not-exist")
    with pytest.raises(SessionNotFound):
        async with registry.acquire_for_execute("does-not-exist"):
            pass


async def test_acquire_for_execute_runs_code(redis_session_registry_factory):
    """acquire_for_execute() yields the live runtime; code runs in it."""
    registry = await redis_session_registry_factory()
    info = await registry.create()

    async with registry.acquire_for_execute(info.session_id) as runtime:
        result = await runtime.execute("print(2 + 2)")
    assert result.exit_code == 0
    assert result.stdout.strip() == "4"


async def test_acquire_for_execute_rejects_concurrent(redis_session_registry_factory):
    """A second acquire while the lock is held raises SessionBusy."""
    registry = await redis_session_registry_factory()
    info = await registry.create()

    async with registry.acquire_for_execute(info.session_id):
        with pytest.raises(SessionBusy):
            async with registry.acquire_for_execute(info.session_id):
                pass


async def test_state_persists_across_executes(redis_session_registry_factory):
    """§6.4 acceptance via the Redis backend: a variable set in one execute
    survives into a later execute in the same session."""
    registry = await redis_session_registry_factory()
    info = await registry.create()

    async with registry.acquire_for_execute(info.session_id) as runtime:
        first = await runtime.execute("x = 41")
    assert first.exit_code == 0

    async with registry.acquire_for_execute(info.session_id) as runtime:
        second = await runtime.execute("print(x + 1)")
    assert second.exit_code == 0
    assert second.stdout.strip() == "42"


async def test_acquire_refreshes_last_used_in_redis(redis_session_registry_factory):
    """acquire_for_execute() bumps last_used in the Redis hash."""
    registry = await redis_session_registry_factory()
    info = await registry.create()
    sid = info.session_id
    before = (await registry.get_info(sid)).last_used

    await asyncio.sleep(0.01)
    async with registry.acquire_for_execute(sid) as runtime:
        await runtime.execute("pass")

    after = (await registry.get_info(sid)).last_used
    assert after > before


async def test_sweep_evicts_idle_and_cleans_redis(redis_session_registry_factory):
    """_sweep_once with an aggressive threshold evicts the local session AND
    removes its Redis directory entry."""
    registry = await redis_session_registry_factory()
    info = await registry.create()
    sid = info.session_id

    await registry._sweep_once(timeout_seconds=-1.0)

    assert sid not in registry._sessions
    client = registry._client
    assert await client.exists(f"session:{sid}") == 0
    assert not await client.sismember("sessions", sid)


async def test_cross_worker_list_visibility(redis_session_registry_factory):
    """A session created on one worker is visible via list()/get_info() on
    another worker sharing the same Redis — but the runtime stays local to
    its owner."""
    worker_a = await redis_session_registry_factory()
    worker_b = await redis_session_registry_factory()
    assert worker_a._worker_id != worker_b._worker_id

    info = await worker_a.create()

    ids = {s.session_id for s in await worker_b.list()}
    assert info.session_id in ids
    assert (await worker_b.get_info(info.session_id)).session_id == info.session_id
    assert info.session_id not in worker_b._sessions


async def test_cross_worker_delete_then_reconcile(redis_session_registry_factory):
    """worker_b deletes a session worker_a owns: the directory entry vanishes
    immediately, and worker_a's next sweep reconciles by closing the now-
    orphaned local runtime."""
    worker_a = await redis_session_registry_factory()
    worker_b = await redis_session_registry_factory()

    info = await worker_a.create()
    sid = info.session_id
    runtime = worker_a.get_runtime(sid)

    await worker_b.delete(sid)
    assert await worker_b._client.exists(f"session:{sid}") == 0
    assert sid in worker_a._sessions  # worker_a hasn't noticed yet

    await worker_a._sweep_once(timeout_seconds=900.0)  # generous — not idle-evict
    assert sid not in worker_a._sessions  # reconciled away
    assert runtime._terminated is True


async def test_aclose_removes_directory_entries(
    redis_session_registry_factory, redis_inspector
):
    """aclose() drops this worker's directory entries from Redis so no ghosts
    remain after shutdown."""
    registry = await redis_session_registry_factory()
    a = await registry.create()
    b = await registry.create()

    await registry.aclose()

    assert await redis_inspector.exists(f"session:{a.session_id}") == 0
    assert await redis_inspector.exists(f"session:{b.session_id}") == 0
    assert await redis_inspector.scard("sessions") == 0


async def test_start_raises_registry_unavailable_on_bad_url():
    """start() against an unreachable Redis raises RegistryUnavailable
    (Decision 3 — fail hard, no fallback). Needs nothing running."""
    registry = RedisSessionRegistry(
        Settings(session_backend="redis", redis_url="redis://localhost:6390/0")
    )
    with pytest.raises(RegistryUnavailable):
        await registry.start()