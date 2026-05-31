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

import asyncio
import time
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


# ── slice 2: POST /admin/keys + DELETE /admin/keys/{id} ──


async def _wait_for_audit_row(engine, request_id: str, timeout: float = 5.0):
    """Poll audit_events for a row with the given request_id (same pattern as
    test_audit_emit). The drain task inserts on TestClient's loop; this reads
    on the test's loop. Returns the row or None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT request_id, route, method, status "
                    "FROM audit_events WHERE request_id = :rid"
                ),
                {"rid": request_id},
            )
            row = result.first()
        if row is not None:
            return row
        await asyncio.sleep(0.05)
    return None


async def test_create_key_returns_token_and_lists_it(
    api_postgres_client, mint_api_key
):
    admin_token, _ = await mint_api_key(label="admin", scopes=["execute", "admin"])
    headers = {"Authorization": f"Bearer {admin_token}"}

    resp = api_postgres_client.post(
        "/admin/keys",
        json={"label": "minted-via-api", "scopes": ["execute"]},
        headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["token"].startswith("kestrel_")
    assert body["label"] == "minted-via-api"
    assert body["scopes"] == ["execute"]
    assert body["revoked_at"] is None

    listing = api_postgres_client.get("/admin/keys", headers=headers).json()
    assert body["id"] in {k["id"] for k in listing["keys"]}


async def test_created_token_can_execute(api_postgres_client, mint_api_key):
    admin_token, _ = await mint_api_key(label="admin", scopes=["execute", "admin"])
    resp = api_postgres_client.post(
        "/admin/keys",
        json={"label": "worker", "scopes": ["execute"]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201
    worker_token = resp.json()["token"]

    execute = api_postgres_client.post(
        "/execute",
        json={"code": "print('hello from minted key')"},
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert execute.status_code == 200
    assert "hello from minted key" in execute.json()["stdout"]


async def test_revoke_marks_key_revoked_in_list(api_postgres_client, mint_api_key):
    admin_token, _ = await mint_api_key(label="admin", scopes=["execute", "admin"])
    _victim_token, victim = await mint_api_key(label="to-revoke", scopes=["execute"])
    headers = {"Authorization": f"Bearer {admin_token}"}

    delete = api_postgres_client.delete(f"/admin/keys/{victim.id}", headers=headers)
    assert delete.status_code == 204

    listing = api_postgres_client.get("/admin/keys", headers=headers).json()
    row = next(k for k in listing["keys"] if k["id"] == str(victim.id))
    assert row["revoked_at"] is not None


async def test_revoked_token_no_longer_authenticates(
    api_postgres_client, mint_api_key
):
    admin_token, _ = await mint_api_key(label="admin", scopes=["execute", "admin"])
    victim_token, victim = await mint_api_key(label="victim", scopes=["execute"])

    # Auth-gated GET /sessions works before revocation (no container needed).
    before = api_postgres_client.get(
        "/sessions", headers={"Authorization": f"Bearer {victim_token}"}
    )
    assert before.status_code == 200

    revoke = api_postgres_client.delete(
        f"/admin/keys/{victim.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert revoke.status_code == 204

    after = api_postgres_client.get(
        "/sessions", headers={"Authorization": f"Bearer {victim_token}"}
    )
    assert after.status_code == 401


async def test_revoke_unknown_uuid_returns_404(api_postgres_client, mint_api_key):
    admin_token, _ = await mint_api_key(label="admin", scopes=["execute", "admin"])
    resp = api_postgres_client.delete(
        f"/admin/keys/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "api key not found"}


async def test_revoke_already_revoked_is_idempotent(
    api_postgres_client, mint_api_key
):
    admin_token, _ = await mint_api_key(label="admin", scopes=["execute", "admin"])
    _token, victim = await mint_api_key(label="twice", scopes=["execute"])
    headers = {"Authorization": f"Bearer {admin_token}"}

    first = api_postgres_client.delete(f"/admin/keys/{victim.id}", headers=headers)
    assert first.status_code == 204
    first = api_postgres_client.delete(f"/admin/keys/{victim.id}", headers=headers)
    assert first.status_code == 204
    # Re-revoking an already-revoked key is a 204 success, not a 404
    # (decision 7.6-revoke-semantics).
    second = api_postgres_client.delete(f"/admin/keys/{victim.id}", headers=headers)
    assert second.status_code == 204


async def test_both_mutation_routes_emit_audit(
    api_postgres_client, mint_api_key, postgres_engine
):
    admin_token, _ = await mint_api_key(label="admin", scopes=["execute", "admin"])
    _victim_token, victim = await mint_api_key(
        label="audit-victim", scopes=["execute"]
    )
    headers = {"Authorization": f"Bearer {admin_token}"}

    create_rid = "test-admin-create-audit"
    create = api_postgres_client.post(
        "/admin/keys",
        json={"label": "audited-create", "scopes": ["execute"]},
        headers={**headers, "X-Request-ID": create_rid},
    )
    assert create.status_code == 201

    delete_rid = "test-admin-delete-audit"
    delete = api_postgres_client.delete(
        f"/admin/keys/{victim.id}",
        headers={**headers, "X-Request-ID": delete_rid},
    )
    assert delete.status_code == 204

    create_row = await _wait_for_audit_row(postgres_engine, create_rid)
    assert create_row is not None
    assert create_row.route == "/admin/keys"
    assert create_row.method == "POST"
    assert create_row.status == 201

    delete_row = await _wait_for_audit_row(postgres_engine, delete_rid)
    assert delete_row is not None
    assert delete_row.route == "/admin/keys/{key_id}"
    assert delete_row.method == "DELETE"
    assert delete_row.status == 204