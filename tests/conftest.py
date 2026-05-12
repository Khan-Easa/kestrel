import shutil
import subprocess
from collections.abc import Callable
from functools import lru_cache
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kestrel.app import create_app
from kestrel.config import Settings, get_settings
from kestrel.execution import get_executor
from kestrel.execution.docker_executor import DockerExecutor
from kestrel.execution.manager import SubprocessExecutor
from kestrel.execution.session_runtime import SessionRuntime
from kestrel.execution.session_registry import SessionRegistry

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

    async def _make(timeout_seconds: float = 5.0) -> SessionRuntime:
        runtime = await SessionRuntime.start(
            image_tag="kestrel-runtime:0.3.0",
            timeout_seconds=timeout_seconds,
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
    SessionRegistry instances with reasonable test defaults. Every registry
    created is aclose()'d in teardown — aclose is idempotent so tests may
    call it explicitly too.

    By default the background sweeper task is NOT started; tests drive
    eviction by calling ``await registry._sweep_once(timeout_seconds=...)``
    directly so no real timers are involved.
    """
    if not _docker_reachable():
        pytest.skip("docker daemon unreachable")

    created: list[SessionRegistry] = []

    async def _make(**overrides: Any) -> SessionRegistry:
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
        registry = SessionRegistry(settings=settings)
        created.append(registry)
        return registry

    try:
        yield _make
    finally:
        for registry in created:
            await registry.aclose()