from __future__ import annotations

import asyncio

import pytest

from kestrel.execution.session_runtime import (
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

async def test_simple_plot_capture(session_runtime_factory):
    """A single plt.plot creates one figure; capture surfaces one PlotOutput."""
    runtime = await session_runtime_factory()

    response = await runtime.execute(
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1, 2, 3])"
    )

    assert response.exit_code == 0
    assert len(response.outputs) == 1
    assert response.outputs[0].type == "plot"
    assert response.outputs[0].mime_type == "image/png"
    assert response.outputs[0].data  # non-empty base64 string
    assert response.dropped_outputs == []


async def test_multi_figure_cell_captures_all_figures(session_runtime_factory):
    """Three plt.figure() calls in one execute produce three PlotOutputs."""
    runtime = await session_runtime_factory()

    response = await runtime.execute(
        "import matplotlib.pyplot as plt\n"
        "for i in range(3):\n"
        "    plt.figure()\n"
        "    plt.plot([i, i + 1])"
    )

    assert response.exit_code == 0
    assert len(response.outputs) == 3
    assert all(o.type == "plot" for o in response.outputs)
    assert len(response.outputs) == 3
    assert all(o.type == "plot" for o in response.outputs)
    assert response.dropped_outputs == []


async def test_no_figure_cell_has_empty_outputs(session_runtime_factory):
    """Code that produces no plot leaves outputs and dropped_outputs empty."""
    runtime = await session_runtime_factory()

    response = await runtime.execute("print('hello')")

    assert response.exit_code == 0
    assert response.stdout == "hello\n"
    assert response.outputs == []
    assert response.dropped_outputs == []


async def test_oversized_plot_drops_to_dropped_outputs(session_runtime_factory):
    """A plot exceeding plot_max_bytes is dropped, surfacing in dropped_outputs."""
    runtime = await session_runtime_factory(plot_max_bytes=100)

    response = await runtime.execute(
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1, 2, 3])"
    )

    assert response.exit_code == 0
    assert response.outputs == []
    assert len(response.dropped_outputs) == 1
    assert response.dropped_outputs[0].type == "plot"
    assert response.dropped_outputs[0].reason == "per_output_cap"
    assert response.dropped_outputs[0].size_bytes > 100

async def test_dataframe_last_expression_captured(session_runtime_factory):
    """A DataFrame as the last expression in a cell produces a DataFrameOutput."""
    runtime = await session_runtime_factory()

    response = await runtime.execute(
        "import pandas as pd\n"
        "pd.DataFrame({'a': [1, 2, 3], 'b': [4, 5, 6]})"
    )

    assert response.exit_code == 0
    assert len(response.outputs) == 1
    assert response.outputs[0].type == "dataframe"
    assert response.outputs[0].shape == (3, 2)
    assert response.outputs[0].data["columns"] == ["a", "b"]
    assert response.dropped_outputs == []


async def test_non_dataframe_last_expression_is_ignored(session_runtime_factory):
    """A non-DataFrame last expression (e.g. an int) produces no rich output."""
    runtime = await session_runtime_factory()

    response = await runtime.execute("1 + 1")

    assert response.exit_code == 0
    assert response.outputs == []
    assert response.dropped_outputs == []


async def test_empty_cell_has_no_outputs(session_runtime_factory):
    """An empty (whitespace-only) cell produces nothing — no error, no output."""
    runtime = await session_runtime_factory()

    response = await runtime.execute("   \n   ")

    assert response.exit_code == 0
    assert response.outputs == []
    assert response.dropped_outputs == []


async def test_multiple_statements_with_dataframe_last(session_runtime_factory):
    """A cell with setup statements followed by a DataFrame captures only the last."""
    runtime = await session_runtime_factory()

    response = await runtime.execute(
        "import pandas as pd\n"
        "x = 5\n"
        "y = 10\n"
        "pd.DataFrame({'sum': [x + y]})"
    )

    assert response.exit_code == 0
    assert len(response.outputs) == 1
    assert response.outputs[0].type == "dataframe"
    assert response.outputs[0].data["data"] == [[15]]


async def test_exception_before_dataframe_produces_no_output(session_runtime_factory):
    """If user code raises before reaching the last expression, no DataFrame surfaces."""
    runtime = await session_runtime_factory()

    response = await runtime.execute(
        "import pandas as pd\n"
        "raise ValueError('boom')\n"
        "pd.DataFrame({'a': [1]})"  # never reached
    )

    assert response.exit_code == 1
    assert "ValueError" in response.stderr
    assert response.outputs == []
    assert response.dropped_outputs == []


async def test_oversized_dataframe_drops_to_dropped_outputs(session_runtime_factory):
    """A DataFrame whose JSON payload exceeds dataframe_max_bytes is dropped."""
    runtime = await session_runtime_factory(dataframe_max_bytes=100)

    response = await runtime.execute(
        "import pandas as pd\n"
        "pd.DataFrame({'a': list(range(1000))})"  # JSON >> 100 bytes
    )

    assert response.exit_code == 0
    assert response.outputs == []
    assert len(response.dropped_outputs) == 1
    assert response.dropped_outputs[0].type == "dataframe"
    assert response.dropped_outputs[0].reason == "per_output_cap"
    assert response.dropped_outputs[0].size_bytes > 100


async def test_file_csv_capture(session_runtime_factory):
    """A user writing one CSV to /workspace/outputs/ surfaces as a FileOutput."""
    import base64

    runtime = await session_runtime_factory()

    response = await runtime.execute(
        "with open('/workspace/outputs/data.csv', 'w') as f:\n"
        "    f.write('a,b\\n1,2\\n3,4\\n')"
    )

    assert response.exit_code == 0
    assert len(response.outputs) == 1
    assert response.outputs[0].type == "file"
    assert response.outputs[0].filename == "data.csv"
    assert response.outputs[0].mime_type == "text/csv"
    decoded = base64.b64decode(response.outputs[0].data).decode()
    assert decoded == "a,b\n1,2\n3,4\n"


async def test_multiple_files_captured_in_sorted_order(session_runtime_factory):
    """Multiple files in /workspace/outputs/ each surface as separate FileOutputs, sorted."""
    runtime = await session_runtime_factory()

    response = await runtime.execute(
        "for name in ['b.txt', 'a.txt', 'c.txt']:\n"
        "    with open(f'/workspace/outputs/{name}', 'w') as f:\n"
        "        f.write(name)"
    )

    assert response.exit_code == 0
    assert len(response.outputs) == 3
    assert [o.filename for o in response.outputs] == ['a.txt', 'b.txt', 'c.txt']


async def test_binary_file_capture_with_mime(session_runtime_factory):
    """A binary file (PNG header) is captured with the correct MIME type."""
    runtime = await session_runtime_factory()

    response = await runtime.execute(
        "png_header = b'\\x89PNG\\r\\n\\x1a\\n' + b'\\x00' * 100\n"
        "with open('/workspace/outputs/img.png', 'wb') as f:\n"
        "    f.write(png_header)"
    )

    assert response.exit_code == 0
    assert len(response.outputs) == 1
    assert response.outputs[0].filename == "img.png"
    assert response.outputs[0].mime_type == "image/png"


async def test_oversized_file_drops_to_dropped_outputs(session_runtime_factory):
    """A file exceeding file_max_bytes is dropped with reason='per_output_cap'."""
    runtime = await session_runtime_factory(file_max_bytes=100)

    response = await runtime.execute(
        "with open('/workspace/outputs/big.bin', 'wb') as f:\n"
        "    f.write(b'x' * 1000)"
    )

    assert response.exit_code == 0
    assert response.outputs == []
    assert len(response.dropped_outputs) == 1
    assert response.dropped_outputs[0].type == "file"
    assert response.dropped_outputs[0].filename == "big.bin"
    assert response.dropped_outputs[0].reason == "per_output_cap"
    assert response.dropped_outputs[0].size_bytes > 100


async def test_file_count_cap_drops_excess_files(session_runtime_factory):
    """When more than file_max_count files are produced, excess drop with file_count_cap."""
    runtime = await session_runtime_factory(file_max_count=3)

    response = await runtime.execute(
        "for i in range(5):\n"
        "    with open(f'/workspace/outputs/file{i}.txt', 'w') as f:\n"
        "        f.write(str(i))"
    )

    assert response.exit_code == 0
    assert len(response.outputs) == 3
    assert len(response.dropped_outputs) == 2
    for drop in response.dropped_outputs:
        assert drop.type == "file"
        assert drop.reason == "file_count_cap"


async def test_file_written_outside_outputs_dir_not_captured(session_runtime_factory):
    """Files written to /tmp (not /workspace/outputs/) are not captured."""
    runtime = await session_runtime_factory()

    response = await runtime.execute(
        "with open('/tmp/scratch.txt', 'w') as f:\n"
        "    f.write('hidden')"
    )

    assert response.exit_code == 0
    assert response.outputs == []
    assert response.dropped_outputs == []