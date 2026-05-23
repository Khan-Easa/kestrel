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