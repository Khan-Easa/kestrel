#!/usr/bin/env python3
"""Persistent Python REPL kernel for Kestrel sessions.

Reads JSON-line messages from stdin, executes code in a persistent
namespace, writes JSON-line responses to stdout. Loops until EOF.

Designed to run inside the kestrel-runtime container as the long-lived
process behind a session. Pure stdlib; no kestrel imports.
"""
from __future__ import annotations

import ast
import base64
import io
import json
import mimetypes
import os
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

_OUTPUTS_DIR = "/workspace/outputs"


def _ensure_outputs_dir() -> None:
    """Create /workspace/outputs/ at kernel boot. Idempotent.

    Tmpfs mount typically creates the directory itself; this helper
    handles the dev/test case where no tmpfs is mounted, and is harmless
    when the directory already exists.
    """
    try:
        os.makedirs(_OUTPUTS_DIR, exist_ok=True)
    except Exception:
        print(
            f"kernel: failed to create {_OUTPUTS_DIR}:\n{traceback.format_exc()}",
            file=sys.stderr,
        )


def _emit(response: dict) -> None:
    """Write one JSON-line response to the real stdout and flush.

    Uses sys.__stdout__ (the preserved reference to the original interpreter
    stdout) rather than sys.stdout, because during _execute the latter is
    redirected to a _StreamingWriter — writing chunk lines through that would
    recurse into the writer's own write() method.
    """
    sys.__stdout__.write(json.dumps(response) + "\n")
    sys.__stdout__.flush()


class _StreamingWriter(io.TextIOBase):
    """File-like object that emits one chunk JSON line per write() and buffers locally.

    Substep 2 streaming protocol: replaces io.StringIO() in _execute. Each
    write() call emits {"id": msg_id, "type": chunk_type, "data": s} via
    _emit, AND appends s to a local StringIO so the final "result" message
    can carry the coalesced stdout/stderr the way the pre-streaming protocol
    did. Streaming consumers read chunks as they arrive; non-streaming
    consumers ignore chunks and read result.stdout/stderr at the end.

    chunk_type is "stdout_chunk" or "stderr_chunk"; constructed twice per
    _execute (once per channel).
    """

    def __init__(self, msg_id: object, chunk_type: str) -> None:
        super().__init__()
        self._msg_id = msg_id
        self._chunk_type = chunk_type
        self._buf = io.StringIO()

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        if not s:
            return 0
        self._buf.write(s)
        _emit({
            "id": self._msg_id,
            "type": self._chunk_type,
            "data": s,
        })
        return len(s)

    def getvalue(self) -> str:
        return self._buf.getvalue()

def _execute(code: str, namespace: dict, msg_id: object) -> tuple[str, str, int, object]:
    """Run user code; return (stdout, stderr, exit_code, captured_value).

    Uses Jupyter-style AST splitting: if the last statement is a bare
    expression, eval it separately and return its value for type dispatch.
    Otherwise captured_value is None. Catches Exception only —
    SystemExit/KeyboardInterrupt propagate and end the kernel.

    Substep 2: stdout/stderr capture uses _StreamingWriter (not io.StringIO),
    which emits one chunk line per write() while also buffering locally —
    msg_id is threaded through so chunk lines carry the right correlation id.
    """
    stdout_buf = _StreamingWriter(msg_id, "stdout_chunk")
    stderr_buf = _StreamingWriter(msg_id, "stderr_chunk")
    exit_code = 0
    captured_value: object = None

    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        stderr_buf.write(traceback.format_exc())
        return stdout_buf.getvalue(), stderr_buf.getvalue(), 1, None

    exec_tree = tree
    eval_expr = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        eval_expr = ast.Expression(body=tree.body[-1].value)
        ast.copy_location(eval_expr, tree.body[-1])
        exec_tree = ast.Module(body=tree.body[:-1], type_ignores=[])

    try:
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            if exec_tree.body:
                exec(compile(exec_tree, "<session>", "exec"), namespace)
            if eval_expr is not None:
                captured_value = eval(
                    compile(eval_expr, "<session>", "eval"), namespace
                )
    except Exception:
        stderr_buf.write(traceback.format_exc())
        exit_code = 1
        captured_value = None  # don't dispatch on partial state

    return stdout_buf.getvalue(), stderr_buf.getvalue(), exit_code, captured_value

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

def _capture_dataframe(value: object) -> dict | None:
    """If value is a pandas DataFrame, return its serialized dict; else None.

    On any pandas failure, prints a traceback to kernel stderr and returns
    None — the kernel survives so the session stays usable.
    """
    if not isinstance(value, pd.DataFrame):
        return None
    try:
        return {
            "type": "dataframe",
            "mime_type": "application/json",
            "data": value.to_dict(orient="split"),
            "shape": list(value.shape),
        }
    except Exception:
        print(
            f"kernel: dataframe capture failed:\n{traceback.format_exc()}",
            file=sys.stderr,
        )
        return None
    
def _capture_files() -> list[dict]:
    """Slurp top-level files in /workspace/outputs, then clear the directory.

    Returns a list of {type, mime_type, filename, data} dicts ready for
    JSON serialization. On any IO failure, prints a traceback to kernel
    stderr and returns whatever was captured before the failure.
    """
    outputs: list[dict] = []
    if not os.path.isdir(_OUTPUTS_DIR):
        return outputs
    try:
        for entry in sorted(os.listdir(_OUTPUTS_DIR)):
            path = os.path.join(_OUTPUTS_DIR, entry)
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                contents = f.read()
            mime_type, _ = mimetypes.guess_type(entry)
            outputs.append({
                "type": "file",
                "mime_type": mime_type or "application/octet-stream",
                "filename": entry,
                "data": base64.b64encode(contents).decode("ascii"),
            })
            os.unlink(path)
    except Exception:
        print(
            f"kernel: file capture failed:\n{traceback.format_exc()}",
            file=sys.stderr,
        )
    return outputs

def main() -> int:
    _ensure_outputs_dir()
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

        stdout, stderr, exit_code, captured_value = _execute(code, namespace, msg_id)
        outputs = _capture_plots()
        df_output = _capture_dataframe(captured_value)
        if df_output is not None:
            outputs.append(df_output)
        outputs.extend(_capture_files())

        _emit({
            "id": msg_id,
            "type": "result",
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "outputs": outputs,
        })

    return 0


if __name__ == "__main__":
    sys.exit(main())