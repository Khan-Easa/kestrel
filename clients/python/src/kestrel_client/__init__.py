"""kestrel-client — Python SDK for the Kestrel code execution service."""

from __future__ import annotations

from kestrel_client._client import KestrelClient
from kestrel_client._exceptions import (
    AuthenticationError,
    KestrelAPIError,
    KestrelError,
    RateLimitedError,
    SessionBusyError,
    SessionGoneError,
    SessionNotFoundError,
)
from kestrel_client._models import (
    DataFrameOutput,
    DroppedOutput,
    ErrorMessage,
    ExecuteResult,
    FileOutput,
    Heartbeat,
    PlotOutput,
    PollingRead,
    ResultMessage,
    Session,
    SessionExecuteResult,
    StderrChunk,
    StdoutChunk,
)

__version__ = "0.8.0"

__all__ = [
    "KestrelClient",
    "KestrelError",
    "KestrelAPIError",
    "AuthenticationError",
    "SessionNotFoundError",
    "SessionBusyError",
    "SessionGoneError",
    "RateLimitedError",
    "ExecuteResult",
    "Session",
    "SessionExecuteResult",
    "PlotOutput",
    "DataFrameOutput",
    "FileOutput",
    "DroppedOutput",
    "StdoutChunk",
    "StderrChunk",
    "Heartbeat",
    "ResultMessage",
    "ErrorMessage",
    "PollingRead",
]
