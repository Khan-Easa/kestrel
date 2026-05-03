# DECISIONS.md

A record of meaningful design decisions made while building Kestrel. Grouped by phase. Each entry covers what was chosen, what alternatives existed, and why this option won.

This file is maintained alongside the code. If a decision is later superseded, the original entry stays (with a "Superseded YYYY-MM-DD by …" note) — decisions are a history, not a snapshot.

---

## Phase 1 Decisions (Foundation)

### Web framework: FastAPI

Chose FastAPI over Flask, Django REST Framework, and Starlette-direct. FastAPI was picked because (a) it is async-native (Starlette + uvicorn under the hood), which the executor pipeline needs to interleave subprocess I/O with HTTP request handling without spawning threads; (b) it integrates pydantic for request/response validation directly in the route signature, eliminating a layer of hand-written serialization code; (c) its `Depends(...)` system gives us first-class dependency injection, which we lean on heavily for auth, settings, and the executor abstraction; (d) it generates OpenAPI docs for free, which is useful for an HTTP service that other systems will integrate with. Flask was rejected because it is sync-first and async support is bolt-on; DRF was rejected because Django is too heavy for a single-purpose service.

### ASGI server: uvicorn (with `--factory`)

Chose `uvicorn[standard]` and the `--factory` flag (`uvicorn kestrel.app:create_app --factory`). Uvicorn is the canonical production ASGI server pairing for FastAPI. The `--factory` flag means uvicorn calls `create_app()` itself rather than importing a pre-built `app` object — this keeps the FastAPI app construction inside a function (the factory pattern) so module import doesn't trigger startup work. Hypercorn and Daphne were not seriously considered; uvicorn is the standard and supports all the features we need.

### Configuration: pydantic-settings + `lru_cache` singleton

Chose `pydantic-settings` (a pydantic-v2 sibling library) with a `Settings(BaseSettings)` class and a module-level `get_settings()` wrapped in `@lru_cache(maxsize=1)`. All keys are env-var-driven with the prefix `KESTREL_` (e.g. `KESTREL_DEV_API_KEY`, `KESTREL_EXECUTOR_BACKEND`). Alternatives considered were ad-hoc `os.environ.get(...)` calls, `python-decouple`, or a YAML config file. Pydantic-settings won on three points: defaults declared next to the type (no separate "config schema" file), validation on parse (so a malformed env var fails fast at startup, not deep in a handler), and easy override in tests via `app.dependency_overrides[get_settings]`. Cached because settings shouldn't be re-read mid-process.

### Logging: structlog with stdlib bridge + uvicorn redirect

Chose structlog over the stdlib `logging` module alone. Two renderers based on `KESTREL_LOG_JSON`: `JSONRenderer` for production (one JSON object per line, ingestible by log aggregators), `ConsoleRenderer` with colors for local development. The pipeline includes a stdlib bridge (`structlog.stdlib.ProcessorFormatter`) so that `logging.getLogger(...)` calls from third-party libraries (uvicorn, FastAPI internals) flow through the same renderer — there is exactly one handler on the root logger and exactly one place that formats output. Uvicorn's three loggers (`uvicorn`, `uvicorn.error`, `uvicorn.access`) have their handlers cleared and `propagate=True` so their lines come through our pipeline too. Plain stdlib was rejected because structured key/value logging is a hard requirement for a service that needs to correlate request_id across log lines.

### Logging payload policy: never log code, only metadata

The `/execute` endpoint logs `code_length=len(req.code)` but never `req.code` itself. Same rule applies to any field that might carry secrets the caller pasted. The reason is operational: log files end up in aggregators, search indexes, and backups; logging payloads is how secrets escape into systems where they shouldn't be. Codified as a project-wide rule in CLAUDE.md.

### Authentication: bearer token via `HTTPBearer(auto_error=False)` + `secrets.compare_digest`

Chose a single bearer-token scheme. The expected token comes from `KESTREL_DEV_API_KEY`. The auth check is `secrets.compare_digest`, not `==`, to defeat timing attacks (constant-time comparison). The `HTTPBearer` security scheme is constructed with `auto_error=False` so it returns `None` on missing header instead of raising — that lets us own the policy decision and return the correct status (401, not the framework's default 403). When `dev_api_key == ""` (the default), auth is bypassed entirely; this is "Phase 1 dev-mode" behaviour and Phase 7 will replace it with a real key store. Alternatives — JWT, mTLS, OAuth2 — were rejected for Phase 1 as overkill for a private/dev service. The empty-key-bypass pattern means local development needs no token while production simply sets the env var.

### Endpoint shape: `POST /execute` (auth-gated) + `GET /health` (public)

`POST /execute` carries a JSON body with the user's code, returns the executor's structured response. `GET /health` returns `{"status": "ok"}` with no auth — public on purpose, because liveness probes (load balancers, Kubernetes, monitoring) typically have no credentials. Auth-gating `/health` would either force monitoring tools to handle keys (operational pain) or cause every probe to 401 (false outage signals). The split — public diagnostic endpoints, gated work endpoints — is the rule going forward.

### Phase 1 subprocess invocation: `python -I -c <code>` (argv passing)

The Phase 1 `SubprocessExecutor` passes user code as a `-c` command-line argument: `asyncio.create_subprocess_exec(sys.executable, "-I", "-c", code, ...)`. This was the simplest end-to-end path and was deliberately a Phase 1 stub — the goal was to get the HTTP-to-runner pipeline working, not to settle the long-term code-delivery design. The known argv-passing tradeoffs (argv size cap on Linux ~128 KB, code visible to anyone with `ps` access on the host) were acceptable for a stub since Phase 2 was always going to replace this with the container backend that uses a different delivery mechanism (bind-mounted tempfile — see the Phase 2 entry). The `-I` ("isolated mode") flag was kept regardless: it disables `PYTHONPATH`/`PYTHONHOME`/user-site so user code can't influence import resolution via parent env vars.

### Output capture strategy: per-stream byte cap with truncation flag

Each stream (stdout, stderr) is read in 8 KiB chunks; once the running total exceeds `KESTREL_EXECUTE_OUTPUT_CAP_BYTES` (default 64 KiB), the buffer is sliced down to the cap and a `*_truncated=True` flag is set in the response. We do NOT raise on overflow; we return whatever fit plus the flag. Alternatives were (a) raise / 413 the request, (b) buffer everything and let memory grow. Both were rejected because the caller (and the user code) can produce unbounded output, and a code-execution service that randomly errors on chatty programs is hostile. The flag lets the caller decide whether truncation matters for their use case.

### Timeout strategy: soft contract — kill, don't raise

`asyncio.wait_for(...)` with `KESTREL_EXECUTE_TIMEOUT_SECONDS` (default 5.0). On expiry: kill the subprocess, set `timed_out=True`, set `exit_code=-1`, return an empty stdout/stderr, and respond `200 OK` with that body. We do NOT raise, do NOT 504, do NOT propagate `asyncio.TimeoutError` to the caller. The reasoning: a hung user program is not a server error — it is normal, expected output for a code-execution service, so timeout is data, not an exception. The caller decides what `timed_out=True` means for them.

### Request tracking: `X-Request-ID` middleware + structlog contextvars + `perf_counter` timing

A single ASGI middleware in `app.py` runs around every request. It (a) reads or generates an `X-Request-ID` header, (b) clears + binds structlog contextvars (request_id, method, path) so every log line in the request inherits them, (c) times the request with `time.perf_counter()` (monotonic — `time.time()` can move backwards on clock adjustments), (d) logs `request_started` and `request_finished` (or `request_failed` with traceback if the handler raises and re-raises), (e) echoes `X-Request-ID` back on the response. Standard distributed-tracing pattern; useful immediately and load-bearing once we have multiple services.

### Request size cap: `code` limited to 100,000 characters

`ExecuteRequest.code` carries `max_length=100_000` (`min_length=1` rejects empty submissions; pydantic returns 422 on either bound violation). 100 K characters is a deliberate upper bound chosen as a sane round number — large enough to comfortably accept any "reasonable" submission (a self-contained script with embedded data, a small multi-function module), small enough to prevent multi-megabyte payload abuse. No specific memory-budget calculation drove the exact value; the goal was just "have *some* cap so the endpoint can't be used as a memory-pressure vector by clients sending arbitrarily large bodies." The cap is enforced by pydantic at parse time, before the handler runs, before the executor allocates anything.

### Validation: pydantic models for request and response shapes

`ExecuteRequest` has one required field, `code: str`, with `min_length=1` and `max_length=100_000`. `ExecuteResponse` has typed fields with sensible defaults so downstream code never has to handle a missing key. Pydantic gives us 422-on-malformed-input for free — no hand-rolled validation in handlers. The `response_model=ExecuteResponse` annotation on the `/execute` route both validates outgoing data at runtime (catching schema drift) and strips fields the model doesn't declare (a built-in safety net against accidental data leaks).

### Executor input flags: subprocess invoked as `python -I -c <code>`

The Phase 1 `SubprocessExecutor` invokes Python via `sys.executable` with the `-I` ("isolated mode") flag. `-I` disables `PYTHONPATH`, `PYTHONHOME`, and the user-site directory — meaning user code cannot influence import resolution by setting env vars in the parent or by writing to `~/.local/lib/...`. This narrows the blast radius even at the subprocess stage, before Phase 2's container isolation lands.

### Test strategy: real subprocesses, no mocks; pytest-asyncio in auto mode

Phase 1 tests use FastAPI's `TestClient` with the real `SubprocessExecutor`, no mocking of `asyncio.create_subprocess_exec`. A subprocess invocation costs ~40 ms — fast enough that the integration test suite stays under 10 s, and slow enough is not a problem in exchange for the certainty that the actual subprocess pipeline works. `asyncio_mode = "auto"` in `pyproject.toml` removes the need for `@pytest.mark.asyncio` on every async test. `pytest-asyncio` was the only serious choice; `anyio`'s pytest plugin was considered but pytest-asyncio is more standard.

### Build backend: `hatchling`

`pyproject.toml` uses `hatchling` (`build-backend = "hatchling.build"`) as the PEP 517 build backend. Hatchling was chosen as the scaffolding default — it is what the project initializer produced and there was no specific reason to override it. Hatchling is a reasonable fit on its merits (modern PEP 517 / PEP 621 native, no `setup.py` required, light footprint) but the active reasoning was simply "take the default and move on." Alternatives — `setuptools` (heavier legacy default), `poetry-core` (tied to poetry's CLI workflow we aren't using), `flit-core` (more minimal but less common), `pdm-backend` (tied to pdm) — were not actively evaluated. If a future need arises (custom build hooks, version sourcing from a `__version__.py`, etc.), the backend can be swapped — it touches one stanza in `pyproject.toml`.

### Package manager: `uv`

Chose `uv` (the Rust-implemented Python package/project manager from Astral) for dependency resolution, locking, virtualenv management, and script running. Two drivers: speed — uv's resolver is dramatically faster than `pip` + `pip-tools` or `poetry`, which compounds over hundreds of `sync` / `add` operations across a long project — and single-tool ergonomics: uv replaces the `pip` + `venv` + (optional) `pip-tools` triad with one binary, so `uv sync`, `uv add`, `uv run pytest`, and `uv run uvicorn ...` all flow through the same tool. Alternatives considered: pip + venv directly (slow resolver, no lock file by default), pip-tools (still slow, three-tool stack), poetry (heavier, opinionated, slower locks), pdm (capable but smaller ecosystem). The project rule going forward: never invoke `python` / `pytest` / `uvicorn` directly — always `uv run ...` — so the venv that ships with the lockfile is the only one in use.

### Python version pin: `>=3.11,<3.12`

`pyproject.toml` declares `requires-python = ">=3.11,<3.12"` — the project targets the 3.11 series exactly. 3.11 was the latest version installed and validated at the time the project was scaffolded; the upper bound was added defensively so a future Python install doesn't silently change the runtime out from under us. Loosening the ceiling (e.g. to allow 3.12) is treated as a deliberate decision — re-run the test suite on the newer version, then bump the pin — rather than a passive one. Alternatives considered: leave the ceiling open (`>=3.11`), or pin even tighter to a single 3.11.x release. Open-ceiling was rejected for the silent-upgrade reason; tighter-than-minor was rejected as needlessly fragile (3.11.x patch releases are safe by Python's policy).

### Package layout: `src/` layout (not flat)

Source code lives under `src/kestrel/` and tests live under `tests/`. Hatchling packages `src/kestrel`. The src layout ensures imports work the same way during development and after install — `from kestrel import ...` always goes through the installed package, not the working tree. Flat layouts (where `kestrel/` is at the repo root) silently make the working tree importable, which can mask packaging bugs until install-time.

---

## Phase 2 Decisions (Docker isolation)

### Executor abstraction: `Protocol` (PEP 544) with `runtime_checkable`, structural typing

Defined `Executor` as a `typing.Protocol` with one method, `async def run(code, settings) -> ExecuteResponse`. `SubprocessExecutor` and `DockerExecutor` both satisfy this contract structurally — neither inherits from `Executor`. The route handler depends on `Executor`, never on a concrete class. Alternatives were `abc.ABC`/`abstractmethod` (nominal subtyping — implementations must inherit) or no abstraction at all (call the concrete class). Protocol won because (a) it lets us write fakes in tests with no inheritance ceremony, (b) implementations don't have to know they conform to the contract, which keeps coupling low, (c) `runtime_checkable` lets us assert conformance with `isinstance(x, Executor)` if we ever need to.

### Backend selection: `KESTREL_EXECUTOR_BACKEND` Literal env var + `lru_cache`d `get_executor`

`Settings.executor_backend: Literal["subprocess", "docker"]` selects the implementation. `get_executor()` is `@lru_cache(maxsize=1)` so the executor is instantiated once per process and reused. The route handler asks for it via `Depends(get_executor)`. In tests, `app.dependency_overrides[get_executor] = lambda: SubprocessExecutor()` (or `DockerExecutor()`) swaps the backend without touching env vars or restarting the process. Default is `"docker"` since Phase 2 — `"subprocess"` is preserved as a fallback for hosts without Docker (and as the test path for the parametrized integration suite).

### Container-per-request, no warm pool

Each `/execute` call spawns a fresh container, runs the user's script, and lets `--rm` reap it. There is no warm-container pool, no reuse, no per-tenant allocation. Two reasons drove this. **Security simplicity:** by construction, no state can leak between requests — no concern about "did the last user leave something running?", no per-tenant cleanup tracking, no risk of one tenant's env leaking into another's. The isolation story is straightforward to defend. **Phase-appropriate:** Phase 2 is about getting isolation correct, not optimizing throughput. A warm pool would add real complexity (pool sizing, eviction, container reuse review, health checks, leaked-fd/process/`/tmp`-state tracking) that is not justified at this phase. The accepted cost is ~300–800 ms cold-start per request; pool optimization can come later if benchmarks show it is needed. Locked design decision (CLAUDE.md) — do not relitigate without a strong reason.

### Code delivery: bind-mounted tempfile at `/sandbox/main.py:ro`

The user's code is written to a host tempfile (in a per-request `tempfile.TemporaryDirectory(prefix="kestrel-")`), bind-mounted into the container as read-only at `/sandbox/main.py`, and executed via `python /sandbox/main.py`. We do NOT pipe the code via stdin or pass it as `python -c "<code>"`. Three benefits drove the choice, all of them desirable: (1) **Real tracebacks** — Python reports errors as `/sandbox/main.py:NN` instead of `<stdin>:NN`, so the caller sees a real file path and line number that can be mapped back to the submitted source. (2) **No argv leakage** — code is not visible to `ps` on the host or under `docker top` / `docker inspect`'s container metadata, unlike `python -c "<code>"`. (3) **Symmetry with normal script execution** — one canonical path ("Python runs a file") instead of a special "Python reads stdin" or "Python parses argv" mode, which makes the executor easier to reason about. Read-only mount (`:ro`) prevents the user code from rewriting itself mid-execution. Locked design decision (CLAUDE.md) — do not relitigate without a strong reason.

### Docker integration: CLI via `asyncio.subprocess`, not `docker-py`

The `DockerExecutor` shells out to the `docker` CLI using `asyncio.create_subprocess_exec("docker", "run", ...)`. We do NOT import `docker-py` (the official Python SDK). Three reasons combined to drive this. **Fewer dependencies:** keeping `pyproject.toml` lean is itself a goal — one fewer pinned library is one fewer thing to update, audit, and break on. **CLI flag familiarity:** the `docker run` flags in code read the same as a shell command (`--network none --read-only --user 65534:65534 ...`), so reasoning about isolation policy doesn't require translating between shell vocabulary and SDK argument names. The same flags you would test interactively are literally the ones in code. **Avoiding SDK pin pain:** `docker-py` versions and Docker Engine versions have historically had fragile compatibility around new flags and API changes; pinning to a CLI binary on PATH is a more stable contract because the CLI's flag surface is documented in `man docker-run` and changes slowly. Tradeoffs accepted: we depend on the `docker` binary being on PATH at runtime, and we parse CLI stdout for output capture (handled by the same `_read_stream` helper Phase 1 used for the subprocess executor). Locked design decision (CLAUDE.md) — do not relitigate without a strong reason.

### Container naming: `kestrel-exec-<uuid-hex>` prefix

Every container is launched with `--name kestrel-exec-<uuid4-hex>`. The deterministic prefix is what makes the orphan sweep work — `docker ps -aq --filter name=kestrel-exec-` finds exactly Kestrel's containers and nothing else. The UUID suffix prevents name collisions when many requests arrive concurrently. Without the prefix, the sweep would either miss orphans (too narrow a filter) or risk killing unrelated containers (too broad).

### Orphan invariant: `docker kill` on timeout + sweep on startup

Killing the `docker run` CLI process does NOT stop the container — the CLI is just a client; the real container parent is `containerd-shim`. So on timeout, `DockerExecutor` issues an explicit `docker kill <name>`. Symmetrically, when the app starts, `sweep_orphan_containers()` lists and force-removes any `kestrel-exec-*` containers left behind by a previous crashed run. Both halves are required: per-request kill prevents leakage during normal timeouts; startup sweep handles the case where the server itself crashed mid-request. The sweep is best-effort — if the Docker daemon is unreachable, it logs `orphan_sweep_skipped` and continues, so the app still starts on subprocess-only hosts.

### Container hardening flags (the bundle)

Every container runs with: `--rm` (auto-remove), `--network none` (no network namespace at all — not just firewalled, *absent*), `--read-only` (rootfs immutable), `--tmpfs /tmp:size=64m` (only writable area, capped), `--user 65534:65534` (UID/GID `nobody`, no privileged identity inside), `--memory 256m --memory-swap 256m` (memory cap with `--memory-swap == --memory` to disable swap so OOM-kill is the actual ceiling, not a slow death by paging), `--cpus 1.0` (CPU quota), `--pids-limit 64` (fork-bomb defence). These are passed as separate argv tokens (not concatenated) so the CLI parses them unambiguously. The set is applied in the same order on every call — there is one canonical command, not flag composition logic.

### `/tmp` tmpfs at 64 MiB

`--tmpfs /tmp:size=64m` is the *only* writable filesystem available to user code. It is RAM-backed (tmpfs), so the size cap is what prevents a malicious script from filling host RAM by writing to disk. 64 MiB is a deliberate small number — enough for normal scratch use (intermediate files, pickled blobs), small enough that a runaway can't move the needle. The cap is enforced by the tmpfs filesystem driver itself (returns ENOSPC on overflow), which works on every Linux including WSL2.

### Runtime image: `python:3.11-slim` pinned by digest, non-root user baked in

`Dockerfile` is `FROM python:3.11-slim@sha256:6d85378d88a19cd4d76079817532d62232be95757cb45945a99fec8e8084b9c2`. A `sandbox` user/group is created with UID/GID 65534 (matching `nobody`), no home directory, `/usr/sbin/nologin` shell. `WORKDIR /sandbox` and `USER 65534:65534` are set in the image — defence in depth, since the runtime `--user` flag also enforces this. The base is `python:3.11-slim` (not `python:3.11`) because the slim variant is ~80 MB smaller and has fewer packages = smaller attack surface. The base image is **pinned by digest** for two reasons that the same one-line change buys at once: (1) **supply-chain integrity** — a compromised registry or malicious actor cannot substitute a backdoored image under the `python:3.11-slim` tag; the SHA256 is content-addressed so only the exact bytes we originally chose will match; (2) **build reproducibility** — the same Dockerfile produces the same image months later regardless of how many times upstream re-pushes the tag, which is useful for debugging ("did the base change?"), compliance audits, and ensuring CI and dev environments build identical artifacts. Trade-off accepted: digest must be manually bumped when we want a newer base (security patches included), so we do not silently inherit upstream changes — that is the whole point.

### Default backend = `"docker"`, subprocess kept as fallback

After Phase 2 shipped, `Settings.executor_backend` default was flipped from `"subprocess"` to `"docker"`. The subprocess backend is preserved (not deleted) for two reasons: (a) hosts without Docker (CI runners, restricted environments) can still run the service by setting `KESTREL_EXECUTOR_BACKEND=subprocess`, and (b) the integration test suite parametrizes 20 black-box tests over both backends in `tests/conftest.py`, ensuring the contract is identical and any future contract drift is caught.

### Test fixture symmetry: parametrize over backends, skip docker if daemon down

`tests/conftest.py` defines `client(request)` parametrized over `["subprocess", "docker"]`, using `app.dependency_overrides[get_executor]` to install the chosen executor. The fixture calls `_docker_reachable()` (a `lru_cache`'d check that runs `docker info` once per session) and `pytest.skip(...)`s the docker variants if the daemon is down — so the suite stays green on hosts without Docker. A separate `docker_client` fixture (always docker, no subprocess variant) is used by the isolation tests, which only make sense against a real container. The override-based approach was chosen over env-var-driven backend selection because it is per-test and doesn't pollute the `lru_cache` of `get_settings`/`get_executor`.

---

## Going forward

This file is autonomously maintained per the protocol locked in `feedback_decisions_md_protocol.md`:
- I read this file at every session start.
- I flag decisions in chat as they happen ("this is a decision worth documenting").
- Before any session-close moment, I draft entries for the session's decisions, show them to you for confirmation, and save.
- I never invent rationale — when uncertain, I ask.
