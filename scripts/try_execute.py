from __future__ import annotations

import asyncio

from kestrel.config import Settings
from kestrel.execution.manager import run_code


async def main() -> None:
    # 1. Success path
    s = Settings(execute_timeout_seconds=2.0, execute_output_cap_bytes=1024)
    r = await run_code('print("hello from child")', s)
    print("SUCCESS:", r.model_dump())

    # 2. Timeout path
    s = Settings(execute_timeout_seconds=0.5, execute_output_cap_bytes=1024)
    r = await run_code("while True: pass", s)
    print("TIMEOUT:", r.model_dump())

    # 3. Truncation path: print 100 KB, cap at 1 KB
    s = Settings(execute_timeout_seconds=5.0, execute_output_cap_bytes=1024)
    r = await run_code('print("x" * 100_000)', s)
    print(
        "TRUNC: len(stdout) =", len(r.stdout),
        "truncated =", r.stdout_truncated,
    )


if __name__ == "__main__":
    asyncio.run(main())