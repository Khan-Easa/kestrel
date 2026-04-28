from __future__ import annotations

import asyncio
import tempfile
import time
import uuid
from pathlib import Path

import structlog

from kestrel.api.schemas import ExecuteResponse
from kestrel.config import Settings
from kestrel.execution.manager import _read_stream


class DockerExecutor:
    """Phase 2 executor: runs user code inside a one-shot Docker container.

    Each call spawns a fresh ``kestrel-runtime`` container with strict isolation
    flags (no network, read-only rootfs, capped memory/CPU/pids, dropped privs).
    On timeout the container is killed by name — necessary because signalling
    the ``docker run`` CLI client does not stop the container; the real parent
    is ``containerd-shim``.
    """

    def __init__(self, image_tag: str = "kestrel-runtime:0.2.0") -> None:
        self._image_tag = image_tag

    async def run(self, code: str, settings: Settings) -> ExecuteResponse:
        start = time.perf_counter()
        container_name = f"kestrel-exec-{uuid.uuid4().hex}"

        with tempfile.TemporaryDirectory(prefix="kestrel-") as host_dir:
            host_path = Path(host_dir) / "main.py"
            host_path.write_text(code, encoding="utf-8")

            cmd = [
                "docker", "run",
                "--rm",
                "--name", container_name,
                "--network", "none",
                "--read-only",
                "--tmpfs", "/tmp:size=64m",
                "--user", "65534:65534",
                "--memory", "256m",
                "--memory-swap", "256m",
                "--cpus", "1.0",
                "--pids-limit", "64",
                "--volume", f"{host_path}:/sandbox/main.py:ro",
                "--workdir", "/sandbox",
                self._image_tag,
                "python", "/sandbox/main.py",
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            read_both = asyncio.gather(
                _read_stream(proc.stdout, settings.execute_output_cap_bytes),
                _read_stream(proc.stderr, settings.execute_output_cap_bytes),
            )

            timed_out = False
            try:
                (stdout_bytes, stdout_truncated), (stderr_bytes, stderr_truncated) = (
                    await asyncio.wait_for(read_both, timeout=settings.execute_timeout_seconds)
                )
                exit_code = await proc.wait()
            except asyncio.TimeoutError:
                timed_out = True
                # Killing the docker-run CLI does NOT stop the container — the real
                # parent is containerd-shim. Issue an explicit `docker kill` by name;
                # the CLI exits on its own once the container is gone.
                await _docker_kill(container_name)
                await proc.wait()
                stdout_bytes, stdout_truncated = b"", False
                stderr_bytes, stderr_truncated = b"", False
                exit_code = -1

        duration_ms = int((time.perf_counter() - start) * 1000)

        return ExecuteResponse(
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            exit_code=exit_code,
            duration_ms=duration_ms,
            timed_out=timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )


async def _docker_kill(name: str) -> None:
    """Best-effort `docker kill` by container name. Output is discarded."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "kill", name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()

async def sweep_orphan_containers() -> int:
    """Force-remove any leftover ``kestrel-exec-*`` containers from prior runs.

    Returns the number of containers removed. Best-effort: returns 0 silently
    if the Docker daemon is unreachable, so app startup still succeeds in
    environments where Docker is optional (e.g. running with the subprocess
    backend on a host without a daemon).
    """
    logger = structlog.get_logger()

    list_proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-aq", "--filter", "name=kestrel-exec-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await list_proc.communicate()
    if list_proc.returncode != 0:
        logger.warning("orphan_sweep_skipped", reason="docker_unreachable")
        return 0

    ids = stdout.decode().split()
    if not ids:
        logger.info("orphan_sweep_clean")
        return 0

    rm_proc = await asyncio.create_subprocess_exec(
        "docker", "rm", "-f", *ids,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await rm_proc.wait()
    logger.warning("orphan_sweep_removed", count=len(ids))
    return len(ids)