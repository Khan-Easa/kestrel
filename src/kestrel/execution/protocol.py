from __future__ import annotations

from typing import Protocol, runtime_checkable

from kestrel.api.schemas import ExecuteResponse
from kestrel.config import Settings


@runtime_checkable
class Executor(Protocol):
    """Contract for any code-execution backend (subprocess, Docker, ...).

    Implementations must expose an async ``run`` method that takes user code
    plus the current Settings and returns a fully populated ExecuteResponse.
    """

    async def run(self, code: str, settings: Settings) -> ExecuteResponse: ...