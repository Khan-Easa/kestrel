# Deploying Kestrel

Kestrel is designed to self-host on a single node with Docker Compose: the API
container, Redis, and PostgreSQL. This guide covers the stack, configuration, and
the production checklist.

## Prerequisites

- Docker Engine with the daemon running (the API talks to it directly).
- The repository checked out (the stack builds the API image from it).

## The one-command stack

```bash
# 1. Build the sandbox runtime image on the host daemon (once, and after kernel changes).
docker build -t kestrel-runtime:0.5.0 docker/executor/

# 2. Bring up API + Redis + PostgreSQL. Migrations run automatically on API start.
docker compose up -d --build

# 3. Mint the first API key.
docker compose exec api kestrel-keys create my-key --scope admin
```

`docker compose down` stops the stack (keeps the Postgres volume); `down -v` also
deletes the database.

### Why the runtime image is a manual prestep

The API container does not run the sandboxes itself — it asks the **host's** Docker
daemon to launch them (see [docker-out-of-docker](#docker-out-of-docker) below). So
the `kestrel-runtime` image must exist on the host daemon, which is why it's a
`docker build` step rather than a Compose service. Rebuild it whenever
`docker/executor/` (the kernel or its dependencies) changes.

## docker-out-of-docker

The `api` service mounts the host Docker socket (`/var/run/docker.sock`) and the
Docker CLI to spawn sandbox containers on the host daemon — they run as *siblings*
of the API container, not nested inside it.

This is a deliberate trust split: **the API is the trusted control plane** (its job
is to manage containers), while **the sandbox containers it launches never get the
socket** and keep every isolation control. Mounting the Docker socket into a
container is equivalent to giving it root on the host — acceptable for the API,
never for anything running untrusted code.

Because the host daemon creates the sandboxes, the bind-mounted code file must
resolve to the same path on both sides. The Compose file mounts a **shared spool
directory** at an identical host:container path (`/var/kestrel/spool`) and points
`KESTREL_EXEC_SPOOL_DIR` at it. Keep those three in sync (host mount, container
mount, env var) if you change them.

## Startup ordering

The API `depends_on` Redis and PostgreSQL with `condition: service_healthy`, so it
waits for both to pass their healthchecks before booting. The entrypoint then runs
`alembic upgrade head` (only when `KESTREL_DATABASE_URL` is set) before starting
uvicorn — so a fresh database is migrated automatically, and there's no race
against a not-yet-ready Postgres.

## Configuration

All settings are environment variables prefixed `KESTREL_`. Defaults are in
[`src/kestrel/config.py`](../src/kestrel/config.py).

| Variable | Default | Purpose |
|---|---|---|
| `KESTREL_DEV_API_KEY` | `""` | Dev-shim bearer token. Empty disables the shim. |
| `KESTREL_EXECUTOR_BACKEND` | `docker` | `docker` (sandboxed) or `subprocess` (dev only). |
| `KESTREL_EXECUTOR_DOCKER_IMAGE` | `kestrel-runtime:0.5.0` | Sandbox image tag. |
| `KESTREL_EXEC_SPOOL_DIR` | `""` | Shared spool dir for docker-out-of-docker (see above). |
| `KESTREL_EXECUTE_TIMEOUT_SECONDS` | `5.0` | Per-execute wall-clock kill. |
| `KESTREL_EXECUTE_OUTPUT_CAP_BYTES` | `65536` | Per-stream output cap. |
| `KESTREL_SESSION_BACKEND` | `memory` | `memory` or `redis` (multi-worker). |
| `KESTREL_REDIS_URL` | `redis://localhost:6379/0` | Used when `session_backend=redis`. |
| `KESTREL_SESSION_IDLE_TIMEOUT_SECONDS` | `900` | Idle session eviction. |
| `KESTREL_SESSION_POOL_SIZE` | `0` | Warm-pool size (0 = disabled). |
| `KESTREL_AUDIT_BACKEND` | `null` | `null` or `postgres`. |
| `KESTREL_API_KEY_BACKEND` | `null` | `null` (dev shim only) or `postgres`. |
| `KESTREL_DATABASE_URL` | `""` | Postgres URL; required for either `postgres` backend. |
| `KESTREL_RATE_LIMIT_EXECUTE_PER_MINUTE` | `60` | Token-bucket limit, `execute` class. |
| `KESTREL_RATE_LIMIT_SESSION_LIFECYCLE_PER_MINUTE` | `300` | …`session_lifecycle` class. |
| `KESTREL_RATE_LIMIT_ADMIN_PER_MINUTE` | `60` | …`admin` class. |
| `KESTREL_LOG_LEVEL` | `INFO` | Root log level. |
| `KESTREL_LOG_JSON` | `False` | `True` → JSON logs (production). |

(Rich-output, streaming, and polling caps have their own `KESTREL_*` settings; see
`config.py`.)

## Backends

Each layer has a simple default and a production option, chosen by env var:

- **Executor** — `docker` (default, sandboxed) or `subprocess` (no isolation; local
  dev on hosts without Docker only).
- **Sessions** — `memory` (single process) or `redis` (shared directory across
  workers). Rate-limit buckets follow the same choice automatically.
- **Audit log** — `null` (no-op) or `postgres`. Audit writes are fire-and-forget
  through a bounded queue; they never block or fail a request.
- **API keys** — `null` (only the dev shim works) or `postgres` (the real store).

The Compose stack turns on `redis` + `postgres` for all of these.

## Production checklist

- **Turn off the dev shim** — leave `KESTREL_DEV_API_KEY` empty; rely on the
  Postgres key store. Mint per-client keys with `kestrel-keys`.
- **Use the `postgres` backends** for audit and keys, and `redis` for sessions if
  running more than one worker.
- **Set `KESTREL_LOG_JSON=true`** so logs are machine-parseable.
- **Scrape `/metrics`** with Prometheus; watch `kestrel_audit_dropped_total`
  (audit queue overflow) and the rate-limit counters.
- **Scale the API horizontally if needed** — it is stateless. Sessions are pinned
  to the worker that owns their container, so put session-id-sticky routing in
  front of multiple workers (already required for the polling buffer).
- **Rotate keys** by minting new ones and revoking old (`kestrel-keys revoke <id>`
  or `DELETE /admin/keys/{id}`); revocation is immediate.
- **Back up PostgreSQL** — it holds the audit log and key store (the source of truth
  for keys). Redis and sandbox containers are disposable.

## Health and observability

- `GET /health` — liveness (open). The API image also has a Docker `HEALTHCHECK`.
- `GET /metrics` — Prometheus metrics (open).
- Every request/audit row carries an `X-Request-ID` for cross-log correlation.

## Beyond a single node

Kubernetes, clustering, and high availability are explicitly out of scope for the
current design — single-node Docker Compose is the supported deployment. The API's
statelessness leaves that door open for later.
