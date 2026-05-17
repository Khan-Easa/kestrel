# Roadmap

See `Kestrel_Project_Brief.pdf` §6 for authoritative phase definitions.

- **Phase 1 — Foundation** ✅ *(complete)*: FastAPI skeleton, subprocess runner, bearer-token stub, structlog, pytest.
- **Phase 2 — Docker Execution** ✅ *(complete)*: containerised execution (`kestrel-runtime` image, asyncio docker CLI driver, orphan sweep).
- **Phase 3 — Security & Resource Limits** ✅ *(complete)*: CPU/memory/pids caps, network isolation, read-only rootfs, non-root user, dropped caps, seccomp, adversarial isolation tests.
- **Phase 4 — Session State** ✅ *(complete — 2026-05-14)*: persistent REPL kernel, JSON-line protocol, in-memory + Redis-backed session registry, container warm pool, list/terminate endpoints.
- **Phase 5 — Rich Outputs** ✅ *(complete — 2026-05-17)*: matplotlib plot capture as base64 PNG, pandas DataFrame → JSON via AST last-expression rule, file outputs via watched `/workspace/outputs/` tmpfs, MIME-type handling, per-output + per-execute total size caps with `dropped_outputs` surfacing.
- **Phase 6 — Streaming**: WebSocket endpoint, line-by-line stdout streaming, client-disconnect handling, polling fallback.
- **Phase 7 — Observability & Management**: Prometheus metrics, request-ID tracing, PostgreSQL audit log, API-key management CLI, per-key rate limits, admin endpoints.
- **Phase 8 — Polish & Ship**: README + architecture diagram, full API docs, one-command Docker Compose, image on Docker Hub, Python SDK, integration examples, demo deploy.
