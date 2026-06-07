# Changelog

All notable changes to Kestrel are documented here. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions map to the eight
implementation phases in [`ROADMAP.md`](ROADMAP.md).

## [1.1.0] — 2026-06-07 — Per-request execute timeout

Post-v1.0 additive enhancement (not a phase). Backward-compatible — omitting the
new field reproduces the prior behavior exactly.

- `POST /execute` accepts an optional `timeout_seconds` field: a per-request
  wall-clock budget, clamped down to the `KESTREL_EXECUTE_TIMEOUT_SECONDS` server
  ceiling. A request may ask for a shorter budget but never exceed the configured
  maximum.
- `kestrel-client` SDK: `execute(code, *, timeout_seconds=...)` on both the sync
  and async clients.

## [1.0.0] — 2026-06-01 — Core complete

All eight phases implemented, tested, and documented. Marks Kestrel's core as
shipped (Brief §10/§11). Same code as `0.8.0`; the `1.0.0` tag denotes project
completion.

## [0.8.0] — 2026-06-01 — Polish & Ship

- One-command Docker Compose stack (API + Redis + PostgreSQL) and the `kestrel-api`
  control-plane image (docker-out-of-docker).
- `kestrel-client` Python SDK — sync + async clients, polling and WebSocket
  streaming, typed errors.
- LLM-agent integration examples (OpenAI, Anthropic, LangChain).
- Documentation suite: README rewrite + architecture diagram, architecture, API
  reference, deployment, security, contributing — and publish/deploy recipes.

## [0.7.0] — 2026-05-31 — Observability & Management

- Prometheus `/metrics` endpoint and request-ID tracing across logs.
- PostgreSQL audit log of every execution (fire-and-forget via a bounded queue).
- API-key store (sha256-hashed `kestrel_` tokens) and the `kestrel-keys` operator CLI.
- Per-API-key token-bucket rate limits (memory or Redis), with `Retry-After`.
- Admin endpoints for keys, sessions, and the audit log (admin-scope gated).

## [0.6.0] — 2026-05-22 — Streaming

- WebSocket streaming endpoint with a typed JSON message protocol (stdout/stderr/
  heartbeat/result/error) over a backward-compatible multi-line kernel.
- Application-layer heartbeats, back-pressure safety, and client-disconnect teardown.
- HTTP long-poll / short-poll fallback for WebSocket-blocked clients.

## [0.5.0] — 2026-05-17 — Rich Outputs

- matplotlib plot capture (base64 PNG), pandas DataFrame capture (JSON via the
  last-expression rule), and file outputs from a watched tmpfs directory.
- Per-output and per-execute size caps with a `dropped_outputs` report.

## [0.4.0] — 2026-05-14 — Session State

- Sessions: a persistent Python REPL kernel per session over a JSON-line protocol.
- In-memory and Redis-backed session registries; container warm pool; idle sweep.
- Session list/terminate endpoints.

## [0.3.0] — 2026-05-04 — Security & Resource Limits

- Memory/CPU/PID caps, `--network none`, read-only rootfs + tmpfs, non-root user,
  `--cap-drop ALL`, `no-new-privileges`, seccomp.
- Output size caps and an adversarial isolation test suite.

## [0.2.0] — 2026-04-28 — Docker Execution

- Replaced the subprocess runner with Docker-based execution: the `kestrel-runtime`
  image, an asyncio Docker-CLI driver, timeout `SIGKILL`, and startup orphan sweep.
- *(Tag backfilled during Phase 8 at commit `df2d45d`.)*

## [0.1.0] — 2026-04-26 — Foundation

- FastAPI app with `/health` and `/execute`, a subprocess runner (later replaced),
  Pydantic request/response models, bearer-token auth stub, structlog, and the
  first integration tests.
