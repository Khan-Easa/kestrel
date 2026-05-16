from __future__ import annotations

import asyncio
import json
import time
import uuid

import structlog

from kestrel.api.schemas import (
    DroppedOutput,
    ExecuteResponse,
    PlotOutput,
    SessionExecuteResponse,
)

_STDERR_CAP_BYTES = 64 * 1024

class SessionRuntimeError(Exception):
    """Base for all session-runtime failures."""

class SessionTerminated(SessionRuntimeError):
    """The kernel/container is gone; further calls are dead."""

class SessionTimeout(SessionRuntimeError):
    """Per-message wait_for expired; the container has been killed."""

class SessionProtocolError(SessionRuntimeError):
    """Reply was malformed JSON, missing fields, or had a mismatched id."""

class SessionRuntime:
    """Host-side client for one kestrel-runtime kernel container.

    Owns a ``docker run -i`` subprocess and the JSON-line protocol over its
    stdin/stdout. One container, many ``execute`` calls, until ``close()``
    or a timeout-induced termination.
    """
    
    def __init__(
        self,
        image_tag: str,
        timeout_seconds: float,
        plot_max_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self._image_tag = image_tag
        self._timeout_seconds = timeout_seconds
        self._plot_max_bytes = plot_max_bytes
        self._proc: asyncio.subprocess.Process | None = None
        self._container_name: str | None = None
        self._stderr_buf = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None
        self._terminated = False

    @classmethod
    async def start(
        cls,
        image_tag: str,
        timeout_seconds: float,
        plot_max_bytes: int = 2 * 1024 * 1024,
    ) -> SessionRuntime:
        """Spawn the container, attach pipes, return ready-to-use runtime."""
        runtime = cls(
            image_tag=image_tag,
            timeout_seconds=timeout_seconds,
            plot_max_bytes=plot_max_bytes,
        )
        runtime._container_name = f"kestrel-session-{uuid.uuid4().hex}"

        cmd = [
            "docker", "run",
            "--rm",
            "-i",
            "--name", runtime._container_name,
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:size=64m",
            "--user", "65534:65534",
            "--memory", "256m",
            "--memory-swap", "256m",
            "--cpus", "1.0",
            "--pids-limit", "64",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            image_tag,
        ]

        runtime._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
        )
        runtime._stderr_task = asyncio.create_task(runtime._drain_stderr())

        logger = structlog.get_logger()
        logger.info(
            "session_runtime_started",
            session_id_prefix=runtime._container_name[len("kestrel-session-"):][:8],
        )
        return runtime
    
    async def execute(self, code: str) -> SessionExecuteResponse:
        """Send one execute message; await one reply.

        Raises ``SessionTerminated`` if the session is already dead,
        ``SessionTimeout`` if the reply doesn't arrive within
        ``timeout_seconds`` (and kills the container as a side effect),
        ``SessionProtocolError`` if the reply is malformed.
        """
        if self._terminated:
            raise SessionTerminated("session is no longer running")

        assert self._proc is not None and self._proc.stdin is not None
        assert self._proc.stdout is not None

        msg_id = uuid.uuid4().hex
        request = json.dumps({"id": msg_id, "code": code}) + "\n"
        start = time.perf_counter()

        try:
            self._proc.stdin.write(request.encode("utf-8"))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._terminated = True
            raise SessionTerminated(f"kernel stdin closed: {exc}") from exc

        try:
            line = await asyncio.wait_for(
                self._proc.stdout.readline(),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._terminated = True
            await _docker_kill(self._container_name)
            await self._proc.wait()
            raise SessionTimeout(
                f"no reply within {self._timeout_seconds}s; container killed"
            )

        if not line:
            self._terminated = True
            await self._proc.wait()
            raise SessionTerminated("kernel exited before sending a reply")

        try:
            data = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionProtocolError(f"malformed reply: {exc}") from exc

        if data.get("id") != msg_id:
            raise SessionProtocolError(
                f"reply id mismatch: sent {msg_id}, got {data.get('id')!r}"
            )

        duration_ms = int((time.perf_counter() - start) * 1000)

        raw_outputs = data.get("outputs", [])
        outputs: list = []
        dropped: list[DroppedOutput] = []
        for raw in raw_outputs:
            if raw.get("type") == "plot":
                size_bytes = len(raw.get("data", ""))
                if size_bytes > self._plot_max_bytes:
                    dropped.append(DroppedOutput(
                        type="plot",
                        reason="per_output_cap",
                        size_bytes=size_bytes,
                    ))
                else:
                    outputs.append(PlotOutput(data=raw["data"]))

        return SessionExecuteResponse(
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            exit_code=int(data.get("exit_code", 0)),
            duration_ms=duration_ms,
            timed_out=False,
            stdout_truncated=False,
            stderr_truncated=False,
            outputs=outputs,
            dropped_outputs=dropped,
        )
    
    async def close(self) -> None:
        """Tear down the container and reap the subprocess.

        Idempotent: safe to call after the session has already terminated.
        """
        if self._terminated:
            await self._cancel_stderr_task()
            return

        self._terminated = True
        assert self._proc is not None

        if self._proc.stdin is not None and not self._proc.stdin.is_closing():
            self._proc.stdin.close()

        try:
            await asyncio.wait_for(self._proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            await _docker_kill(self._container_name)
            await self._proc.wait()

        await self._cancel_stderr_task()

    @property
    def stderr_buffer(self) -> str:
        """Decoded view of anything the kernel/Python runtime wrote to stderr."""
        return bytes(self._stderr_buf).decode("utf-8", errors="replace")

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                chunk = await self._proc.stderr.read(8192)
                if not chunk:
                    return
                if len(self._stderr_buf) < _STDERR_CAP_BYTES:
                    remaining = _STDERR_CAP_BYTES - len(self._stderr_buf)
                    self._stderr_buf.extend(chunk[:remaining])
        except asyncio.CancelledError:
            return
        
    async def _cancel_stderr_task(self) -> None:
        if self._stderr_task is None or self._stderr_task.done():
            return
        self._stderr_task.cancel()
        try:
            await self._stderr_task
        except asyncio.CancelledError:
            pass


async def _docker_kill(name: str | None) -> None:
    """Best-effort ``docker kill`` by container name. Output is discarded."""
    if not name:
        return
    proc = await asyncio.create_subprocess_exec(
        "docker", "kill", name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()