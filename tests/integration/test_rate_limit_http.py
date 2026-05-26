"""Phase 7 substep 5 slice 3: end-to-end rate-limit integration tests.

Exercises the HTTP 429 path + Retry-After header + RATE_LIMITED counter
bumps + fail-open behavior + skip-when-unauthenticated. Uses tiny limit
values so tests don't need 60+ requests to hit the wall.

Subprocess-only (no Docker, Postgres, Redis required for the core path).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kestrel.app import create_app
from kestrel.audit import NullAuditSink
from kestrel.config import Settings, get_settings
from kestrel.execution import get_executor
from kestrel.execution.manager import SubprocessExecutor
from kestrel.execution.session_registry import InMemorySessionRegistry
from kestrel.observability import RATE_LIMITED, RATE_LIMIT_FAILURES
from kestrel.rate_limit import (
    InMemoryRateLimiter,
    RateLimiterUnavailable,
    get_rate_limiter,
)


def _counter_value(counter, **labels) -> float:
    """Read a Prometheus Counter value via .collect() — version-safe across
    prometheus-client versions."""
    for metric in counter.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total") and all(
                sample.labels.get(k) == v for k, v in labels.items()
            ):
                return sample.value
    return 0.0


def _make_app(**settings_overrides) -> TestClient:
    defaults = {
        "dev_api_key": "test-key",
        "executor_backend": "subprocess",
        "rate_limit_execute_per_minute": 3,
        "rate_limit_session_lifecycle_per_minute": 5,
        "rate_limit_admin_per_minute": 2,
    }
    defaults.update(settings_overrides)
    settings = Settings(**defaults)
    app = create_app()
    limiter = InMemoryRateLimiter(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_executor] = lambda: SubprocessExecutor()
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    app.state.audit_sink = NullAuditSink()
    app.state.api_key_store = None
    app.state.rate_limiter = limiter
    app.state.registry = InMemorySessionRegistry(settings=settings)
    return TestClient(app)


def test_execute_within_limit_returns_200():
    client = _make_app()
    for _ in range(3):
        response = client.post(
            "/execute",
            json={"code": "print(1)"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 200


def test_execute_beyond_limit_returns_429_with_retry_after():
    client = _make_app()
    for _ in range(3):
        client.post(
            "/execute",
            json={"code": "print(1)"},
            headers={"Authorization": "Bearer test-key"},
        )
    response = client.post(
        "/execute",
        json={"code": "print(1)"},
        headers={"Authorization": "Bearer test-key"},
    )
    assert response.status_code == 429
    assert "Retry-After" in response.headers
    assert int(response.headers["Retry-After"]) >= 1


def test_unauthenticated_requests_skip_rate_limit():
    """Auth disabled (dev_api_key="" + no store) → identity None →
    rate limit skipped per 7.5-unauth-skip. Fire well beyond the 3-per-min
    limit; all should pass."""
    client = _make_app(dev_api_key="", rate_limit_execute_per_minute=3)
    for _ in range(10):
        response = client.post("/execute", json={"code": "print(1)"})
        assert response.status_code == 200


def test_dev_shim_is_rate_limited_under_dev_key():
    """7.5-dev-shim-limit: dev shim shares one bucket keyed "dev"."""
    client = _make_app()
    for _ in range(3):
        client.post(
            "/execute",
            json={"code": "print(1)"},
            headers={"Authorization": "Bearer test-key"},
        )
    response = client.post(
        "/execute",
        json={"code": "print(1)"},
        headers={"Authorization": "Bearer test-key"},
    )
    assert response.status_code == 429


def test_rate_limited_counter_increments_on_denial():
    client = _make_app()
    before = _counter_value(RATE_LIMITED, route_class="execute")
    for _ in range(3):
        client.post(
            "/execute",
            json={"code": "print(1)"},
            headers={"Authorization": "Bearer test-key"},
        )
    client.post(
        "/execute",
        json={"code": "print(1)"},
        headers={"Authorization": "Bearer test-key"},
    )
    after = _counter_value(RATE_LIMITED, route_class="execute")
    assert after == before + 1


def test_session_lifecycle_within_limit_returns_201():
    """POST /sessions uses session_lifecycle class (5/min in test)."""
    if not _docker_check():
        pytest.skip("docker daemon unreachable for session test")
    client = _make_app()
    for _ in range(5):
        response = client.post(
            "/sessions",
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 201


def test_session_lifecycle_beyond_limit_returns_429():
    if not _docker_check():
        pytest.skip("docker daemon unreachable for session test")
    client = _make_app()
    for _ in range(5):
        client.post("/sessions", headers={"Authorization": "Bearer test-key"})
    response = client.post(
        "/sessions",
        headers={"Authorization": "Bearer test-key"},
    )
    assert response.status_code == 429


def test_execute_and_session_lifecycle_buckets_are_independent():
    """7-rate-limit-dims: different classes have independent buckets.
    Burn the execute bucket; session_lifecycle should still pass."""
    if not _docker_check():
        pytest.skip("docker daemon unreachable for session test")
    client = _make_app()
    for _ in range(3):
        client.post(
            "/execute",
            json={"code": "print(1)"},
            headers={"Authorization": "Bearer test-key"},
        )
    response = client.post("/sessions", headers={"Authorization": "Bearer test-key"})
    assert response.status_code == 201


def test_failed_open_when_limiter_unavailable():
    """7.5-fail-policy: RateLimiterUnavailable → request passes, counter bumps."""

    class _BrokenLimiter(InMemoryRateLimiter):
        async def check(self, identity, route_class):
            raise RateLimiterUnavailable("simulated outage")

    settings = Settings(
        dev_api_key="test-key",
        executor_backend="subprocess",
        rate_limit_execute_per_minute=60,
    )
    app = create_app()
    broken = _BrokenLimiter(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_executor] = lambda: SubprocessExecutor()
    app.dependency_overrides[get_rate_limiter] = lambda: broken
    app.state.audit_sink = NullAuditSink()
    app.state.api_key_store = None
    app.state.rate_limiter = broken
    test_client = TestClient(app)

    before = _counter_value(RATE_LIMIT_FAILURES, route_class="execute")
    response = test_client.post(
        "/execute",
        json={"code": "print(1)"},
        headers={"Authorization": "Bearer test-key"},
    )
    assert response.status_code == 200  # fail-open
    after = _counter_value(RATE_LIMIT_FAILURES, route_class="execute")
    assert after == before + 1


def _docker_check() -> bool:
    """Inline docker check — kept here to avoid pulling in conftest's heavier
    machinery for the subprocess-mostly tests in this file."""
    import shutil
    import subprocess

    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=5.0, check=False
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False