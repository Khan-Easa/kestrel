from __future__ import annotations

from functools import lru_cache

from kestrel.execution.manager import SubprocessExecutor
from kestrel.execution.protocol import Executor


@lru_cache(maxsize=1)
def get_executor() -> Executor:
    """Return the process-wide executor singleton.

    FastAPI calls this via ``Depends(get_executor)``. The ``lru_cache`` ensures
    a single instance is shared across requests, which is the right default for
    stateless executors and the only sane default for stateful ones (Phase 2+).

    To swap implementations in tests, use ``app.dependency_overrides[get_executor] = ...``.
    """
    return SubprocessExecutor()