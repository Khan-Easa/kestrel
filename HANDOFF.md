# Kestrel — Session Handoff

**Last updated:** 2026-04-24 (paused mid-session for ~2-hour break)
**Current phase:** Phase 1 — Foundation, **in progress** (walkthrough/learning mode)
**Active environment:** WSL (`/mnt/d/My_project/Kestrel`)

---

## Important context for the next session

This project is being built as a **learning exercise** for the user (Python + software-engineering skills). Earlier the assistant scaffolded all of Phase 1 autonomously; the user then wiped it and asked for **guided walkthrough mode**, where the assistant explains each step and the user writes/runs everything himself.

- Treat the user as a beginner. Explain every concept (not just syntax) before showing code.
- Deliver work in **small steps**. Wait for the user to confirm each step before moving on.
- After each completed step, **read the user's file** and verify it before instructing the next step. (User explicitly asked for this after a typo slipped through.)
- After giving an explanation in chat, silently append a matching section to **`LEARNING_NOTES.md`** (the file is his personal offline study log; already listed in `.gitignore`).
- Memory under `~/.claude/projects/-mnt-d-My-project-Kestrel/memory/` has full details on collaboration style and the LEARNING_NOTES policy.

## Where we stopped today (2026-04-24)

Completed since yesterday's handoff:

- **`src/kestrel/config.py`** — full `Settings(BaseSettings)` class with `model_config` and 5 fields (`dev_api_key`, `execute_timeout_seconds` [`gt=0`], `execute_output_cap_bytes` [`gt=0`], `log_level`, `log_json`) plus `get_settings()` cached with `@lru_cache(maxsize=1)`. **Verified** — defaults load cleanly via `uv run python -c "from kestrel.config import get_settings; print(get_settings())"`. (One real-world hiccup: the original `uv sync` predated any source files, so the editable install link was missing. Fixed with `uv sync --reinstall-package kestrel`. Recorded in LEARNING_NOTES and memory.)
- **`src/kestrel/api/__init__.py`** (empty) and **`src/kestrel/api/schemas.py`** — `ExecuteRequest` (one required `code: str`, `min_length=1`, `max_length=100_000`) and `ExecuteResponse` (`stdout`, `stderr`, `exit_code`, `duration_ms` [`ge=0`], `timed_out`, `stdout_truncated`, `stderr_truncated` — all with sensible defaults). **Verified** by instantiating both and confirming empty `code` raises `ValidationError`.
- **`src/kestrel/execution/__init__.py`** (empty) — package marker for the next module.
- **Asyncio orientation delivered** (Step 7.2, no code) — concepts already covered: why async exists, `async def` / `await`, the executor's six responsibilities, `asyncio.gather` for concurrent stdout/stderr reads (and the pipe-buffer deadlock it prevents), `asyncio.wait_for` for the timeout pattern, and the planned shape of `manager.py`.

## Resume point

Next step is **Step 7.3 — write the imports of `src/kestrel/execution/manager.py`**.

Planned imports (will explain each at resume time):

```python
from __future__ import annotations

import asyncio
import sys
import time

from kestrel.api.schemas import ExecuteResponse
from kestrel.config import Settings
```

Then in order:

- **7.4** — `_read_stream` helper coroutine (reads bytes from one pipe, stops at the byte cap, returns `(bytes, was_truncated)`).
- **7.5** — `run_code` main coroutine: subprocess spawn (`asyncio.create_subprocess_exec`) → `asyncio.gather` for concurrent stdout/stderr reads → `asyncio.wait_for` for the wall-clock timeout → kill on timeout → assemble `ExecuteResponse`.
- **7.6** — verification with a short script exercising the success path, the timeout path, and the truncation path.

**Do not re-teach** at resume: the asyncio orientation already covered in 7.2 (`async def`/`await`, why concurrent reads, `gather`, `wait_for`).

## Remaining Phase 1 work after the executor

Already drafted in the earlier autonomous run; will be re-built step-by-step in the walkthrough:

1. `src/kestrel/auth.py` — `HTTPBearer` FastAPI dependency.
2. `src/kestrel/observability/logging.py` — structlog configuration.
3. `src/kestrel/api/routes.py` — `GET /health`, `POST /execute`.
4. `src/kestrel/main.py` — `create_app()` factory + lifespan hook.
5. `tests/conftest.py` + `tests/integration/test_execute.py`.
6. `uv run pytest` — verify.
7. Boot uvicorn, curl both endpoints.

Plus supporting directories that still need creating when their respective steps come up: `src/kestrel/observability/`, `src/kestrel/sessions/`, `src/kestrel/db/migrations/`, `tests/{unit,integration,security}/`, `docker/executor/`, `scripts/`, `docs/examples/`. Each will get an `__init__.py` as needed.

## Open items / blockers

- **Docker in WSL:** `docker` CLI is not on PATH in this shell. Irrelevant for Phase 1 (no Docker yet), must be fixed before Phase 2. Memory note exists.
- **Git init:** user declined for now. `.pre-commit-config.yaml` not yet written (will add when ready to init git).
- **Ruff style cleanup:** the user's hand-typed code has consistent PEP 8 spacing nits (spaces around `=` in keyword args, single blank line where two are expected, etc.). All harmless; `ruff --fix` will normalize when we wire it up.
- **Editable install gotcha:** documented above. If new files appear under `src/kestrel/` and import fails despite a successful `uv sync`, run `uv sync --reinstall-package kestrel`.

## Scope reminders (do NOT pull forward)

Per brief §10 — resist adding: multi-language, GPU, persistent storage, runtime pip install, custom per-user images, web UI, billing, multi-tenant, k8s, HA, collaboration, gVisor/Firecracker.

Phase 1-specific: no Docker execution, no resource limits, no sessions, no Redis, no Postgres, no rate limiting, no rich outputs, no streaming, no audit logging. Those are Phases 2–7.
