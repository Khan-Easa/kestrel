# Roadmap

See `Kestrel_Project_Brief.pdf` §6 for authoritative phase definitions.

- **Phase 1 — Foundation** *(in progress)*: FastAPI skeleton, subprocess runner, bearer-token stub, structlog, pytest.
- **Phase 2 — Docker isolation**: replace subprocess with containerised execution.
- **Phase 3 — Resource limits**: CPU/memory/pids caps, disk quotas.
- **Phase 4 — Sessions**: stateful kernels, Redis-backed session registry.
- **Phase 5 — Persistence**: Postgres for audit + session metadata.
- **Phase 6 — Rich outputs / streaming**: incremental results, media handling.
- **Phase 7 — Auth hardening**: real API keys, hashing, per-key policy.
