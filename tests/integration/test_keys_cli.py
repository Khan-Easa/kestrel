"""Phase 7 substep 4: kestrel-keys CLI integration tests.

Black-box: invokes the entry point as a real subprocess via
``uv run kestrel-keys ...`` (decision 7.4-test-style). Requires the
kestrel-postgres test container; skips when unreachable. Shares the
per-test ``postgres_engine`` fixture so both the subprocess and the
assertion-side store see the same TRUNCATEd api_keys table.
"""

from __future__ import annotations

import json
import os
import subprocess

from kestrel.api_keys import TOKEN_PREFIX

TEST_DATABASE_URL = "postgresql+asyncpg://postgres:kestrel@localhost:5432/postgres"


def _run(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "KESTREL_DATABASE_URL": TEST_DATABASE_URL}
    return subprocess.run(
        ["uv", "run", "kestrel-keys", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


async def test_create_mints_key_and_prints_token(
    postgres_engine, api_key_store_factory
):
    result = _run("create", "ci-test-key")
    assert result.returncode == 0, result.stderr
    assert TOKEN_PREFIX in result.stdout
    assert "ci-test-key" in result.stdout

    store = await api_key_store_factory()
    listed = await store.list()
    assert any(k.label == "ci-test-key" for k in listed)


async def test_revoke_marks_key_revoked(
    postgres_engine, api_key_store_factory
):
    store = await api_key_store_factory()
    _token, info = await store.create(label="to-revoke")

    result = _run("revoke", str(info.id))
    assert result.returncode == 0, result.stderr
    assert "revoked" in result.stdout
    assert str(info.id) in result.stdout

    listed = await store.list()
    match = next(k for k in listed if k.id == info.id)
    assert match.revoked_at is not None


async def test_list_json_shows_revoked_flag(
    postgres_engine, api_key_store_factory
):
    store = await api_key_store_factory()
    await store.create(label="active-key")
    _token, to_revoke = await store.create(label="revoked-key")
    await store.revoke(to_revoke.id)

    result = _run("list", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    by_label = {entry["label"]: entry for entry in payload}
    assert by_label["active-key"]["revoked_at"] is None
    assert by_label["revoked-key"]["revoked_at"] is not None