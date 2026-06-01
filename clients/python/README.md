# kestrel-client

Python client SDK for [Kestrel](https://github.com/Khan-Easa/kestrel) — a self-hosted, sandboxed Python code execution service for LLM agents.

Sync (`KestrelClient`) and async (`AsyncKestrelClient`) clients cover stateless
execution, sessions, rich outputs, streaming, and the polling fallback. The only
runtime dependencies are `httpx` and `websockets`.

## Install

```bash
pip install kestrel-client
```

## Quickstart (sync)

```python
from kestrel_client import KestrelClient

with KestrelClient("http://localhost:8000", api_key="kestrel_...") as kestrel:
    # Stateless one-shot execution
    result = kestrel.execute("print(2 + 2)")
    print(result.stdout)          # "4\n"
    print(result.exit_code)       # 0

    # A stateful session — variables persist across executes
    session = kestrel.create_session()
    kestrel.session_execute(session.session_id, "x = 41")
    result = kestrel.session_execute(session.session_id, "print(x + 1)")
    print(result.stdout)          # "42\n"

    # Stream output as it is produced (sync uses the HTTP polling fallback)
    for message in kestrel.stream(session.session_id, "for i in range(3): print(i)"):
        print(message)

    kestrel.delete_session(session.session_id)
```

## Quickstart (async)

```python
import asyncio
from kestrel_client import AsyncKestrelClient

async def main():
    async with AsyncKestrelClient("http://localhost:8000", api_key="kestrel_...") as kestrel:
        result = await kestrel.execute("print(2 + 2)")
        print(result.stdout)

        session = await kestrel.create_session()
        # Async streaming rides a real WebSocket
        async for message in kestrel.stream(session.session_id, "for i in range(3): print(i)"):
            print(message)
        await kestrel.delete_session(session.session_id)

asyncio.run(main())
```

## Results vs errors

Execution *outcomes* are returned as data, never raised. A timeout or a non-zero
exit code comes back on the result object:

```python
result = kestrel.execute("while True: pass")
assert result.timed_out is True      # not an exception
```

Exceptions are raised only for HTTP-transport failures, all under `KestrelError`:

| Exception | When |
|---|---|
| `AuthenticationError` | 401 — key missing or rejected |
| `SessionNotFoundError` | 404 — session expired or unknown |
| `SessionBusyError` | 409 — a run is already in flight |
| `SessionGoneError` | 410 — the session's container died |
| `RateLimitedError` | 429 — rate limit exceeded (has `.retry_after`) |
| `KestrelAPIError` | any other 4xx/5xx (has `.status_code`, `.detail`) |

## Rich outputs

`session_execute` returns captured plots, DataFrames, and files on `result.outputs`:

```python
result = kestrel.session_execute(session_id, "import pandas as pd; pd.DataFrame({'a': [1, 2]})")
for output in result.outputs:
    print(output.type)   # "plot" | "dataframe" | "file"
```

## Streaming messages

`stream()` yields a sequence of typed messages, ending with a `ResultMessage`:

- `StdoutChunk(data=...)` / `StderrChunk(data=...)` — output as it is written
- `Heartbeat(elapsed_ms=...)` — keep-alive during silent intervals
- `ResultMessage(result=SessionExecuteResult, ...)` — the final result (terminal)
- `ErrorMessage(code=..., detail=...)` — a stream-level error (terminal)

## License

MIT.
