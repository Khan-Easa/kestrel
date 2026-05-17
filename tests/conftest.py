import shutil
import subprocess
from collections.abc import Callable
from functools import lru_cache
from typing import Any

import pytest
from fastapi.testclient import TestClient
import redis.asyncio

from kestrel.app import create_app
from kestrel.config import Settings, get_settings
from kestrel.execution import get_executor
from kestrel.execution.docker_executor import DockerExecutor
from kestrel.execution.manager import SubprocessExecutor
from kestrel.execution.session_runtime import SessionRuntime
from kestrel.execution.session_registry import InMemorySessionRegistry
from kestrel.execution.redis_session_registry import RedisSessionRegistry

TEST_REDIS_URL = "redis://localhost:6379/15"  # db 15 — isolated from real data

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
    return TestClient(app)


@pytest.fixture
def docker_client() -> TestClient:
    """Always-Docker variant for isolation-specific tests."""
    if not _docker_reachable():
        pytest.skip("docker daemon unreachable")

    app = create_app()
    app.dependency_overrides[get_executor] = lambda: DockerExecutor()
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