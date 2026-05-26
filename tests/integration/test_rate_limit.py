"""Phase 7 substep 5 slice 1: InMemoryRateLimiter unit tests.

No external dependencies — pure Python. Each test instantiates a fresh
limiter with a controllable clock so the refill behavior is deterministic
without ``asyncio.sleep`` calls.
"""

from __future__ import annotations

import pytest

from kestrel.config import Settings
from kestrel.rate_limit import (
    InMemoryRateLimiter,
    RateLimitDecision,
    build_rate_limiter,
)


class _Clock:
    """Monotonic-time stand-in for tests. ``now()`` returns the current
    virtual time; ``advance(seconds)`` moves it forward."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _settings(**overrides) -> Settings:
    defaults = {
        "rate_limit_execute_per_minute": 60,
        "rate_limit_session_lifecycle_per_minute": 300,
        "rate_limit_admin_per_minute": 60,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_build_rate_limiter_defaults_to_memory():
    limiter = build_rate_limiter(_settings())
    assert isinstance(limiter, InMemoryRateLimiter)


def test_build_rate_limiter_returns_memory_when_session_backend_redis_slice1():
    # Slice-1 placeholder: Redis backend ships in slice 2; until then
    # multi-worker setups get per-worker memory limiters (no shared state).
    settings = _settings(session_backend="redis")
    limiter = build_rate_limiter(settings)
    assert isinstance(limiter, InMemoryRateLimiter)


async def test_initial_bucket_is_full_for_each_route_class():
    limiter = InMemoryRateLimiter(_settings())
    for route_class in ("execute", "session_lifecycle", "admin"):
        decision = await limiter.check("alice", route_class)
        assert decision.allowed is True
        assert decision.retry_after_seconds == 0


async def test_burst_consumes_full_capacity():
    clock = _Clock()
    limiter = InMemoryRateLimiter(_settings(), time_source=clock.now)
    for _ in range(60):
        decision = await limiter.check("alice", "execute")
        assert decision.allowed is True


async def test_request_beyond_capacity_is_rejected():
    clock = _Clock()
    limiter = InMemoryRateLimiter(_settings(), time_source=clock.now)
    for _ in range(60):
        await limiter.check("alice", "execute")
    decision = await limiter.check("alice", "execute")
    assert decision.allowed is False
    assert decision.retry_after_seconds >= 1


async def test_bucket_refills_over_time():
    clock = _Clock()
    limiter = InMemoryRateLimiter(_settings(), time_source=clock.now)
    for _ in range(60):
        await limiter.check("alice", "execute")
    # bucket is empty; advance clock 30s → refill rate is 1/s → 30 tokens
    clock.advance(30.0)
    for i in range(30):
        decision = await limiter.check("alice", "execute")
        assert decision.allowed is True, f"request {i+1} of 30 should pass"
    decision = await limiter.check("alice", "execute")
    assert decision.allowed is False


async def test_refill_caps_at_capacity():
    clock = _Clock()
    limiter = InMemoryRateLimiter(_settings(), time_source=clock.now)
    for _ in range(60):
        await limiter.check("alice", "execute")
    # Wait WAY more than 60s — bucket should cap at 60, not accumulate beyond
    clock.advance(3600.0)
    for _ in range(60):
        assert (await limiter.check("alice", "execute")).allowed is True
    assert (await limiter.check("alice", "execute")).allowed is False


async def test_different_identities_have_independent_buckets():
    limiter = InMemoryRateLimiter(_settings())
    for _ in range(60):
        await limiter.check("alice", "execute")
    # alice is empty; bob is full
    decision = await limiter.check("bob", "execute")
    assert decision.allowed is True


async def test_different_route_classes_have_independent_buckets():
    limiter = InMemoryRateLimiter(_settings())
    for _ in range(60):
        await limiter.check("alice", "execute")
    # execute bucket empty; session_lifecycle (300/min, separate) still full
    decision = await limiter.check("alice", "session_lifecycle")
    assert decision.allowed is True


async def test_session_lifecycle_uses_300_limit():
    limiter = InMemoryRateLimiter(_settings())
    for _ in range(300):
        decision = await limiter.check("alice", "session_lifecycle")
        assert decision.allowed is True
    decision = await limiter.check("alice", "session_lifecycle")
    assert decision.allowed is False


async def test_unknown_route_class_raises():
    limiter = InMemoryRateLimiter(_settings())
    with pytest.raises(ValueError, match="unknown route_class"):
        await limiter.check("alice", "unknown")


async def test_retry_after_reflects_time_to_next_token():
    clock = _Clock()
    limiter = InMemoryRateLimiter(_settings(), time_source=clock.now)
    for _ in range(60):
        await limiter.check("alice", "execute")
    # bucket is empty; refill rate is 1/sec → next token in ~1s
    decision = await limiter.check("alice", "execute")
    assert decision.allowed is False
    assert decision.retry_after_seconds == 1


async def test_lifecycle_methods_are_no_ops_for_memory_backend():
    limiter = InMemoryRateLimiter(_settings())
    await limiter.start()
    await limiter.aclose()
    # Should still work after aclose — memory backend has no resources to release
    decision = await limiter.check("alice", "execute")
    assert decision.allowed is True