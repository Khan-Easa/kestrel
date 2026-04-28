from __future__ import annotations

from functools import lru_cache

from kestrel.config import get_settings
from kestrel.execution.docker_executor import DockerExecutor
from kestrel.execution.manager import SubprocessExecutor
from kestrel.execution.protocol import Executor


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