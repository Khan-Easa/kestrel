# Contributing to Kestrel

Kestrel is a learning project built incrementally across eight phases (see
[`ROADMAP.md`](ROADMAP.md)). Contributions and forks are welcome; this guide covers
the dev setup and the conventions the codebase follows.

## Development setup

```bash
uv sync --extra dev      # create the venv with runtime + dev dependencies
uv run pytest -v         # run the test suite
```

Always invoke tools through **`uv run`** (`uv run pytest`, `uv run uvicorn …`) — the
project standardises on [uv](https://docs.astral.sh/uv/) for reproducible
environments. Python is pinned to 3.11.

### Test prerequisites

The suite layers tests by the infrastructure they need; tests skip cleanly when a
dependency isn't reachable:

- **Docker** (daemon running) — execution, isolation, and session tests. Build the
  runtime image first: `docker build -t kestrel-runtime:0.5.0 docker/executor/`.
- **Redis** — the Redis-backed session/rate-limit tests:
  `docker run -d --name kestrel-redis -p 6379:6379 redis:7-alpine`.
- **PostgreSQL** — the audit-log and API-key tests:
  `docker run -d --name kestrel-postgres -p 5432:5432 -e POSTGRES_PASSWORD=kestrel postgres:16-alpine`.

The SDK has its own suite: `cd clients/python && uv sync --extra dev && uv run pytest`.

## Conventions

- **Factory pattern** — app construction goes through `create_app()`; startup hooks
  (logging, middleware, lifespan, routers) live there.
- **Dependency injection** — handlers take `Settings` and backends via `Depends(...)`,
  never by calling globals; this is what makes them overrideable in tests.
- **Swappable backends via `Protocol`** — executor, session registry, audit sink,
  key store, and rate limiter are interfaces selected by config; routes import the
  Protocol, never a concrete class.
- **Structured logging only** — `logger.info("event_name", key=value)`, never `print`
  or f-strings into the message. Event names are snake_case.
- **Never log payloads or secrets** — log `len(code)`, never the code; log a
  session-id *prefix*, never the full capability.
- **Constant-time secret comparison** — use `secrets.compare_digest`.
- **Test the contract, not the implementation** — assert on status codes and response
  shapes; don't pin error wording or log-line format.
- **Type hints everywhere**, `pathlib` over `os.path`, no bare `except`.

## Security-sensitive changes

Anything touching the Docker run flags, the sandbox image, auth, the output caps, or
the timeout is security-critical. Don't weaken a control without understanding the
attack it prevents (see [`SECURITY.md`](SECURITY.md)), and add/extend an adversarial
test in `tests/integration/test_isolation.py` for new isolation behaviour. Never use
privileged containers or mount the Docker socket into a sandbox.

## Design decisions

Significant decisions are recorded in [`DECISIONS.md`](DECISIONS.md), grouped by
phase, with the rejected alternatives and rationale. Read the relevant entries
before relitigating a locked decision; if you're proposing a change, add or
supersede an entry rather than silently diverging.

## Pull requests

- Branch from `main`; keep changes scoped to one logical step.
- Run the full test suite (with Docker, ideally Redis + Postgres too) before opening a PR.
- Plain imperative commit messages, no Conventional-Commits prefix.
- Update the docs when behaviour changes — the public API, schemas, and these docs
  are part of the contract.
