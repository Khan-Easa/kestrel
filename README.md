# Kestrel

Self-hosted Python code execution service. Send code over HTTP, get back captured output.

**Status:** Phase 1 of 7 — single-node service with a subprocess executor. Docker-based isolation lands in Phase 2 (see `ROADMAP.md`).

## Requirements

- Python 3.11
- [uv](https://docs.astral.sh/uv/) for dependency management

## Quickstart

```bash
# Install dependencies (creates .venv, installs runtime + dev extras)
uv sync --extra dev

# Run the server (auth disabled by default — see Authentication below)
uv run uvicorn kestrel.app:create_app --factory --reload --port 8000
```

In another shell:

```bash
# Liveness check
curl http://localhost:8000/health

# Execute some code
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "print(2 + 2)"}'
```

Expected response:

```json
{
  "stdout": "4\n",
  "stderr": "",
  "exit_code": 0,
  "duration_ms": 42,
  "timed_out": false,
  "stdout_truncated": false,
  "stderr_truncated": false
}
```

The server also publishes interactive API docs at `http://localhost:8000/docs` (Swagger UI) and `/redoc`.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/health` | open | Liveness probe. Returns `{"status": "ok"}`. |
| `POST` | `/execute` | bearer (if configured) | Run Python code in a subprocess. |

Every response carries an `X-Request-ID` header. If the client supplies one in the request, it is echoed back; otherwise a fresh UUID is generated. Useful for correlating client logs with server logs.

## Configuration

All settings come from environment variables prefixed `KESTREL_`. Defaults are defined in `src/kestrel/config.py`.

| Variable | Default | Description |
|---|---|---|
| `KESTREL_DEV_API_KEY` | `""` | Bearer token required by `/execute`. Empty value disables auth. |
| `KESTREL_EXECUTE_TIMEOUT_SECONDS` | `5.0` | Maximum subprocess wall time before SIGKILL. |
| `KESTREL_EXECUTE_OUTPUT_CAP_BYTES` | `1048576` | Per-stream truncation cap (stdout and stderr each). |
| `KESTREL_LOG_LEVEL` | `INFO` | Root log level. |
| `KESTREL_LOG_JSON` | `False` | `True` for one-line JSON logs; `False` for colored console output. |

## Authentication

`/execute` is gated by a bearer token. The default `KESTREL_DEV_API_KEY=""` disables the gate entirely — useful for local development. To enable auth, set the variable and restart the server:

```bash
KESTREL_DEV_API_KEY="some-long-secret" uv run uvicorn kestrel.app:create_app --factory --port 8000
```

Clients then send the token as `Authorization: Bearer some-long-secret`. Missing or wrong tokens get `HTTP 401`. `/health` is unauthenticated regardless.

## Tests

```bash
uv run pytest -v          # full suite (~1s)
uv run pytest -v -s       # show structured log lines
uv run pytest -k auth     # run a subset by name
```

## Project layout

```
src/kestrel/
├── config.py              Settings + get_settings (lru_cache singleton)
├── app.py                 create_app factory: logging + middleware + routes
├── logging.py             configure_logging (structlog + stdlib bridge)
├── api/
│   ├── auth.py            require_api_key dependency
│   ├── routes.py          GET /health, POST /execute
│   └── schemas.py         ExecuteRequest, ExecuteResponse
└── execution/
    └── manager.py         run_code (asyncio subprocess + timeout + output cap)

tests/integration/test_execute.py    10 tests covering all behaviors
```

## Roadmap

Phase 1 (this) is intentionally minimal: no container isolation, no resource limits, no sessions, no persistence. See `ROADMAP.md` for the full 7-phase plan.

## License

MIT — see `LICENSE`.
