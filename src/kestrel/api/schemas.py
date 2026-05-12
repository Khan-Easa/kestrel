from __future__ import annotations

from datetime import datetime

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