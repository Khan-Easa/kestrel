#!/usr/bin/env python3
"""Persistent Python REPL kernel for Kestrel sessions.

Reads JSON-line messages from stdin, executes code in a persistent
namespace, writes JSON-line responses to stdout. Loops until EOF.

Designed to run inside the kestrel-runtime container as the long-lived
process behind a session. Pure stdlib; no kestrel imports.
"""
from __future__ import annotations

import base64
import io
import json
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def _emit(response: dict) -> None:
    """Write one JSON-line response to stdout and flush."""
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()


def _execute(code: str, namespace: dict) -> tuple[str, str, int]:
    """Run user code in the given namespace; return (stdout, stderr, exit_code).

    Catches `Exception` only — `SystemExit`, `KeyboardInterrupt`, and
    other `BaseException` subclasses propagate and end the kernel.
    """
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    exit_code = 0
    try:
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            exec(code, namespace)
    except Exception:
        stderr_buf.write(traceback.format_exc())
        exit_code = 1
    return stdout_buf.getvalue(), stderr_buf.getvalue(), exit_code

def _capture_plots() -> list[dict]:
    """Walk open matplotlib figures, encode each as base64 PNG, close them.

    Returns a list of {type, mime_type, data} dicts ready for JSON serialization.
    On any matplotlib failure, prints a traceback to kernel stderr and returns
    [] — the kernel survives so the session stays usable.
    """
    outputs: list[dict] = []
    try:
        for fig_num in plt.get_fignums():
            fig = plt.figure(fig_num)
            buf = io.BytesIO()
            fig.savefig(buf, format="png")
            outputs.append({
                "type": "plot",
                "mime_type": "image/png",
                "data": base64.b64encode(buf.getvalue()).decode("ascii"),
            })
            plt.close(fig)
    except Exception:
        print(
            f"kernel: plot capture failed:\n{traceback.format_exc()}",
            file=sys.stderr,
        )
        return []
    return outputs

def main() -> int:
    namespace: dict = {"__name__": "__main__"}

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            _emit({
                "id": None,
                "stdout": "",
                "stderr": f"kernel: invalid json line: {exc}\n",
                "exit_code": -1,
            })
            continue

        msg_id = msg.get("id")
        code = msg.get("code", "")

        stdout, stderr, exit_code = _execute(code, namespace)
        outputs = _capture_plots()

        _emit({
            "id": msg_id,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "outputs": outputs,
        })

    return 0


if __name__ == "__main__":
    sys.exit(main())