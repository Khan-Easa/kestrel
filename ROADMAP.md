# Roadmap

See `Kestrel_Project_Brief.pdf` §6 for authoritative phase definitions.

- **Phase 1 — Foundation** ✅ *(complete)*: FastAPI skeleton, subprocess runner, bearer-token stub, structlog, pytest.
- **Phase 2 — Docker Execution** ✅ *(complete)*: containerised execution (`kestrel-runtime` image, asyncio docker CLI driver, orphan sweep).
- **Phase 3 — Security & Resource Limits** ✅ *(complete)*: CPU/memory/pids caps, network isolation, read-only rootfs, non-root user, dropped caps, seccomp, adversarial isolation tests.
- **Phase 4 — Session State** ✅ *(complete — 2026-05-14)*: persistent REPL kernel, JSON-line protocol, in-memory + Redis-backed session registry, container warm pool, list/terminate endpoints.
- **Phase 5 — Rich Outputs** ✅ *(complete — 2026-05-17)*: matplotlib plot capture as base64 PNG, pandas DataFrame → JSON via AST last-expression rule, file outputs via watched `/workspace/outputs/` tmpfs, MIME-type handling, per-output + per-execute total size caps with `dropped_outputs` surfacing.
- **Phase 6 — Streaming** ✅ *(complete — 2026-05-22)*: WebSocket streaming endpoint, typed JSON message protocol (stdout/stderr/heartbeat/result/error discriminated union) over a backward-compatible multi-line streaming kernel, application-layer heartbeats, back-pressure safety, client-disconnect → kernel teardown, and an HTTP long-poll/short-poll fallback for WebSocket-blocked clients.
- **Phase 7 — Observability & Management** ✅ *(complete — 2026-05-31)*: Prometheus metrics, request-ID tracing, PostgreSQL audit log, API-key management CLI, per-key rate limits, admin endpoints. Delivered in 7 substeps per `DECISIONS.md` `7-scope`.
- **Phase 8 — Polish & Ship**: README + architecture diagram, full API docs, one-command Docker Compose, image on Docker Hub, Python SDK, integration examples, demo deploy.
