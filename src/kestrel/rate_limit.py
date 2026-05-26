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

from kestrel.config import Settings

logger = structlog.get_logger()


ROUTE_CLASSES = ("execute", "session_lifecycle", "admin")
RouteClass = Literal["execute", "session_lifecycle", "admin"]


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


def build_rate_limiter(settings: Settings) -> RateLimiter:
    """Build the limiter named by ``settings.session_backend``
    (per decision ``7-ratelimit-storage``).

    Slice 1 ships only the memory backend. When
    ``session_backend == "redis"`` we still return ``InMemoryRateLimiter``
    (per-worker, not shared across workers) — slice 2 replaces this with
    ``RedisRateLimiter``. This keeps multi-worker Redis deployments
    running through the slice gap; no route consumes the limiter until
    slice 3, so the temporary per-worker-only behavior is invisible to
    callers in slice 1 and slice 2.
    """
    return InMemoryRateLimiter(settings)


def get_rate_limiter(request: Request) -> RateLimiter:
    """FastAPI dependency: returns the limiter bound to ``app.state`` by
    the lifespan. Slice 3 builds the per-route-class wrappers on top of
    this provider."""
    return request.app.state.rate_limiter