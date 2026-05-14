from __future__ import annotations

import asyncio

import pytest

from kestrel.execution.session_registry import (
    SessionInfo,
    SessionNotFound,
)


async def test_create_returns_session_info(session_registry_factory):
    """create() returns a SessionInfo with a UUID4-hex id, equal
    created_at and last_used timestamps (UTC-aware), and the registry
    contains exactly one entry afterwards."""
    registry = await session_registry_factory()

    info = await registry.create()

    assert isinstance(info, SessionInfo)
    assert len(info.session_id) == 32  # uuid4().hex
    assert info.created_at == info.last_used
    assert info.created_at.tzinfo is not None
    assert len(await registry.list()) == 1


async def test_get_runtime_bumps_last_used_but_get_info_does_not(session_registry_factory):
    """Decision 4-return: get_runtime() touches last_used (session in active
    use); get_info() leaves last_used alone (ops/listing path)."""
    registry = await session_registry_factory()
    info = await registry.create()
    sid = info.session_id

    # get_info is read-only — last_used should not move.
    before_info = (await registry.get_info(sid)).last_used
    await asyncio.sleep(0.01)
    after_info = (await registry.get_info(sid)).last_used
    assert after_info == before_info

    # get_runtime bumps last_used forward.
    before_runtime = (await registry.get_info(sid)).last_used
    await asyncio.sleep(0.01)
    registry.get_runtime(sid)
    after_runtime = (await registry.get_info(sid)).last_used
    assert after_runtime > before_runtime


async def test_list_returns_snapshot(session_registry_factory):
    """Decision 4-concur: list() returns a fresh list — mutating it must
    not affect the registry's internal state."""
    registry = await session_registry_factory()
    a = await registry.create()
    b = await registry.create()
    c = await registry.create()

    snapshot = await registry.list()
    assert {s.session_id for s in snapshot} == {a.session_id, b.session_id, c.session_id}

    snapshot.pop()  # mutate the returned list
    assert len(await registry.list()) == 3 # registry unaffected


async def test_delete_removes_and_closes_runtime(session_registry_factory):
    """delete() pops the entry, closes the runtime, and subsequent lookups
    raise SessionNotFound."""
    registry = await session_registry_factory()
    info = await registry.create()
    runtime = registry.get_runtime(info.session_id)

    await registry.delete(info.session_id)

    assert await registry.list() == []
    assert runtime._terminated is True
    with pytest.raises(SessionNotFound):
        registry.get_runtime(info.session_id)


async def test_session_not_found_raises_on_unknown_id(session_registry_factory):
    """All three lookup paths raise SessionNotFound for an unknown id."""
    registry = await session_registry_factory()

    with pytest.raises(SessionNotFound):
        registry.get_runtime("does-not-exist")
    with pytest.raises(SessionNotFound):
        await registry.get_info("does-not-exist")
    with pytest.raises(SessionNotFound):
        await registry.delete("does-not-exist")


async def test_sweep_evicts_only_idle_sessions(session_registry_factory):
    """Decision 4-evict: _sweep_once() evicts entries idle longer than
    the threshold, preserves the rest, and is safe to call on an empty
    registry."""
    registry = await session_registry_factory()
    await registry.create()
    await registry.create()

    # Generous threshold — nothing has been idle long enough.
    await registry._sweep_once(timeout_seconds=900.0)
    assert len(await registry.list()) == 2

    # Negative threshold — every session's idle (>= 0) exceeds it.
    await registry._sweep_once(timeout_seconds=-1.0)
    assert await registry.list() == []

    # Empty-registry sweep is a no-op.
    await registry._sweep_once(timeout_seconds=-1.0)


async def test_aclose_is_idempotent_and_closes_all_runtimes(session_registry_factory):
    """aclose() tears down every live runtime and is safe to call twice."""
    registry = await session_registry_factory()
    a = await registry.create()
    b = await registry.create()
    runtime_a = registry.get_runtime(a.session_id)
    runtime_b = registry.get_runtime(b.session_id)

    await registry.aclose()

    assert await registry.list() == []
    assert runtime_a._terminated is True
    assert runtime_b._terminated is True

    # Second aclose() is a no-op, not an error.
    await registry.aclose()


async def test_start_after_aclose_raises(session_registry_factory):
    """aclose() is terminal: start() afterwards raises RuntimeError."""
    registry = await session_registry_factory()
    await registry.start()
    await registry.aclose()

    with pytest.raises(RuntimeError, match="closed"):
        await registry.start()


async def test_state_persists_across_get_runtime_calls(session_registry_factory):
    """§6.4 acceptance via the registry: a variable defined in one
    execute() call survives into a later execute() reached through a
    second get_runtime() lookup, and get_runtime returns the same handle."""
    registry = await session_registry_factory()
    info = await registry.create()

    runtime = registry.get_runtime(info.session_id)
    setup = await runtime.execute("x = 41")
    assert setup.exit_code == 0

    runtime_again = registry.get_runtime(info.session_id)
    assert runtime_again is runtime  # registry returns the same live handle

    follow_up = await runtime_again.execute("print(x + 1)")
    assert follow_up.exit_code == 0
    assert follow_up.stdout.strip() == "42"

async def _wait_for_pool_warm(registry) -> None:
    """Helper — await every in-flight refill task to land."""
    pending = list(registry._refill_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_pool_warm_fills_on_start(session_registry_factory):
    """With session_pool_size=2, start() schedules two background refills
    that bring the pool to size 2."""
    registry = await session_registry_factory(session_pool_size=2)
    assert registry._pool == []

    await registry.start()
    await _wait_for_pool_warm(registry)

    assert len(registry._pool) == 2


async def test_create_pops_from_pool_and_schedules_refill(session_registry_factory):
    """create() takes a runtime from the pool (no fresh docker spawn) and queues
    a refill task that returns the pool to its target size."""
    registry = await session_registry_factory(session_pool_size=2)
    await registry.start()
    await _wait_for_pool_warm(registry)
    assert len(registry._pool) == 2

    info = await registry.create()
    assert info.session_id  # got a valid session
    assert len(registry._pool) == 1  # one popped

    await _wait_for_pool_warm(registry)
    assert len(registry._pool) == 2  # refilled back to target


async def test_create_falls_back_to_fresh_spawn_when_pool_disabled(session_registry_factory):
    """The default session_pool_size=0 means no pool — every create() spawns
    a fresh runtime, _pool stays empty, no refill tasks are scheduled."""
    registry = await session_registry_factory(session_pool_size=0)
    await registry.start()

    info = await registry.create()

    assert info.session_id
    assert registry._pool == []
    assert registry._refill_tasks == set()


async def test_sweeper_ignores_pool_entries(session_registry_factory):
    """Pool entries live in _pool, not _sessions. The sweeper iterates _sessions
    only, so pool entries survive an aggressively-aged sweep pass."""
    registry = await session_registry_factory(session_pool_size=2)
    await registry.start()
    await _wait_for_pool_warm(registry)

    # Aggressive sweep — negative threshold makes every active session "expired".
    # Pool entries are not in _sessions, so they should NOT be touched.
    await registry._sweep_once(timeout_seconds=-1.0)

    assert await registry.list() == [] # _sessions was empty all along
    assert len(registry._pool) == 2  # pool untouched


async def test_aclose_drains_pool_and_active_sessions(session_registry_factory):
    """aclose() closes every live runtime — pool entries and active sessions both."""
    registry = await session_registry_factory(session_pool_size=2)
    await registry.start()
    await _wait_for_pool_warm(registry)
    pool_runtimes_before = list(registry._pool)

    info = await registry.create()
    active_runtime = registry.get_runtime(info.session_id)

    await registry.aclose()

    # Active session runtime closed
    assert active_runtime._terminated is True
    # Pool runtimes (those originally in pool) closed
    for rt in pool_runtimes_before:
        assert rt._terminated is True
    # Registry state cleared
    assert await registry.list() == []
    assert registry._pool == []


async def test_aclose_waits_for_pending_refills(session_registry_factory):
    """aclose() called immediately after start() (while refills are still spawning)
    awaits the pending tasks before returning, so no spawn task is abandoned."""
    registry = await session_registry_factory(session_pool_size=2)
    await registry.start()
    # Don't wait for pool warm — close immediately.

    await registry.aclose()

    # By the time aclose() returns, every refill task is done.
    # (Each task's add_done_callback removes itself from the set, so the set is empty.)
    assert registry._refill_tasks == set()