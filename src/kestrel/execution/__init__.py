from __future__ import annotations

from functools import lru_cache

from kestrel.config import Settings, get_settings
from kestrel.execution.docker_executor import DockerExecutor
from kestrel.execution.manager import SubprocessExecutor
from kestrel.execution.protocol import Executor
from kestrel.execution.session_registry import (InMemorySessionRegistry, SessionRegistry,)


@lru_cache(maxsize=1)
def get_executor() -> Executor:
    """Return the process-wide executor singleton.

    Reads ``executor_backend`` from settings on first call to pick the
    implementation. Settings are themselves cached, so this resolves once per
    process; restart the process to change backends.

    To swap implementations in tests, use ``app.dependency_overrides[get_executor] = ...``.
    """
    settings = get_settings()
    if settings.executor_backend == "docker":
        return DockerExecutor(image_tag=settings.executor_docker_image)
    return SubprocessExecutor()

def build_session_registry(settings: Settings) -> SessionRegistry:
    """Build the session-registry backend named by ``settings.session_backend``.

    Called once at app startup (the FastAPI lifespan). Unlike ``get_executor``
    this is not a cached DI provider — the registry is a stateful singleton
    owned by ``app.state`` for the process lifetime, so it is built explicitly,
    not memoised.
    """
    if settings.session_backend == "redis":
        from kestrel.execution.redis_session_registry import RedisSessionRegistry

        return RedisSessionRegistry(settings)
    return InMemorySessionRegistry(settings)