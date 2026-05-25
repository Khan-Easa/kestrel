"""Phase 7 substep 3 slice 1: PostgresApiKeyStore integration tests.

Requires kestrel-postgres container (skips when unreachable). Each test
truncates api_keys via the postgres_engine fixture before yielding.
"""

from __future__ import annotations

import uuid

import pytest

from kestrel.api_keys import (
    ApiKeyInfo,
    PostgresApiKeyStore,
    TOKEN_PREFIX,
    build_api_key_store,
    generate_token,
    hash_token,
)
from kestrel.config import Settings


def test_generate_token_has_kestrel_prefix():
    token = generate_token()
    assert token.startswith(TOKEN_PREFIX)
    assert len(token) >= len(TOKEN_PREFIX) + 40  # 32 bytes urlsafe ≈ 43 chars


def test_hash_token_returns_64_char_hex():
    h = hash_token("kestrel_abc123")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_hash_token_is_deterministic():
    assert hash_token("kestrel_abc123") == hash_token("kestrel_abc123")


def test_build_api_key_store_returns_none_when_backend_null():
    settings = Settings()
    assert build_api_key_store(settings) is None


def test_build_api_key_store_requires_engine_for_postgres():
    settings = Settings(
        api_key_backend="postgres",
        database_url="postgresql+asyncpg://x/y",
    )
    with pytest.raises(ValueError, match="engine"):
        build_api_key_store(settings)


def test_build_api_key_store_returns_postgres_with_engine(postgres_engine):
    settings = Settings(
        api_key_backend="postgres",
        database_url="postgresql+asyncpg://x/y",
    )
    store = build_api_key_store(settings, engine=postgres_engine)
    assert isinstance(store, PostgresApiKeyStore)


async def test_create_returns_token_and_info(api_key_store_factory):
    store = await api_key_store_factory()
    token, info = await store.create(label="test-key")
    assert token.startswith(TOKEN_PREFIX)
    assert isinstance(info, ApiKeyInfo)
    assert info.label == "test-key"
    assert info.scopes == ["execute"]
    assert info.revoked_at is None
    assert info.created_at is not None


async def test_verify_returns_info_for_valid_token(api_key_store_factory):
    store = await api_key_store_factory()
    token, info = await store.create(label="verify-test")
    verified = await store.verify(token)
    assert verified is not None
    assert verified.id == info.id
    assert verified.label == "verify-test"


async def test_verify_returns_none_for_unknown_token(api_key_store_factory):
    store = await api_key_store_factory()
    assert await store.verify("kestrel_definitely_not_a_real_token") is None


async def test_verify_returns_none_for_revoked_token(api_key_store_factory):
    store = await api_key_store_factory()
    token, info = await store.create(label="to-revoke")
    assert await store.revoke(info.id) is True
    assert await store.verify(token) is None


async def test_list_returns_all_keys(api_key_store_factory):
    store = await api_key_store_factory()
    await store.create(label="key-1")
    await store.create(label="key-2")
    await store.create(label="key-3")
    listed = await store.list()
    assert len(listed) == 3
    assert {info.label for info in listed} == {"key-1", "key-2", "key-3"}


async def test_revoke_unknown_id_returns_false(api_key_store_factory):
    store = await api_key_store_factory()
    assert await store.revoke(uuid.uuid4()) is False


async def test_create_with_custom_scopes(api_key_store_factory):
    store = await api_key_store_factory()
    token, info = await store.create(
        label="admin-key", scopes=["execute", "admin"]
    )
    assert info.scopes == ["execute", "admin"]
    verified = await store.verify(token)
    assert verified is not None
    assert verified.scopes == ["execute", "admin"]