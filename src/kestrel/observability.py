from __future__ import annotations

"""Phase 7 substep 1: Prometheus metric definitions for the Kestrel service.

Metrics are module-level singletons registered with prometheus_client's default
REGISTRY at import time. Instrumentation sites reference these objects directly
(.inc() / .observe() / .set()); the GET /metrics route hands the REGISTRY to
generate_latest() to produce a scrape.

Label-cardinality discipline (per 7-metrics-auth):
- NEVER add api_key_id, request_id, or session_id as a metric label — those
values are high-cardinality (1 series per key/request/session) and would blow
up Prometheus storage. The audit log is the right place for that data.
- "route" labels use the FastAPI route template (e.g. "/sessions/{session_id}"),
never the concrete path, for the same reason.
"""

from prometheus_client import Counter, Gauge, Histogram


HTTP_REQUESTS = Counter(
    "kestrel_http_requests_total",
    "Total HTTP requests handled by Kestrel.",
    ["route", "method", "status"],
)

HTTP_REQUEST_DURATION = Histogram(
    "kestrel_http_request_duration_seconds",
    "HTTP request duration in seconds, labelled by route template.",
    ["route", "method"],
)

EXECUTIONS = Counter(
    "kestrel_executions_total",
    "Total code-execution invocations completed, labelled by backend and outcome.",
    ["backend", "outcome"],
)

EXECUTION_DURATION = Histogram(
    "kestrel_execution_duration_seconds",
    "Code-execution wall-clock duration in seconds, labelled by backend.",
    ["backend"],
)

SESSIONS_ACTIVE = Gauge(
    "kestrel_sessions_active",
    "Number of active sessions currently held by this worker.",
)

SESSION_POOL_SIZE = Gauge(
    "kestrel_session_pool_size",
    "Number of warm runtimes currently sitting in the session pool.",
)

STREAM_ACTIVE = Gauge(
    "kestrel_stream_active",
    "Number of WebSocket streaming connections currently open on this worker.",
)

POLLING_BUFFERS_ACTIVE = Gauge(
    "kestrel_polling_buffers_active",
    "Number of polling buffers currently held by this worker.",
)

AUDIT_DROPPED = Counter(
    "kestrel_audit_dropped_total",
    "Audit events dropped due to bounded-queue overflow (7-audit-sync).",
)

RATE_LIMITED = Counter(
    "kestrel_rate_limited_total",
    "Requests rejected by the rate limiter, by route class (7.5-metric).",
    ["route_class"],
)