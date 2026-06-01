"""Exception hierarchy for the Kestrel client.

Only *HTTP-transport-level* failures raise. Execution outcomes (a non-zero
exit code, a timeout) are returned as data on the result object, mirroring the
server's "timeout is data, not an error" contract (decision 8-sdk-errors).
"""

from __future__ import annotations


class KestrelError(Exception):
    """Base class for every error raised by the client."""


class KestrelAPIError(KestrelError):
    """An unexpected HTTP error (4xx/5xx not mapped to a more specific type)."""

    def __init__(self, status_code: int, detail: str = "") -> None:
        self.status_code = status_code
        self.detail = detail
        message = f"Kestrel API error {status_code}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class AuthenticationError(KestrelError):
    """The API key was missing, malformed, or rejected (HTTP 401)."""


class SessionNotFoundError(KestrelError):
    """The session id does not exist or has expired (HTTP 404)."""


class SessionBusyError(KestrelError):
    """The session already has an execute in flight (HTTP 409)."""


class SessionGoneError(KestrelError):
    """The session's container has terminated (HTTP 410)."""


class RateLimitedError(KestrelError):
    """The per-key rate limit was exceeded (HTTP 429)."""

    def __init__(self, retry_after: int | None = None) -> None:
        self.retry_after = retry_after
        message = "rate limited"
        if retry_after is not None:
            message = f"{message}; retry after {retry_after}s"
        super().__init__(message)
