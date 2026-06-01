"""Frozen-dataclass mirrors of Kestrel's response shapes (decision 8-sdk-models).

No pydantic dependency — JSON dicts are mapped to objects via ``from_dict``
classmethods and small ``type``-field dispatch functions for the unions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Union


def _parse_dt(value: str) -> datetime:
    # pydantic emits ISO-8601 with offset (e.g. "2026-06-01T08:53:10+00:00").
    # Normalise a trailing "Z" so Python < 3.11's fromisoformat accepts it.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class ExecuteResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExecuteResult":
        return cls(
            stdout=d.get("stdout", ""),
            stderr=d.get("stderr", ""),
            exit_code=d.get("exit_code", 0),
            duration_ms=d.get("duration_ms", 0),
            timed_out=d.get("timed_out", False),
            stdout_truncated=d.get("stdout_truncated", False),
            stderr_truncated=d.get("stderr_truncated", False),
        )


@dataclass(frozen=True)
class Session:
    session_id: str
    created_at: datetime
    last_used: datetime

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Session":
        return cls(
            session_id=d["session_id"],
            created_at=_parse_dt(d["created_at"]),
            last_used=_parse_dt(d["last_used"]),
        )


@dataclass(frozen=True)
class PlotOutput:
    data: str
    mime_type: str = "image/png"
    type: str = "plot"


@dataclass(frozen=True)
class DataFrameOutput:
    data: dict
    shape: tuple
    mime_type: str = "application/json"
    type: str = "dataframe"


@dataclass(frozen=True)
class FileOutput:
    filename: str
    data: str
    mime_type: str
    type: str = "file"


@dataclass(frozen=True)
class DroppedOutput:
    type: str
    reason: str
    size_bytes: int
    filename: str | None = None


RichOutput = Union[PlotOutput, DataFrameOutput, FileOutput]


def _parse_output(d: dict[str, Any]) -> RichOutput:
    kind = d.get("type")
    if kind == "plot":
        return PlotOutput(data=d["data"], mime_type=d.get("mime_type", "image/png"))
    if kind == "dataframe":
        return DataFrameOutput(
            data=d["data"],
            shape=tuple(d["shape"]),
            mime_type=d.get("mime_type", "application/json"),
        )
    if kind == "file":
        return FileOutput(filename=d["filename"], data=d["data"], mime_type=d["mime_type"])
    raise ValueError(f"unknown output type: {kind!r}")


@dataclass(frozen=True)
class SessionExecuteResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool
    outputs: list
    dropped_outputs: list

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionExecuteResult":
        return cls(
            stdout=d.get("stdout", ""),
            stderr=d.get("stderr", ""),
            exit_code=d.get("exit_code", 0),
            duration_ms=d.get("duration_ms", 0),
            timed_out=d.get("timed_out", False),
            stdout_truncated=d.get("stdout_truncated", False),
            stderr_truncated=d.get("stderr_truncated", False),
            outputs=[_parse_output(o) for o in d.get("outputs", [])],
            dropped_outputs=[
                DroppedOutput(
                    type=x["type"],
                    reason=x["reason"],
                    size_bytes=x["size_bytes"],
                    filename=x.get("filename"),
                )
                for x in d.get("dropped_outputs", [])
            ],
        )


@dataclass(frozen=True)
class StdoutChunk:
    data: str
    type: str = "stdout"


@dataclass(frozen=True)
class StderrChunk:
    data: str
    type: str = "stderr"


@dataclass(frozen=True)
class Heartbeat:
    elapsed_ms: int
    type: str = "heartbeat"


@dataclass(frozen=True)
class ResultMessage:
    result: SessionExecuteResult
    request_id: str = ""
    type: str = "result"


@dataclass(frozen=True)
class ErrorMessage:
    code: str
    detail: str
    request_id: str = ""
    type: str = "error"


StreamMessage = Union[StdoutChunk, StderrChunk, Heartbeat, ResultMessage, ErrorMessage]


def parse_stream_message(d: dict[str, Any]) -> StreamMessage:
    kind = d.get("type")
    if kind == "stdout":
        return StdoutChunk(data=d["data"])
    if kind == "stderr":
        return StderrChunk(data=d["data"])
    if kind == "heartbeat":
        return Heartbeat(elapsed_ms=d["elapsed_ms"])
    if kind == "result":
        return ResultMessage(
            result=SessionExecuteResult.from_dict(d), request_id=d.get("request_id", "")
        )
    if kind == "error":
        return ErrorMessage(code=d["code"], detail=d["detail"], request_id=d.get("request_id", ""))
    raise ValueError(f"unknown stream message type: {kind!r}")


@dataclass(frozen=True)
class PollingRead:
    messages: list
    next_cursor: int
    done: bool
    request_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PollingRead":
        return cls(
            messages=[parse_stream_message(m) for m in d.get("messages", [])],
            next_cursor=d["next_cursor"],
            done=d["done"],
            request_id=d.get("request_id", ""),
        )
