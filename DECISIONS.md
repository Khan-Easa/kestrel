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

## Phase 3 Decisions (Resource limits)

### Hardening flags: `--security-opt no-new-privileges` + `--cap-drop ALL`

Phase 3 Step 1 added two flags to the `docker run` command on top of the existing `--user 65534:65534`. **`no-new-privileges`** sets the kernel `NO_NEW_PRIVS` bit, which prevents a process from gaining additional privileges across `execve()` — setuid binaries don't escalate, file capabilities don't apply. **`--cap-drop ALL`** empties the container's effective Linux capabilities bitmap, removing every per-area root power (CAP_NET_RAW for raw sockets, CAP_SYS_ADMIN for mounts, CAP_SYS_PTRACE for `ptrace`, CAP_SYS_MODULE for kernel modules, etc. — the kernel divides root's authority into ~37 named privileges and we drop them all). Together these two flags mean that even if a setuid binary somehow exists inside the image, executing it can't restore capabilities — the kernel refuses both the setuid bit transition and the capability grant. Defence in depth: each flag covers what the other might miss. Verified at runtime by reading `/proc/self/status` inside the container — `test_isolation_no_new_privileges` asserts `NoNewPrivs:\t1`, `test_isolation_capabilities_dropped` parses the `CapEff:` hex bitmap and asserts it equals 0. Catches a regression where either flag is silently dropped from the run command.

### Pids cap: `--pids-limit 64`

Phase 3 Step 2 chose 64 as the per-container process/thread limit. The cap is the fork-bomb defence — without it, a `while True: os.fork()` loop in user code can exhaust host PID slots until the kernel itself starts refusing process creation system-wide. 64 is the chosen number because it is **large enough** to comfortably handle normal multi-threaded Python (a few worker threads, GC threads, asyncio loop threads, the parent process itself) and **small enough** that a fork bomb hits `OSError`/`EAGAIN` from the kernel within milliseconds, well before the 5-second wall-clock timeout fires. Verified by `test_isolation_pids_limit_enforced`: parent forks in a loop (capped at 200 attempts to bound the test), children `time.sleep(60)` + `os._exit(0)` to hold their pid slot, parent counts how many forks succeeded. Lenient-but-bounded assertion — `0 < forked < 200` proves the cap fired (loop didn't exhaust); `forked <= 64` catches a regression that loosens the cap (some kernel-version pid-accounting jitter is allowed within that bound).

### Seccomp: keep Docker's default profile, no custom profile

Phase 3 Step 3 considered authoring a custom seccomp profile narrowly tailored to "what user Python actually needs to run." Rejected. Docker's default profile is the right cost/benefit point. **Why:** the default already blocks ~44 dangerous syscalls (including `mount`, `unshare`, `setns`, `kexec_load`, `bpf`, `ptrace` against unrelated processes, etc.) and is maintained by Docker's security team — they update it as new kernel features land. Authoring a custom profile for *arbitrary* user Python is structurally hard: too broad (effectively matches the default — pointless work) or too narrow (breaks legitimate Python features the moment a user imports something the profile didn't anticipate — `socket`, `multiprocessing`, `os.fork`, `subprocess`, `mmap`, file I/O on uncommon filesystems, etc., all touch syscalls the default permits but a hand-written narrow profile might miss). For an arbitrary-Python execution service, the maintenance burden of keeping a custom profile in sync with both kernel changes *and* Python's evolving syscall surface outweighs the marginal security gain. Verified at runtime by `test_isolation_seccomp_filter_active`, which asserts `Seccomp:\t2` (mode 2 = BPF filter attached). The test does not try to prove any individual syscall is blocked — Docker's blocklist varies across versions, and "filter is on" is the durable signal. Catches a regression where someone adds `--security-opt seccomp=unconfined` for "debugging" and forgets to remove it.

### Tmpfs size-cap test: raw `os.open`/`os.write`, not buffered I/O

Phase 3 Step 4 added `test_isolation_tmpfs_size_capped` to verify the `--tmpfs /tmp:size=64m` cap actually fires. The test deliberately uses **raw file descriptor I/O** (`os.open` + `os.write`) instead of Python's high-level `open(...).write(...)`. Reason: the high-level API returns a buffered file object — writes don't immediately hit the kernel; Python collects them in memory and flushes when the buffer fills. For a tmpfs cap test, that delay would distort the test in two ways: the ENOSPC error wouldn't fire on the iteration that actually overran the cap (it would fire later, on the buffer-flush boundary), and the byte count we observe wouldn't reflect actual kernel-level writes. Raw fd I/O bypasses the buffer entirely; every `os.write(fd, chunk)` is a real syscall, so the cap fires at a predictable byte count. The 1-MiB chunk size is small enough that the cap's exact byte position is observable. Upper-bound assertion `50 < written_mib <= 64` catches both a regression that loosens the cap (`<= 64` would fail if someone bumps `size=64m` to `size=64g`) and one that tightens it implausibly (`50 <` rules out spurious early failures). The lower bound has slack because tmpfs accounts for filesystem metadata as well as user data — the actual usable write capacity is slightly less than the configured 64 MiB. **Crucially**, the tmpfs `size=` cap is enforced by the **tmpfs filesystem driver itself** (returns ENOSPC = errno 28 on overflow), *not* by the cgroup memory controller — which is why this test works on WSL2 even though `--memory` doesn't.

### `--memory` test with `pytest.skipif(_is_wsl2)`

Phase 3 Step 5 added `test_isolation_memory_limit_enforced` plus a private `_is_wsl2()` helper used in a `pytest.mark.skipif`. **The skip is necessary because the WSL2 kernel doesn't enforce the Docker memory cgroup** — Docker passes the flag, the cgroup is created, but the actual OOM-killer doesn't fire when processes inside the container exceed the configured limit. Without the skip, the test would falsely report "cap not enforced" on every WSL2 development host, even though production Linux *does* enforce it. The skip is the honest answer: don't claim a test result we can't verify. `_is_wsl2()` reads `/proc/version` and looks for the substring `"microsoft"` (WSL2 advertises a Microsoft-built kernel; real Linux does not). The skip reason is human-readable so pytest output is self-explanatory. On real Linux, the test allocates 1-MiB `bytearray` chunks in a loop up to 512 MiB (twice the 256 MiB cap, generous headroom — `bytearray(N)` zero-fills, which forces page faults so the cgroup actually accounts for the memory rather than counting only reserved virtual address space). Asserts `timed_out is False` (OOM-kill should fire faster than the 5-second wall-clock, sub-second on a tight allocation loop) and `exit_code != 0` (OOM-killed containers exit non-zero — typically 137 = 128 + SIGKILL=9, but we use `!= 0` rather than `== 137` for resilience to Docker's signal-reporting convention across versions). The error messages are diagnostic — if the cap silently fails and the loop completes normally, the assertion message includes the stdout (`'ALLOCATED 512 MiB'`) so the regression is immediately obvious.

### HTTP rate limiting deferred out of Phase 3

Phase 3 Step 5 considered including HTTP-layer rate limiting (per-client request-per-time-unit caps) before declaring Phase 3 done. Deferred. **Why:** Phase 3's stated theme is *per-execution* resource limits — what a single request can consume on the host (memory, CPU, pids, tmpfs, network, capabilities). Rate limiting is a different concern: how many requests *one caller* can issue per unit time, regardless of what each request does. It naturally lives in the auth/hardening layer alongside API keys, quotas, tenancy, and abuse-mitigation policy — concerns that arrive in Phase 6 (auth) or Phase 7 (production hardening) per `Kestrel_Project_Brief.pdf` §6. Building rate limiting in Phase 3, before there's a meaningful identity or tenancy concept, would mean implementing it against the IP address of the client — useful but coarse, and likely to be replaced once `KESTREL_DEV_API_KEY` evolves into a real key store. Phase 3 ships without rate limiting; the decision is captured here so a future "why isn't there rate limiting?" question has the right answer ready.

---

## Phase 4 Decisions (Sessions)

### Communication protocol: home-grown JSON-line over stdin/stdout

Phase 4 introduces a long-running Python process inside each session container; the API needs a wire format to send code in and receive stdout/stderr/exit_code back. Chose a home-grown JSON-line protocol over the container's stdin/stdout, framed as "one JSON message per line." The container runs a small kernel script on startup that loops: read a JSON line from stdin, exec the code in a persistent global namespace, write a JSON-line response to stdout. The API holds onto the container's stdin/stdout pipes (`docker run -i ...`) for the session's lifetime and writes/reads JSON lines on demand.

Two real alternatives were considered and rejected. The Jupyter messaging protocol (ZeroMQ across five sockets per kernel, HMAC-signed multipart messages, `ipykernel` as the runtime) would have been production-grade and would have given us Phase 6's rich-output story essentially for free, but at three costs that didn't fit Phase 4: (a) ZMQ wants TCP, and we'd have had to either weaken the Phase 2 `--network none` lock or design unix-socket bind-mounts into the container, both real work; (b) `ipykernel` brings IPython's auto-magic and cell-level introspection, behaviour we may not want; (c) ZMQ + HMAC + Jupyter-specific debugging is a learning-cliff for what Phase 4's stated theme (sessions) does not actually need. A hybrid (home-grown wire transport but Jupyter-shaped message field names) was also considered — rejected as adding discipline now for migration insurance against an option we may never exercise.

The chosen design's tradeoffs are accepted explicitly: (1) Kestrel sessions will not be Jupyter-compatible — a Jupyter UI cannot attach to a Kestrel session; (2) we own every edge case ourselves (kernel crash mid-message, stdout that arrives long after the request "completes," partial output capture), but those are bounded problems we can solve incrementally. The win is that `--network none` survives Phase 4 untouched — pipes are file descriptors, not sockets, so there is no network-namespace concern at all.

### Concurrency within a session: reject second concurrent execute with HTTP 409

A session has one Python process behind it, which is single-threaded by nature. When two `POST /sessions/{id}/execute` calls arrive for the same session while one is still running, the server rejects the second with HTTP 409 Conflict and `{"error": "session_busy"}`. The caller decides whether to retry. The first request runs through normally; the rejection is immediate (no queueing, no waiting).

Two alternatives were considered. **Serialize** (queue the second behind the first) was rejected because it builds an implicit unbounded per-session work queue, and HTTP semantics make "wait an arbitrary time for someone else's code to finish" awkward — wall-clock timeout starts when, request arrival or execution start? Either answer is wrong half the time. **Cancel-and-replace** (interrupt the running execution, start the new one) matches Jupyter's "interrupt and re-run cell" UX but is too aggressive as a default; cleanly interrupting arbitrary user code is genuinely hard (signal handling, partial-state cleanup), and surprising if the contention is accidental.

Reject-with-409 won on three points: (1) every request gets a deterministic answer in bounded time; (2) 409 is the standard HTTP code for "resource in a conflicting state," well-understood by clients and proxies; (3) a normal REPL-shaped client only issues one execute at a time, so contention is a bug or a race, and 409 surfaces it loudly instead of absorbing it. Cancel-and-replace can be added later as an explicit `?force=true` opt-in if a use case appears — easier to add than to remove.

### Auth scope: bearer token + session_id as capability (unguessable UUID4)

`/sessions/*` endpoints are gated by the same bearer-token check as `/execute` (consistency with Phase 1's auth policy). Once a session exists, the session_id itself is treated as a capability: a UUID4 carries 122 bits of entropy and is unguessable in practice, and knowledge of the session_id *is* the right to access it. We do not track ownership explicitly in Phase 4 — there is exactly one bearer token (`KESTREL_DEV_API_KEY`), so multi-user differentiation has nowhere to come from at the auth layer.

The defence-in-depth posture is intentional: an attacker needs *both* the bearer *and* a specific session_id to do anything to a session. A bearer leak alone gives no session access (the attacker still doesn't know which session_ids exist); a session_id leak alone gives no API access (without the bearer, every endpoint returns 401). Same family of pattern as JupyterHub tokens, Google Docs share-links, AWS pre-signed URLs — unguessable identifiers used as capabilities.

Explicit ownership tracking (Redis-stored `session_id → bearer_hash`; even with a valid bearer you can only access sessions you created) was considered and rejected for Phase 4: there is only one bearer in this phase, so ownership has nothing to differentiate. Phase 7 brings real per-key API keys, and ownership tracking will be added then on top of the existing capability semantics — no semantic break, no migration. The design composes cleanly forward.

One operational discipline this commits us to: never log the full session_id at INFO level. Logs end up in aggregators, search indexes, and backups; the same reason we never log `req.code` applies here. Log a prefix for traceability (`session_id_prefix=abc12345`); the prefix is enough for ops correlation, not enough for an attacker to use as a capability.

### Phase 2 lock formally amended: warm pool *is* allowed for session containers (not for per-request execution)

The Phase 2 design lock "fresh container per request, no warm pool" is preserved for the original `/execute` endpoint and for any future stateless one-shot endpoints. Phase 4 introduces a separate, *session-scoped* container model where one container outlives many requests within one session, and `Kestrel_Project_Brief.pdf` §6.4 mandates a container pool ("Container pooling for efficiency") for warming session containers ahead of time. The two design locks address different concerns: Phase 2's lock was about *cross-request* state leakage (no state carries between independent users' executions); Phase 4's pool only serves sessions, and a session is by definition a single user's stateful conversation, so the cross-request-leakage concern does not apply within a pool. The contradiction is therefore shallow — surface-level wording, not substance — but it is worth noting explicitly so a future reader can see both decisions in context.

---

## Going forward

This file is autonomously maintained per the protocol locked in `feedback_decisions_md_protocol.md`:
- I read this file at every session start.
- I flag decisions in chat as they happen ("this is a decision worth documenting").
- Before any session-close moment, I draft entries for the session's decisions, show them to you for confirmation, and save.
- I never invent rationale — when uncertain, I ask.
