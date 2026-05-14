# Roadmap

See `Kestrel_Project_Brief.pdf` §6 for authoritative phase definitions.

- **Phase 1 — Foundation** ✅ *(complete)*: FastAPI skeleton, subprocess runner, bearer-token stub, structlog, pytest.
- **Phase 2 — Docker isolation** ✅ *(complete)*: replace subprocess with containerised execution.
- **Phase 3 — Resource limits** ✅ *(complete)*: CPU/memory/pids caps, disk quotas.
- **Phase 4 — Sessions** ✅ *(complete — 2026-05-14)*: stateful kernels, persistent REPL, in-memory + Redis-backed session registry, container warm pool, list/terminate endpoints.
- **Phase 5 — Persistence**: Postgres for audit + session metadata.
- **Phase 6 — Rich outputs / streaming**: incremental results, media handling.
- **Phase 7 — Auth hardening**: real API keys, hashing, per-key policy.

> Note: the per-phase scope here diverges from `Kestrel_Project_Brief.pdf` §6 for Phases 5+ (the Brief has 8 phases — 5 Rich Outputs, 6 Streaming, 7 Observability, 8 Polish). The Brief is authoritative; reconcile this list to it before starting Phase 5.
