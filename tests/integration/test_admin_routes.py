"""Phase 7 substep 6 slice 1: admin GET-route integration tests.

Exercises:
- ``require_admin_scope`` (decision ``7-admin-dev-shim``): scope-rejected-403,
scope-accepted-200, dev-shim-allowed, missing-bearer-401.
- ``GET /admin/keys``, ``GET /admin/sessions``, ``GET /admin/audit`` shape.
- Audit pagination (``7.6-audit-pagination``): cursor advance + limit clamp +
503-when-no-backend.

Postgres-backed tests skip when ``kestrel-postgres`` is unreachable via the
shared ``postgres_migrations_applied`` fixture.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text


# ── require_admin_scope (decision 7-admin-dev-shim) ──


async def test_admin_keys_without_admin_scope_returns_403(
    api_postgres_client, mint_api_key
):
    token, _info = await mint_api_key(label="execute-only", scopes=["execute"])
    response = api_postgres_client.get(
        "/admin/keys", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "admin scope required"}


async def test_admin_keys_with_admin_scope_returns_200_and_lists_keys(
    api_postgres_client, mint_api_key
):
    token, info = await mint_api_key(
        label="admin-key", scopes=["execute", "admin"]
    )
    response = api_postgres_client.get(
        "/admin/keys", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    body = response.json()
    ids = {k["id"] for k in body["keys"]}
    assert str(info.id) in ids
    minted = next(k for k in body["keys"] if k["id"] == str(info.id))
    assert minted["label"] == "admin-key"
    assert "admin" in minted["scopes"]
    assert minted["revoked_at"] is None


async def test_admin_keys_dev_shim_allowed_for_admin_routes(
    api_postgres_client_with_dev_shim,
):
    response = api_postgres_client_with_dev_shim.get(
        "/admin/keys",
        headers={"Authorization": "Bearer test-dev-shim-12345"},
    )
    assert response.status_code == 200


async def test_admin_keys_no_bearer_returns_401(api_postgres_client):
    response = api_postgres_client.get("/admin/keys")
    assert response.status_code == 401


# ── /admin/sessions shape ──


async def test_admin_sessions_with_admin_scope_returns_list_shape(
    api_postgres_client_with_dev_shim,
):
    response = api_postgres_client_with_dev_shim.get(
        "/admin/sessions",
        headers={"Authorization": "Bearer test-dev-shim-12345"},
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["sessions"], list)


# ── /admin/audit pagination + bounds ──


def test_admin_audit_returns_503_when_no_backend(docker_client):
    """No KESTREL_AUDIT_BACKEND set → no sessionmaker on app.state → 503.
    Auth is disabled here (api_key_store None, dev_api_key empty), so the
    scope + rate-limit deps pass and we reach the route body."""
    response = docker_client.get("/admin/audit")
    assert response.status_code == 503
    assert response.json() == {"detail": "audit backend not configured"}


async def test_admin_audit_limit_out_of_bounds_returns_422(
    api_postgres_client_with_dev_shim,
):
    headers = {"Authorization": "Bearer test-dev-shim-12345"}
    too_large = api_postgres_client_with_dev_shim.get(
        "/admin/audit?limit=1000", headers=headers
    )
    assert too_large.status_code == 422
    too_small = api_postgres_client_with_dev_shim.get(
        "/admin/audit?limit=0", headers=headers
    )
    assert too_small.status_code == 422


async def test_admin_audit_paginates_with_before_ts_cursor(
    api_postgres_client_with_dev_shim, postgres_engine
):
    """Insert 5 rows with controlled timestamps, then page through them at
    limit=2. Verify newest-first ordering, next_before_ts advancing, and
    null cursor on the last partial page."""
    base_ts = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
    async with postgres_engine.begin() as conn:
        for i in range(5):
            await conn.execute(
                text(
                    "INSERT INTO audit_events "
                    "(id, ts, request_id, route, method, status) "
                    "VALUES (:id, :ts, :rid, '/test', 'GET', 200)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "ts": base_ts + timedelta(seconds=i),
                    "rid": f"test-rid-{i}",
                },
            )

    headers = {"Authorization": "Bearer test-dev-shim-12345"}
    page1 = api_postgres_client_with_dev_shim.get(
        "/admin/audit?limit=2", headers=headers
    ).json()
    assert [e["request_id"] for e in page1["events"]] == ["test-rid-4", "test-rid-3"]
    assert page1["next_before_ts"] is not None

    page2 = api_postgres_client_with_dev_shim.get(
        f"/admin/audit?limit=2&before_ts={page1['next_before_ts']}",
        headers=headers,
    ).json()
    assert [e["request_id"] for e in page2["events"]] == ["test-rid-2", "test-rid-1"]
    assert page2["next_before_ts"] is not None

    page3 = api_postgres_client_with_dev_shim.get(
        f"/admin/audit?limit=2&before_ts={page2['next_before_ts']}",
        headers=headers,
    ).json()
    assert [e["request_id"] for e in page3["events"]] == ["test-rid-0"]
    assert page3["next_before_ts"] is None