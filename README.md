# Kestrel

Self-hosted, sandboxed Python code execution service.

**Status:** Phase 1 (Foundation) — `/health` and `/execute` endpoints backed by a throwaway subprocess runner. Docker-based isolation lands in Phase 2.

## Requirements

- Python 3.11
- [uv](https://docs.astral.sh/uv/) for dependency management

## Quick start

```bash
# 1. Install dependencies
uv sync --extra dev

# 2. Set the dev API key (Phase 1 uses a single static bearer token)
export KESTREL_DEV_API_KEY="dev-secret-change-me"

# 3. Run the server
uv run uvicorn kestrel.main:app --reload --port 8000

# 4. In another shell, hit the endpoints
curl http://localhost:8000/health

curl -X POST http://localhost:8000/execute \
  -H "Authorization: Bearer $KESTREL_DEV_API_KEY" \
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
  "output_truncated": false
}
```

## Running tests

```bash
uv run pytest
```

## Project layout

See `DESIGN.md` for the full module map. Phase 1 populates:

- `src/kestrel/main.py` — FastAPI entrypoint
- `src/kestrel/api/` — routes + schemas
- `src/kestrel/execution/manager.py` — throwaway subprocess runner (replaced in Phase 2)
- `src/kestrel/config.py` — pydantic-settings
- `src/kestrel/auth.py` — bearer-token dependency
- `src/kestrel/observability/logging.py` — structlog setup

## Scope

Phase 1 is intentionally minimal. No Docker, no resource limits, no sessions, no persistence, no rate limiting. Do not pull those forward — see `ROADMAP.md`.
