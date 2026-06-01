# Security

Kestrel runs **untrusted Python code by design**. This document states explicitly
what the system protects against and what it does not — an honest threat model is
itself part of the security posture.

## Threat model — what Kestrel protects against

- **Resource exhaustion** — fork bombs, memory bombs, infinite loops (memory/CPU/PID caps + wall-clock timeout).
- **Container-escape attempts** — privilege escalation is blunted by dropped capabilities, `no-new-privileges`, a non-root user, and a seccomp filter.
- **Network-based attacks** — containers run with `--network none`; no inbound or outbound network.
- **Filesystem escape** — the root filesystem is read-only; the only writable paths are small tmpfs mounts.
- **Cross-session data leakage** — each session is an isolated container; stateless executes are one-shot containers, removed after use.
- **Denial of service via output size** — stdout/stderr and rich outputs are capped; excess is truncated or dropped, never buffered unbounded.
- **API abuse** — per-API-key rate limits throttle flooding; auth gates every work endpoint.

## Threat model — what Kestrel does NOT protect against

- **Nation-state attackers with 0-day Docker/kernel escapes** — out of scope; use a VM-isolation layer if that's your threat model.
- **Side-channel attacks** (timing, cache) — infeasible to fully mitigate.
- **Supply-chain attacks on Python packages** — the runtime image's packages are the user's responsibility.
- **Cryptocurrency mining within the time limit** — financially unattractive at this scale.
- **Accidentally shared/leaked API keys** — the holder's responsibility (keys are revocable; rotate promptly).

## Security controls

Every container Kestrel launches applies the full bundle (see
[`docs/architecture.md`](docs/architecture.md) and
[`docker_executor`](src/kestrel/execution/docker_executor.py)):

| Control | Mechanism |
|---|---|
| Non-root execution | `--user 65534:65534` (nobody) |
| Read-only root FS | `--read-only` + small `--tmpfs` for writable paths |
| Memory cap | `--memory` / `--memory-swap` (no swap beyond the cap) |
| CPU cap | `--cpus` |
| Process cap | `--pids-limit` |
| No network | `--network none` |
| Dropped privileges | `--cap-drop ALL`, `--security-opt no-new-privileges` |
| Syscall filter | seccomp (Docker default profile) |
| Timeout | wall-clock `SIGKILL` (the timeout returns data, not an error) |
| No Docker socket in sandboxes | sandboxes never receive `/var/run/docker.sock` |
| Code delivery | read-only bind-mounted tempfile, not `python -c` or stdin |

**On the orphan invariant:** killing the `docker run` client does *not* stop the
container, so the executor issues an explicit `docker kill` by name on timeout and
the app sweeps any `kestrel-exec-*` survivors on startup.

**On the control plane:** in the containerised deployment the API mounts the host
Docker socket to launch sandboxes (docker-out-of-docker). The API is the *trusted*
component; the sandboxes it launches are not, and never get the socket. See
[`docs/deployment.md`](docs/deployment.md).

## Adversarial testing

The isolation guarantees are verified by an adversarial test suite that runs real
malicious code against the live Docker backend — fork bombs, memory bombs, infinite
loops, network-access attempts, filesystem-escape attempts, and large-output
truncation. These live in
[`tests/integration/test_isolation.py`](tests/integration/test_isolation.py) and run
as part of the normal suite (Docker required).

> **WSL2 note:** the `--memory` cgroup limit is set correctly but not OOM-enforced by
> the WSL2 kernel, so the memory-bomb test is skipped there. Production Linux enforces
> it; the flag is passed from day one for that reason.

## Reporting a vulnerability

This is a learning project, not a hosted service. If you find a security issue,
open an issue on the [repository](https://github.com/Khan-Easa/kestrel) (omit any
working exploit details from public issues; describe the class of problem and
contact the maintainer to share specifics).
