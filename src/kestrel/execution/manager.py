from __future__ import annotations

import asyncio
import sys
import time

from kestrel.api.schemas import ExecuteResponse
from kestrel.config import Settings


async def _read_stream(stream: asyncio.StreamReader, cap_bytes: int) -> tuple[bytes, bool]:
    """Read the stream until it ends or we hit the byte cap."""
    buf = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > cap_bytes:
            truncated = True
            del buf[cap_bytes:]
            break
    return bytes(buf), truncated


class SubprocessExecutor:
    """Phase 1 executor: runs user code in a local Python subprocess.

    Satisfies the ``Executor`` protocol structurally — no explicit inheritance.
    Will be replaced by ``DockerExecutor`` in Phase 2 Step 4; the route handler
    won't need to change.
    """

    async def run(self, code: str, settings: Settings) -> ExecuteResponse:
        start = time.perf_counter()

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        read_both = asyncio.gather(
            _read_stream(proc.stdout, settings.execute_output_cap_bytes),
            _read_stream(proc.stderr, settings.execute_output_cap_bytes),
        )

        timed_out = False
        try:
            (stdout_bytes, stdout_truncated), (stderr_bytes, stderr_truncated) = (
                await asyncio.wait_for(read_both, timeout=settings.execute_timeout_seconds)
            )
            exit_code = await proc.wait()
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            await proc.wait()
            stdout_bytes, stdout_truncated = b"", False
            stderr_bytes, stderr_truncated = b"", False
            exit_code = -1
        duration_ms = int((time.perf_counter() - start) * 1000)

        return ExecuteResponse(
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            exit_code=exit_code,
            duration_ms=duration_ms,
            timed_out=timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )