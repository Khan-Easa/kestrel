from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

class ExecuteRequest(BaseModel):
    code: str= Field(min_length = 1, max_length = 100_000, description= "Python source to execute.")

class ExecuteResponse(BaseModel):
    stdout: str = Field(default="", description= "Captured standard output (UTF-8, possibly truncated).")
    stderr: str = Field(default="", description= "Captured standard error (UTF-8, possibly truncated).")
    exit_code: int = Field(default= 0, description= "Subprocess exit code; 0 = success.")
    duration_ms: int = Field(default= 0, ge= 0, description= "Wall-clock execution time in milliseconds.")
    timed_out: bool = Field(default=False, description="True if killed for exceeding the timeout.")
    stdout_truncated: bool = Field(default=False, description="True if stdout exceeded the byte cap and was cut off.")
    stderr_truncated: bool = Field(default=False, description="True if stderr exceeded the byte cap and was cut off.")

class SessionResponse(BaseModel):
    session_id: str = Field(description="Unguessable UUID4 (hex, 32 chars). Knowledge of this value is the access right for the session.")
    created_at: datetime = Field(description="UTC timestamp of when the session was created.")
    last_used: datetime = Field(description="UTC timestamp of the most recent execute on the session. Bumped on every /sessions/{id}/execute.")


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse] = Field(default_factory=list, description="All currently-active sessions known to this process.")

class PlotOutput(BaseModel):
    type: Literal["plot"] = "plot"
    mime_type: Literal["image/png"] = "image/png"
    data: str = Field(description="Base64-encoded PNG bytes.")


class DataFrameOutput(BaseModel):
    type: Literal["dataframe"] = "dataframe"
    mime_type: Literal["application/json"] = "application/json"
    data: dict = Field(description="DataFrame serialised via to_dict(orient='split'): {'index': [...], 'columns': [...], 'data': [[...]]}.")
    shape: tuple[int, int] = Field(description="(n_rows, n_cols) of the captured DataFrame.")


class FileOutput(BaseModel):
    type: Literal["file"] = "file"
    mime_type: str = Field(description="MIME type guessed from the file extension, e.g. text/csv, image/png, application/pdf.")
    filename: str = Field(description="Filename relative to /workspace/outputs/, e.g. report.csv.")
    data: str = Field(description="Base64-encoded file bytes.")


RichOutput = Annotated[
    PlotOutput | DataFrameOutput | FileOutput,
    Field(discriminator="type"),
]


class DroppedOutput(BaseModel):
    type: Literal["plot", "dataframe", "file"] = Field(description="Which output type was dropped.")
    reason: Literal["per_output_cap", "total_cap", "file_count_cap"] = Field(description="Why the output was dropped.")
    size_bytes: int = Field(ge=0, description="Size in bytes of the dropped output's encoded form.")
    filename: str | None = Field(default=None, description="Filename, set only for file drops; None for plot and dataframe drops.")


class SessionExecuteResponse(ExecuteResponse):
    outputs: list[RichOutput] = Field(default_factory=list, description="Phase 5: rich outputs captured during this execute. Empty when the cell produced none.")
    dropped_outputs: list[DroppedOutput] = Field(default_factory=list, description="Phase 5: outputs that were captured but exceeded a size or count cap. Surfaces what was lost without polluting stdout/stderr.")


class StreamStdoutChunk(BaseModel):
    type: Literal["stdout"] = "stdout"
    data: str = Field(description="Phase 6: one chunk of stdout text. Per-write granularity from the kernel's _StreamingWriter — print('hello') typically yields two chunks ('hello' + '\\n').")


class StreamStderrChunk(BaseModel):
    type: Literal["stderr"] = "stderr"
    data: str = Field(description="Phase 6: one chunk of stderr text. Same per-write granularity as stdout.")


class StreamHeartbeat(BaseModel):
    type: Literal["heartbeat"] = "heartbeat"
    elapsed_ms: int = Field(ge=0, description="Phase 6: milliseconds since the execute started. Emitted every Settings.stream_heartbeat_seconds when no other message has been sent in that window.")


class StreamResult(SessionExecuteResponse):
    type: Literal["result"] = "result"
    request_id: str = Field(
        default="",
        description=(
            "Phase 7 substep 1 (7-reqid-stream): X-Request-ID of the originating "
            "HTTP request — handshake for WebSocket, POST for polling. Stamped by "
            "the API layer immediately before send/buffer; empty when the runtime "
            "constructs the message in-process."
        ),
    )


class StreamError(BaseModel):
    type: Literal["error"] = "error"
    code: str = Field(description="Phase 6: short stable error code, e.g. session_not_found, session_busy, session_terminated, internal.")
    detail: str = Field(description="Phase 6: human-readable error message; not stable enough for clients to switch on.")
    request_id: str = Field(
        default="",
        description=(
            "Phase 7 substep 1 (7-reqid-stream): X-Request-ID of the originating "
            "HTTP request. Same conventions as StreamResult.request_id."
        ),
    )


StreamMessage = Annotated[ StreamStdoutChunk | StreamStderrChunk | StreamHeartbeat | StreamResult | StreamError,
Field(discriminator="type"), ]


class PollingExecuteResponse(BaseModel):
    execution_id: str = Field(description="Phase 6 substep 6: opaque handle for the async polling execute just started. Pass it to GET /sessions/{id}/executions/{execution_id} to read output as it accumulates.")


class PollingReadResponse(BaseModel):
    messages: list[StreamMessage] = Field(default_factory=list, description="Phase 6 substep 6: stream messages with index >= the requested ?since cursor. Same discriminated-union shape the WebSocket route sends.")
    next_cursor: int = Field(ge=0, description="Phase 6 substep 6: cursor to pass as ?since on the next poll. Equals the requested since plus len(messages).")
    done: bool = Field(description="Phase 6 substep 6: True once the execute has finished AND every message up to it has been delivered in this or an earlier poll. When True, the client stops polling.")
    request_id: str = Field(
        default="",
        description=(
            "Phase 7 substep 1 (7-reqid-stream): X-Request-ID of THIS GET request "
            "(not the originating POST). Top-level so clients can correlate the "
            "read with server logs without inspecting per-message frames."
        ),
    )


# ── Phase 7 substep 6 slice 1: admin response shapes ──


class ApiKeyResponse(BaseModel):
    id: str = Field(description="UUID4 of the key. Stable identity; safe to log and pass to DELETE/admin/keys/{id} in slice 2.")
    label: str = Field(description="Human label set at creation time. Free-form, not unique.")
    created_at: datetime = Field(description="UTC timestamp of when the key was minted.")
    revoked_at: datetime | None = Field(default=None, description="UTC timestamp of when the key was revoked, or null if still active.")
    scopes: list[str] = Field(description="Granted scopes, e.g. ['execute'] or ['execute', 'admin'].")


class ApiKeyCreateRequest(BaseModel):
    label: str = Field(min_length=1, max_length=200, description="Human label for the new key. Free-form, not unique.")
    scopes: list[str] | None = Field(default=None, description="Scopes to grant. None → store default (['execute']). Pass ['execute', 'admin'] to mint an admin key.")


class ApiKeyCreateResponse(ApiKeyResponse):
    token: str = Field(description="Plaintext API token (kestrel_<43 chars>). Returned ONCE at creation; the store keeps only its sha256 hash, so it is never recoverable afterward — capture it now.")


class ApiKeyListResponse(BaseModel):
    keys: list[ApiKeyResponse] = Field(default_factory=list, description="All API keys known to the store (active + revoked), newest-first. Empty list when api_key_backend == 'null'.")


class AuditEventResponse(BaseModel):
    id: str = Field(description="UUID4 of the audit row.")
    ts: datetime = Field(description="UTC timestamp the row was inserted server-side (DB default).")
    request_id: str = Field(description="X-Request-ID of the originating request.")
    api_key_id: str | None = Field(default=None, description="Identity that made the request: API-key UUID, the literal 'dev' for the dev shim, or null when auth was disabled.")
    route: str = Field(description="Route path template, e.g. /sessions/{session_id}/execute.")
    method: str = Field(description="HTTP method; 'WS' for WebSocket activity per decision 7.3-ws-method-label.")
    status: int = Field(description="HTTP status code (or WebSocket-mapped equivalent).")
    session_id: str | None = Field(default=None, description="Session UUID when the request was scoped to one.")
    execution_id: str | None = Field(default=None, description="Execute ID when the row corresponds to one execute (session execute, polling, WS).")
    code_length: int | None = Field(default=None, description="Length of the user-supplied code in bytes. Never the code itself.")
    exit_code: int | None = Field(default=None, description="Exit code of the executed kernel cell, when applicable.")
    timed_out: bool | None = Field(default=None, description="True if the execute was killed for exceeding the timeout.")
    duration_ms: int | None = Field(default=None, description="Wall-clock duration in milliseconds.")
    error_kind: str | None = Field(default=None, description="Short stable error category when the request failed.")


class AuditListResponse(BaseModel):
    events: list[AuditEventResponse] = Field(default_factory=list, description="Phase 7 substep 6: audit rows in ts-desc order, page of up to ``limit``.")
    next_before_ts: datetime | None = Field(default=None, description="Phase 7 substep 6: cursor for the next page; pass as ?before_ts= on the next call. Null on the last page (when fewer results than limit).")