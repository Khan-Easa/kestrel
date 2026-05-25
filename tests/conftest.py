import shutil
import asyncio
import subprocess
from collections.abc import Callable
from functools import lru_cache
from typing import Any

import pytest
from fastapi.testclient import TestClient
import redis.asyncio
import os
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from kestrel.audit import NullAuditSink, PostgresAuditSink
from kestrel.api_keys import PostgresApiKeyStore
from kestrel.app import create_app
from kestrel.config import Settings, get_settings
from kestrel.execution import get_executor
from kestrel.execution.docker_executor import DockerExecutor
from kestrel.execution.manager import SubprocessExecutor
from kestrel.execution.session_runtime import SessionRuntime
from kestrel.execution.session_registry import InMemorySessionRegistry
from kestrel.execution.redis_session_registry import RedisSessionRegistry

TEST_REDIS_URL = "redis://localhost:6379/15"  # db 15 — isolated from real data
TEST_DATABASE_URL = "postgresql+asyncpg://postgres:kestrel@localhost:5432/postgres"


@lru_cache(maxsize=1)
def _docker_reachable() -> bool:
    """Quick check that the Docker CLI exists and the daemon responds."""
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5.0,
            check=False,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False

@lru_cache(maxsize=1)
def _postgres_reachable() -> bool:
    """Quick check that Postgres answers on the test URL."""
    try:
        import asyncpg

        async def _check() -> bool:
            try:
                conn = await asyncpg.connect(
                    host="localhost",
                    port=5432,
                    user="postgres",
                    password="kestrel",
                    database="postgres",
                    timeout=2.0,
                )
                await conn.close()
                return True
            except Exception:
                return False

        return asyncio.run(_check())
    except Exception:
        return False
    
@lru_cache(maxsize=1)
def _redis_reachable() -> bool:
    """Quick check that a Redis server answers on the test URL."""
    try:
        client = redis.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=2)
        client.ping()
        client.close()
        return True
    except Exception:
        return False


@pytest.fixture(params=["subprocess", "docker"])
def client(request: pytest.FixtureRequest) -> TestClient:
    backend = request.param
    if backend == "docker" and not _docker_reachable():
        pytest.skip("docker daemon unreachable")

    app = create_app()
    if backend == "subprocess":
        app.dependency_overrides[get_executor] = lambda: SubprocessExecutor()
    else:
        app.dependency_overrides[get_executor] = lambda: DockerExecutor()
    app.state.audit_sink = NullAuditSink()
    app.state.api_key_store = None
    return TestClient(app)
@pytest.fixture
def docker_client() -> TestClient:
    """Always-Docker variant for isolation-specific tests."""
    if not _docker_reachable():
        pytest.skip("docker daemon unreachable")

    app = create_app()
    app.dependency_overrides[get_executor] = lambda: DockerExecutor()
    app.state.audit_sink = NullAuditSink()
    app.state.api_key_store = None
    return TestClient(app)


@pytest.fixture
def override_settings(client: TestClient) -> Callable[..., None]:
    def _apply(**overrides: Any) -> None:
        defaults = {
            "dev_api_key": "",
            "execute_timeout_seconds": 5.0,
            "execute_output_cap_bytes": 1_048_576,
            "log_level": "INFO",
            "log_json": False,
        }
        defaults.update(overrides)
        client.app.dependency_overrides[get_settings] = lambda: Settings(**defaults)

    yield _apply
    client.app.dependency_overrides.clear()

@pytest.fixture
async def session_runtime_factory():
    """Yields an async factory ``_make(timeout_seconds=5.0)`` that starts
    SessionRuntime instances. Every instance created during the test is
    closed in teardown — close() is idempotent so explicit close() in
    a test is fine.
    """
    if not _docker_reachable():
        pytest.skip("docker daemon unreachable")

    started: list[SessionRuntime] = []

    async def _make(timeout_seconds: float = 5.0, **kwargs) -> SessionRuntime:
        runtime = await SessionRuntime.start(
            image_tag=get_settings().executor_docker_image,
            timeout_seconds=timeout_seconds,
            **kwargs,
        )
        started.append(runtime)
        return runtime

    try:
        yield _make
    finally:
        for runtime in started:
            await runtime.close()


@pytest.fixture
async def session_registry_factory():
    """Yields an async factory ``_make(**settings_overrides)`` that builds
    InMemorySessionRegistry instances with reasonable test defaults. Every registry
    created is aclose()'d in teardown — aclose is idempotent so tests may
    call it explicitly too.

    By default the background sweeper task is NOT started; tests drive
    eviction by calling ``await registry._sweep_once(timeout_seconds=...)``
    directly so no real timers are involved.
    """
    if not _docker_reachable():
        pytest.skip("docker daemon unreachable")

    created: list[InMemorySessionRegistry] = []

    async def _make(**overrides: Any) -> InMemorySessionRegistry:
        defaults = {
            "dev_api_key": "",
            "execute_timeout_seconds": 5.0,
            "execute_output_cap_bytes": 1_048_576,
            "log_level": "INFO",
            "log_json": False,
            "executor_backend": "docker",
            "executor_docker_image": "kestrel-runtime:0.3.0",
            "session_idle_timeout_seconds": 900.0,
            "session_sweep_interval_seconds": 60.0,
        }
        defaults.update(overrides)
        settings = Settings(**defaults)
        registry = InMemorySessionRegistry(settings=settings)
        created.append(registry)
        return registry

    try:
        yield _make
    finally:
        for registry in created:
            await registry.aclose()


@pytest.fixture
def session_http_client():
    """TestClient with the FastAPI lifespan started, so app.state.registry is set."""
    if not _docker_reachable():
        pytest.skip("docker daemon unreachable")

    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def session_http_client_authed():
    """Like session_http_client but with dev_api_key set so /sessions/* requires bearer auth."""
    if not _docker_reachable():
        pytest.skip("docker daemon unreachable")

    app = create_app()
    settings = Settings(
        dev_api_key="test-secret-12345",
        executor_backend="docker",
        executor_docker_image="kestrel-runtime:0.3.0",
    )
    app.dependency_overrides[get_settings] = lambda: settings

    with TestClient(app) as client:
        yield client


@pytest.fixture
def session_http_client_factory():
    """Factory fixture: ``_make(**settings_overrides)`` returns a TestClient
    whose ``get_settings`` dependency is overridden to a copy of the current
    Settings with the supplied fields swapped in.

    Used by Phase 6 substep 5 tests to vary ``stream_heartbeat_seconds`` and
    ``stream_backpressure_timeout_seconds`` per-test. Each client started via
    the factory is properly entered (so the FastAPI lifespan fires and the
    session registry is built) and is cleaned up in fixture teardown.
    """
    if not _docker_reachable():
        pytest.skip("docker daemon unreachable")

    entered: list[TestClient] = []

    def _make(**settings_overrides: Any) -> TestClient:
        app = create_app()
        defaults = get_settings()
        merged = defaults.model_copy(update=settings_overrides)
        app.dependency_overrides[get_settings] = lambda: merged
        client = TestClient(app)
        client.__enter__()
        entered.append(client)
        return client

    yield _make

    for client in entered:
        client.__exit__(None, None, None)

@pytest.fixture
async def redis_session_registry_factory():
    """Yields an async factory ``_make(**settings_overrides)`` that builds
    *started* RedisSessionRegistry instances pointed at the test Redis db.

    Unlike ``session_registry_factory`` this calls ``start()`` for you — the
    Redis backend needs a live connection before any method (including
    ``_sweep_once``) does anything. The test db is flushed before the test
    and after teardown, so tests start clean and never pollute each other.
    Calling ``_make`` more than once gives independent registries sharing one
    Redis db — that is how the cross-worker tests simulate two uvicorn workers.
    """
    if not _docker_reachable():
        pytest.skip("docker daemon unreachable")
    if not _redis_reachable():
        pytest.skip("redis unreachable")

    admin = redis.asyncio.Redis.from_url(TEST_REDIS_URL)
    await admin.flushdb()

    created: list[RedisSessionRegistry] = []

    async def _make(**overrides: Any) -> RedisSessionRegistry:
        defaults = {
            "dev_api_key": "",
            "execute_timeout_seconds": 5.0,
            "execute_output_cap_bytes": 1_048_576,
            "log_level": "INFO",
            "log_json": False,
            "executor_backend": "docker",
            "executor_docker_image": "kestrel-runtime:0.3.0",
            "session_idle_timeout_seconds": 900.0,
            "session_sweep_interval_seconds": 60.0,
            "session_backend": "redis",
            "redis_url": TEST_REDIS_URL,
        }
        defaults.update(overrides)
        settings = Settings(**defaults)
        registry = RedisSessionRegistry(settings=settings)
        await registry.start()
        created.append(registry)
        return registry

    try:
        yield _make
    finally:
        for registry in created:
            await registry.aclose()
        await admin.flushdb()
        await admin.aclose()


@pytest.fixture
async def redis_inspector():
    """A bare redis.asyncio client on the test db — for tests that need to
    inspect Redis directly (e.g. after a registry has been closed)."""
    if not _redis_reachable():
        pytest.skip("redis unreachable")
    client = redis.asyncio.Redis.from_url(TEST_REDIS_URL)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture(scope="session")
def postgres_migrations_applied() -> bool:
    """Run alembic upgrade head once per pytest session. Skips if Postgres
    is unreachable. Returns True so per-test fixtures can depend on it."""
    if not _postgres_reachable():
        pytest.skip("postgres unreachable")

    os.environ["KESTREL_DATABASE_URL"] = TEST_DATABASE_URL
    alembic_cfg = AlembicConfig("alembic.ini")
    command.upgrade(alembic_cfg, "head")
    return True


@pytest.fixture
async def postgres_engine(postgres_migrations_applied):
    """Async engine pointed at the test Postgres. Truncates audit_events
    + api_keys before yielding so each test starts with empty tables.
    Disposes in teardown."""
    engine = create_async_engine(TEST_DATABASE_URL, future=True)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE audit_events, api_keys"))
    yield engine
    await engine.dispose()


@pytest.fixture
async def postgres_audit_sink_factory(postgres_engine):
    """Yields an async factory ``_make(**settings_overrides)`` that builds
    *started* PostgresAuditSink instances bound to the per-test engine.
    Every sink is aclose()'d in teardown."""
    sinks: list[PostgresAuditSink] = []

    async def _make(**overrides: Any) -> PostgresAuditSink:
        defaults = {
            "audit_backend": "postgres",
            "database_url": TEST_DATABASE_URL,
            "audit_queue_max_size": 1000,
            "audit_shutdown_drain_seconds": 2.0,
        }
        defaults.update(overrides)
        settings = Settings(**defaults)
        sink = PostgresAuditSink(settings, postgres_engine)
        await sink.start()
        sinks.append(sink)
        return sink

    yield _make

    for sink in sinks:
        await sink.aclose()


@pytest.fixture
def audit_postgres_client(postgres_engine, monkeypatch):
    """TestClient with audit_backend=postgres + database_url=TEST_DATABASE_URL.
    Lifespan runs and binds a real PostgresAuditSink to app.state.audit_sink
    that writes to the test Postgres database.

    Mutates the env var before create_app() so the captured settings inside
    the lifespan see audit_backend='postgres'. Clears the get_settings cache
    on entry and teardown so other tests get fresh defaults."""
    if not _docker_reachable():
        pytest.skip("docker daemon unreachable")
    monkeypatch.setenv("KESTREL_AUDIT_BACKEND", "postgres")
    monkeypatch.setenv("KESTREL_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()

    app = create_app()
    with TestClient(app) as client:
        yield client

    get_settings.cache_clear()


@pytest.fixture
async def api_key_store_factory(postgres_engine):
    """Yields an async factory ``_make()`` that builds + starts a
    PostgresApiKeyStore bound to the per-test engine. Every store is
    aclose()'d in teardown."""
    stores: list[PostgresApiKeyStore] = []

    async def _make() -> PostgresApiKeyStore:
        store = PostgresApiKeyStore(postgres_engine)
        await store.start()
        stores.append(store)
        return store

    yield _make

    for store in stores:
        await store.aclose()


@pytest.fixture
def api_postgres_client(postgres_engine, monkeypatch):
    """TestClient with audit_backend=postgres + api_key_backend=postgres +
    dev_api_key='' (no dev shim). Real PostgresAuditSink + PostgresApiKeyStore
    bound to app.state. Used by store-only auth tests."""
    if not _docker_reachable():
        pytest.skip("docker daemon unreachable")
    monkeypatch.setenv("KESTREL_AUDIT_BACKEND", "postgres")
    monkeypatch.setenv("KESTREL_API_KEY_BACKEND", "postgres")
    monkeypatch.setenv("KESTREL_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("KESTREL_DEV_API_KEY", "")
    get_settings.cache_clear()

    app = create_app()
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


@pytest.fixture
def api_postgres_client_with_dev_shim(postgres_engine, monkeypatch):
    """Like api_postgres_client but with KESTREL_DEV_API_KEY set so the
    dev shim path is exercisable. Used by dev-shim auth tests."""
    if not _docker_reachable():
        pytest.skip("docker daemon unreachable")
    monkeypatch.setenv("KESTREL_AUDIT_BACKEND", "postgres")
    monkeypatch.setenv("KESTREL_API_KEY_BACKEND", "postgres")
    monkeypatch.setenv("KESTREL_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("KESTREL_DEV_API_KEY", "test-dev-shim-12345")
    get_settings.cache_clear()

    app = create_app()
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


@pytest.fixture
async def mint_api_key(api_key_store_factory):
    """Yields an async ``mint(label, scopes=None) -> (token, info)`` callable
    that creates API keys via a PostgresApiKeyStore on the per-test engine.
    The keys are visible to the app's own store (same DB)."""
    store = await api_key_store_factory()

    async def _mint(label: str = "test", scopes: list[str] | None = None):
        return await store.create(label, scopes)

    yield _mint