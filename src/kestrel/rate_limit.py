from __future__ import annotations

"""Phase 7 substep 5: per-key rate limiting.

Two locks shape this module:

- ``7-rate-limit-dims``: token-bucket per ``(api_key_id, route_class)``;
three route_classes (``execute`` / ``session_lifecycle`` / ``admin``);
capacity = per-minute limit; refill = limit / 60 per second; rejection
→ HTTP 429 + ``Retry-After`` (WS → close 4429 per ``7.5-ws-close-code``).
- ``7-ratelimit-storage``: bucket storage follows ``KESTREL_SESSION_BACKEND``
(memory or Redis — no new env knob). Slice 1 ships the memory backend
and the Protocol; slice 2 adds the Redis backend; slice 3 wires the
limiter into the HTTP + WS routes.

Identity rules (see ``7.5-unauth-skip`` / ``7.5-dev-shim-limit``):
- ``None`` identity (auth disabled) → caller skips the check entirely.
- ``"dev"`` (dev-shim sentinel) → bucket keyed on literal string ``"dev"``.
- ``ApiKeyInfo`` → bucket keyed on ``str(info.id)``.

Decision ``7.5-metric``: rate-limit denials bump
``kestrel_rate_limited_total{route_class}`` at the HTTP/WS dependency
boundary in slice 3, not inside ``check()`` itself. ``check()`` stays
framework-free so a future non-HTTP user could call it without dragging
Prometheus along.
"""

import math
import time
from dataclasses import dataclass
from typing import Callable, Literal, Protocol, runtime_checkable

import structlog
from fastapi import Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

from kestrel.config import Settings

logger = structlog.get_logger()


ROUTE_CLASSES = ("execute", "session_lifecycle", "admin")
RouteClass = Literal["execute", "session_lifecycle", "admin"]


class RateLimiterUnavailable(RuntimeError):
    """Raised when the rate limiter cannot reach its backing store.

    Slice 3's HTTP/WS deps catch this and decide whether to fail-open
    (allow the request, log a warning, bump a separate metric) or
    fail-closed. For slice 2, the limiter just signals the condition;
    callers handle policy.
    """


_LUA_TOKEN_BUCKET = """\
-- KEYS[1] = bucket key
-- ARGV[1] = capacity (int)
-- ARGV[2] = refill_rate_per_second (float as string)
-- ARGV[3] = now_ms (int)
-- Returns: {allowed (1 or 0), retry_after_seconds (int)}

local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local bucket = redis.call('HMGET', key, 'tokens', 'last_refill_at_ms')
local tokens = tonumber(bucket[1])
local last_refill = tonumber(bucket[2])

if tokens == nil or last_refill == nil then
    tokens = capacity
    last_refill = now_ms
else
    local elapsed_s = math.max(0, (now_ms - last_refill) / 1000.0)
    tokens = math.min(capacity, tokens + elapsed_s * refill_rate)
    last_refill = now_ms
end

local allowed = 0
local retry_after = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
else
    local deficit = 1 - tokens
    local secs = deficit / refill_rate
    retry_after = math.max(1, math.ceil(secs))
end

redis.call('HSET', key, 'tokens', tostring(tokens), 'last_refill_at_ms', tostring(last_refill))
redis.call('EXPIRE', key, ttl)

return {allowed, retry_after}
"""

_REDIS_KEY_TTL_SECONDS = 120


@dataclass(frozen=True)
class RateLimitDecision:
    """Returned by ``RateLimiter.check``.

    - ``allowed=True`` → caller proceeds; ``retry_after_seconds`` is 0.
    - ``allowed=False`` → caller rejects (HTTP 429 + ``Retry-After`` header,
    WS close 4429 per ``7.5-ws-close-code``). ``retry_after_seconds`` is
    a whole-second ceiling on the wait until the next token is available.
    """

    allowed: bool
    retry_after_seconds: int


@runtime_checkable
class RateLimiter(Protocol):
    """The rate-limiter contract.

    ``check`` is the hot path: called on every authenticated request.
    Implementations must keep the per-key state machine consistent under
    concurrent calls (single-event-loop atomicity is enough for the
    memory backend; the Redis backend in slice 2 uses an atomic Lua
    script / pipelined INCR).
    """

    async def start(self) -> None: ...
    async def aclose(self) -> None: ...
    async def check(
        self, identity: str, route_class: RouteClass
    ) -> RateLimitDecision: ...


@dataclass
class _Bucket:
    tokens: float
    last_refill_at: float


class InMemoryRateLimiter:
    """Per-worker in-memory token-bucket limiter.

    Stores one ``_Bucket`` per ``(identity, route_class)``. Refill happens
    lazily inside ``check`` — no background tasks, no Redis. Fine for
    single-worker deployments; multi-worker correctness requires
    ``RedisRateLimiter`` (slice 2) via ``KESTREL_SESSION_BACKEND=redis``.

    The ``time_source`` knob exists for tests — a controllable clock makes
    the refill behavior deterministic without ``asyncio.sleep``.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._now = time_source
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._limits: dict[str, int] = {
            "execute": settings.rate_limit_execute_per_minute,
            "session_lifecycle": settings.rate_limit_session_lifecycle_per_minute,
            "admin": settings.rate_limit_admin_per_minute,
        }

    async def start(self) -> None:
        logger.info("rate_limiter_started", backend="memory")

    async def aclose(self) -> None:
        logger.info("rate_limiter_stopped", backend="memory")

    async def check(
        self, identity: str, route_class: RouteClass
    ) -> RateLimitDecision:
        limit = self._limits.get(route_class)
        if limit is None:
            raise ValueError(f"unknown route_class: {route_class!r}")

        key = (identity, route_class)
        now = self._now()
        bucket = self._buckets.get(key)
        refill_rate = limit / 60.0  # tokens per second

        if bucket is None:
            bucket = _Bucket(tokens=float(limit), last_refill_at=now)
            self._buckets[key] = bucket
        else:
            elapsed = max(0.0, now - bucket.last_refill_at)
            bucket.tokens = min(float(limit), bucket.tokens + elapsed * refill_rate)
            bucket.last_refill_at = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return RateLimitDecision(allowed=True, retry_after_seconds=0)

        deficit = 1.0 - bucket.tokens
        seconds_until_token = deficit / refill_rate
        return RateLimitDecision(
            allowed=False,
            retry_after_seconds=max(1, math.ceil(seconds_until_token)),
        )


class RedisRateLimiter:
    """Cross-worker token-bucket limiter backed by Redis.

    State lives in one Redis HASH per ``(identity, route_class)`` —
    ``kestrel:rate_limit:{identity}:{route_class}`` with fields ``tokens``
    and ``last_refill_at_ms``. The token-bucket read/refill/decide/write
    runs as a single Lua script (decision ``7.5-redis-atomicity``) so two
    workers checking the same identity at the same time can't both decide
    allow off a stale read.

    Does NOT own the Redis client — receives it from the lifespan, which
    pulls it from the session registry's pool (decision
    ``7.5-redis-pool-share``). ``aclose()`` only logs; the actual pool
    close happens when ``RedisSessionRegistry.aclose()`` runs.

    ``time_source`` defaults to ``time.time`` (wall-clock Unix epoch
    seconds) — different from ``InMemoryRateLimiter``'s ``time.monotonic``
    default. Wall clock is required here because ``last_refill_at_ms`` is
    stored in Redis and read by other workers; monotonic clocks have
    different reference frames across processes (decision
    ``7.5-redis-time-source``).
    """

    def __init__(
        self,
        settings: Settings,
        client: Redis,
        *,
        time_source: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._client = client
        self._now = time_source
        self._limits: dict[str, int] = {
            "execute": settings.rate_limit_execute_per_minute,
            "session_lifecycle": settings.rate_limit_session_lifecycle_per_minute,
            "admin": settings.rate_limit_admin_per_minute,
        }
        self._script = client.register_script(_LUA_TOKEN_BUCKET)
        self._ttl_seconds = _REDIS_KEY_TTL_SECONDS

    async def start(self) -> None:
        try:
            await self._client.ping()  # type: ignore[misc]
        except RedisError as exc:
            raise RateLimiterUnavailable(f"cannot reach redis: {exc}") from exc
        logger.info("rate_limiter_started", backend="redis")

    async def aclose(self) -> None:
        # We don't own the client — RedisSessionRegistry does. Just log.
        logger.info("rate_limiter_stopped", backend="redis")

    async def check(
        self, identity: str, route_class: RouteClass
    ) -> RateLimitDecision:
        limit = self._limits.get(route_class)
        if limit is None:
            raise ValueError(f"unknown route_class: {route_class!r}")

        refill_rate = limit / 60.0
        now_ms = int(self._now() * 1000)
        key = f"kestrel:rate_limit:{identity}:{route_class}"

        try:
            result = await self._script(
                keys=[key],
                args=[limit, str(refill_rate), now_ms, self._ttl_seconds],
            )
        except RedisError as exc:
            raise RateLimiterUnavailable(f"redis error during check: {exc}") from exc

        allowed_int, retry_after_int = result
        return RateLimitDecision(
            allowed=bool(allowed_int),
            retry_after_seconds=int(retry_after_int),
        )


def build_rate_limiter(
    settings: Settings, *, redis_client: Redis | None = None
) -> RateLimiter:
    """Build the limiter named by ``settings.session_backend``
    (per decision ``7-ratelimit-storage``).

    - ``session_backend == "memory"`` → returns ``InMemoryRateLimiter``
    regardless of whether ``redis_client`` was passed.
    - ``session_backend == "redis"`` → returns ``RedisRateLimiter``;
    ``redis_client`` is required (raises ``ValueError`` otherwise).
    The lifespan pulls the client from the session registry via
    ``getattr(registry, "client", None)`` (decision ``7.5-redis-pool-share``).
    """
    if settings.session_backend == "redis":
        if redis_client is None:
            raise ValueError(
                "RedisRateLimiter requires redis_client=...; "
                "the lifespan must extract it from the session registry "
                "via getattr(registry, 'client', None) when "
                "session_backend == 'redis'."
            )
        return RedisRateLimiter(settings, redis_client)
    return InMemoryRateLimiter(settings)


def get_rate_limiter(request: Request) -> RateLimiter:
    """FastAPI dependency: returns the limiter bound to ``app.state`` by
    the lifespan. Slice 3 builds the per-route-class wrappers on top of
    this provider."""
    return request.app.state.rate_limiter