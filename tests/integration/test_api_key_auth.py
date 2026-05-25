"""Phase 7 substep 3 slice 2: end-to-end auth-flow tests.

Verifies:
- A token from the store authenticates a request and lands in audit as
api_key_id = str(info.id).
- Unknown / revoked tokens 401.
- The dev shim still works when set, with audit api_key_id = "dev".
"""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import text


async def _wait_for_audit_row(engine, request_id: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT request_id, status, api_key_id "
                    "FROM audit_events WHERE request_id = :rid"
                ),
                {"rid": request_id},
            )
            row = result.first()
        if row is not None:
            return row
        await asyncio.sleep(0.05)
    return None


async def test_store_token_authenticates(api_postgres_client, mint_api_key):
    token, _info = await mint_api_key(label="happy-path")
    response = api_postgres_client.post(
        "/execute",
        json={"code": "print('hi')"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200


async def test_unknown_token_rejects_with_401(api_postgres_client):
    response = api_postgres_client.post(
        "/execute",
        json={"code": "print('hi')"},
        headers={"Authorization": "Bearer kestrel_definitely_not_a_real_token"},
    )
    assert response.status_code == 401


async def test_no_bearer_rejects_with_401(api_postgres_client):
    response = api_postgres_client.post(
        "/execute",
        json={"code": "print('hi')"},
    )
    assert response.status_code == 401


async def test_revoked_token_rejects_with_401(api_postgres_client, mint_api_key, postgres_engine):
    token, info = await mint_api_key(label="to-revoke")
    # Revoke via direct DB update (no admin endpoint yet).
    async with postgres_engine.begin() as conn:
        await conn.execute(
            text("UPDATE api_keys SET revoked_at = now() WHERE id = :id"),
            {"id": info.id},
        )

    response = api_postgres_client.post(
        "/execute",
        json={"code": "print('hi')"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


async def test_audit_row_carries_store_api_key_id(
    api_postgres_client, mint_api_key, postgres_engine
):
    token, info = await mint_api_key(label="audit-test")
    rid = "test-req-audit-store-key"
    response = api_postgres_client.post(
        "/execute",
        json={"code": "print('hi')"},
        headers={
            "Authorization": f"Bearer {token}",
            "X-Request-ID": rid,
        },
    )
    assert response.status_code == 200

    row = await _wait_for_audit_row(postgres_engine, rid)
    assert row is not None
    assert row.api_key_id == str(info.id)


async def test_dev_shim_path_authenticates_and_audits_as_dev(
    api_postgres_client_with_dev_shim, postgres_engine
):
    rid = "test-req-dev-shim-audit"
    response = api_postgres_client_with_dev_shim.post(
        "/execute",
        json={"code": "print('hi')"},
        headers={
            "Authorization": "Bearer test-dev-shim-12345",
            "X-Request-ID": rid,
        },
    )
    assert response.status_code == 200

    row = await _wait_for_audit_row(postgres_engine, rid)
    assert row is not None
    assert row.api_key_id == "dev"