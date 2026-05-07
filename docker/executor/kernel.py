#!/usr/bin/env python3
"""Persistent Python REPL kernel for Kestrel sessions.

Reads JSON-line messages from stdin, executes code in a persistent
namespace, writes JSON-line responses to stdout. Loops until EOF.

Designed to run inside the kestrel-runtime container as the long-lived
process behind a session. Pure stdlib; no kestrel imports.
"""
from __future__ import annotations

import io
import json
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout


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

        _emit({
            "id": msg_id,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
        })

    return 0


if __name__ == "__main__":
    sys.exit(main())