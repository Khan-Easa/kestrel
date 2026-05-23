from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kestrel.app import create_app


@pytest.fixture
def metrics_client() -> TestClient:
    """Plain TestClient — no lifespan, no executor. Metrics tests only need /health and /metrics."""
    return TestClient(create_app())


def test_metrics_endpoint_returns_prometheus_format(metrics_client: TestClient) -> None:
    resp = metrics_client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "kestrel_http_requests_total" in resp.text


def test_health_call_increments_http_counter(metrics_client: TestClient) -> None:
    metrics_client.get("/health")
    metrics = metrics_client.get("/metrics").text
    assert 'kestrel_http_requests_total{method="GET",route="/health",status="200"}' in metrics


def test_unmatched_path_labels_as_unmatched(metrics_client: TestClient) -> None:
    metrics_client.get("/no-such-path-here")
    metrics = metrics_client.get("/metrics").text
    assert 'route="unmatched"' in metrics


def test_metrics_route_appears_in_its_own_metrics(metrics_client: TestClient) -> None:
    """The /metrics endpoint instruments itself — sanity check we didn't accidentally exclude it."""
    metrics_client.get("/metrics")
    metrics = metrics_client.get("/metrics").text
    assert 'route="/metrics"' in metrics


# ───────────────────── Phase 7 substep 1 slice 2 additions ─────────────────────


def test_metrics_includes_sessions_active_gauge(metrics_client: TestClient) -> None:
    """The SESSIONS_ACTIVE gauge name is registered and scrape-visible."""
    text = metrics_client.get("/metrics").text
    assert "kestrel_sessions_active" in text
    assert "kestrel_session_pool_size" in text
    assert "kestrel_polling_buffers_active" in text


def test_metrics_includes_stream_active_gauge(metrics_client: TestClient) -> None:
    assert "kestrel_stream_active" in metrics_client.get("/metrics").text


def test_metrics_includes_executions_counter(metrics_client: TestClient) -> None:
    """The EXECUTIONS counter is registered (no need for an actual execute to verify the name)."""
    text = metrics_client.get("/metrics").text
    assert "kestrel_executions_total" in text
    assert "kestrel_execution_duration_seconds" in text


def test_metrics_includes_audit_dropped_counter(metrics_client: TestClient) -> None:
    """The AUDIT_DROPPED counter is declared in slice 1 even though the audit queue lands in substep 2."""
    assert "kestrel_audit_dropped_total" in metrics_client.get("/metrics").text


def test_stateless_execute_increments_executions_counter(client: TestClient) -> None:
    """Stateless /execute bumps kestrel_executions_total with the current backend label.

    Uses the parametrized client fixture so it runs against both subprocess and docker
    backends — both should increment the counter.
    """
    resp = client.post("/execute", json={"code": "print('hello')"})
    assert resp.status_code == 200

    metrics = client.get("/metrics").text
    # Substring match — value omitted, backend label varies with the fixture param.
    assert 'kestrel_executions_total{' in metrics
    assert 'outcome="ok"' in metrics