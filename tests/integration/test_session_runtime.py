from __future__ import annotations

import asyncio

import pytest

from kestrel.execution.session_runtime import (
    SessionRuntime,
    SessionTerminated,
    SessionTimeout,
)


async def test_round_trip(session_runtime_factory):
    """One execute round-trip: code in, structured response out."""
    runtime = await session_runtime_factory()

    response = await runtime.execute("print(1)")

    assert response.exit_code == 0
    assert response.stdout == "1\n"
    assert response.stderr == ""


async def test_state_persists_across_executes(session_runtime_factory):
    """§6.4 acceptance: variables defined in one call survive into the next."""
    runtime = await session_runtime_factory()

    setup = await runtime.execute("x = 7")
    assert setup.exit_code == 0
    assert setup.stdout == ""

    follow_up = await runtime.execute("print(x)")
    assert follow_up.exit_code == 0
    assert follow_up.stdout == "7\n"


async def test_caught_exception_does_not_kill_kernel(session_runtime_factory):
    """A user-raised Exception is reflected in the response and the kernel
    survives. Verifies the substep-2B 'catch Exception only' decision."""
    runtime = await session_runtime_factory()

    boom = await runtime.execute("raise ValueError('boom')")
    assert boom.exit_code == 1
    assert "ValueError" in boom.stderr
    assert "boom" in boom.stderr

    survived = await runtime.execute("print(2 + 2)")
    assert survived.exit_code == 0
    assert survived.stdout == "4\n"


async def test_per_message_timeout_kills_session(session_runtime_factory):
    """A wedged execute() raises SessionTimeout; the session is then dead
    and any further execute raises SessionTerminated. Verifies substep-3
    decision 4c."""
    runtime = await session_runtime_factory(timeout_seconds=1.0)

    with pytest.raises(SessionTimeout):
        await runtime.execute("import time; time.sleep(60)")

    with pytest.raises(SessionTerminated):
        await runtime.execute("print('after')")


async def test_system_exit_terminates_session(session_runtime_factory):
    """SystemExit propagates out of the kernel's _execute (which only
    catches Exception) and ends the kernel loop. The execute() call that
    triggered it raises SessionTerminated because the kernel exited
    before sending a reply. Verifies the other half of substep-2B."""
    runtime = await session_runtime_factory()

    with pytest.raises(SessionTerminated):
        await runtime.execute("import sys; sys.exit(0)")

    with pytest.raises(SessionTerminated):
        await runtime.execute("print('after')")


async def test_close_cleans_up_container(session_runtime_factory):
    """After close(), the named container is gone — the orphan-invariant
    story for sessions."""
    runtime = await session_runtime_factory()
    container_name = runtime._container_name
    assert container_name is not None

    await runtime.execute("print(1)")
    await runtime.close()

    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-aq", "--filter", f"name={container_name}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    assert stdout.decode().strip() == "", (
        f"container {container_name} still exists after close()"
    )