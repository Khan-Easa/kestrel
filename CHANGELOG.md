# Changelog

All notable changes to Kestrel are documented here. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions map to the eight
implementation phases in [`ROADMAP.md`](ROADMAP.md).

## [Unreleased]

Phase 8 (Polish & Ship) in progress: one-command Docker Compose stack, the
`kestrel-api` image, the `kestrel-client` Python SDK, LLM-agent integration
examples, and the documentation suite (README rewrite, architecture, API
reference, deployment guide, security). To be tagged `v0.8.0` (and `v1.0.0` at
project completion).

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
