from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix= "KESTREL_",env_file = ".env",env_file_encoding = "utf-8",extra="ignore")
    dev_api_key: str = Field(default="", description = "Bearer token clients must send. Empty disables auth (Phase 1 only).")
    execute_timeout_seconds: float = Field(default=5.0, gt=0, description= "Hard wall-clock timeout for each /execute call, in seconds.")
    execute_output_cap_bytes: int = Field(default=64* 1024, gt= 0, description="Maximum bytes captured per output stream before truncation.")
    log_level: str= Field(default="INFO", description= "Logging verbosity. One of DEBUG, INFO, WARNING, ERROR, CRITICAL.")
    log_json: bool= Field(default=False, description= "If True, emit logs as JSON (production); else pretty console output (dev).")
    executor_backend: Literal["subprocess", "docker"] = Field(default="docker", description="Which executor implementation to use. 'subprocess' = Phase 1 local subprocess (fallback for hosts without Docker); 'docker' = Phase 2 sandboxed container (default since Phase 2).")
    executor_docker_image: str = Field(default="kestrel-runtime:0.5.0", description="Phase 6 substep 2: runtime image (was :0.4.0 in Phase 5). Bumped because the kernel now speaks the multi-line streaming protocol (stdout_chunk / stderr_chunk lines terminated by a result line). :0.4.0 retained for rollback.")
    session_idle_timeout_seconds: float = Field(default=900.0, gt=0, description="Phase 4: max seconds a session may sit without an execute before the sweeper evicts it (15 min default).")
    session_sweep_interval_seconds: float = Field(default=60.0, gt=0, description="Phase 4: how often the background sweeper wakes up to check for idle sessions.")
    session_pool_size: int = Field(default=0, ge=0, description="Phase 4 substep 6: number of pre-started session containers kept warm in the pool. 0 = pool disabled, every POST /sessions cold-starts a new container. Opt-in via KESTREL_SESSION_POOL_SIZE.")
    session_backend: Literal["memory", "redis"] = Field(default="memory", description="Phase 4 substep 7: session registry backend. 'memory' = single-process in-memory map (default, identical to substep 6 behaviour); 'redis' = shared session directory across workers. Opt-in via KESTREL_SESSION_BACKEND.")
    redis_url: str = Field(default="redis://localhost:6379/0", description="Phase 4 substep 7: Redis connection URL. Used only when session_backend == 'redis'.")
    rich_output_plot_max_bytes: int = Field(default=2 * 1024 * 1024, gt=0, description="Phase 5: per-plot byte cap. Plots whose base64-encoded PNG exceeds this are dropped into dropped_outputs with reason='per_output_cap'.")
    rich_output_dataframe_max_bytes: int = Field(default=1 * 1024 * 1024, gt=0, description="Phase 5: per-DataFrame byte cap measured against the JSON-encoded data + shape payload. Dropped with reason='per_output_cap'.")
    rich_output_file_max_bytes: int = Field(default=5 * 1024 * 1024, gt=0, description="Phase 5: per-file byte cap for files written to /workspace/outputs/. Dropped with reason='per_output_cap'.")
    rich_output_total_max_bytes: int = Field(default=10 * 1024 * 1024, gt=0, description="Phase 5: per-execute total byte cap across all rich outputs combined. Once exceeded, remaining outputs are dropped with reason='total_cap'.")
    rich_output_file_max_count: int = Field(default=10, gt=0, description="Phase 5: max number of files captured per execute. Excess files are dropped with reason='file_count_cap'.")
    stream_heartbeat_seconds: float = Field(default=5.0, ge=0.0, description="Phase 6: cadence (seconds) of WebSocket-streaming heartbeat messages emitted during silent intervals. Reset on every other message sent. Set to 0.0 to disable heartbeats entirely.")
    stream_backpressure_timeout_seconds: float = Field(default=30.0, gt=0.0, description="Phase 6: per-send back-pressure safety cap. If a single WebSocket send (chunk or heartbeat) can't drain within this window, the streaming runtime kills the kernel and closes the connection with code 1011.")
    polling_buffer_ttl_seconds: float = Field(default=60.0, gt=0.0, description="Phase 6 substep 6: seconds a polling buffer survives after its execute completes. The session sweeper drops buffers older than this, giving late-polling clients a grace window. Decision 6.6-evict.")
    polling_max_wait_seconds: float = Field(default=30.0, gt=0.0, description="Phase 6 substep 6: server-side clamp on the long-poll GET ?wait= parameter. A GET that asks to wait longer is held only this long, bounding how long a worker stays pinned on one held request. Decision 6.6-mech.")
    audit_backend: Literal["null", "postgres"] = Field(default="null", description="Phase 7 substep 2: audit-log sink. 'null' = no-op (dev, tests). 'postgres' = bounded-queue + PostgresAuditSink. Opt-in via KESTREL_AUDIT_BACKEND.")
    database_url: str = Field(default="", description="Phase 7 substep 2: SQLAlchemy async URL (e.g.'postgresql+asyncpg://user:pw@localhost:5432/kestrel'). Required when audit_backend='postgres' or any later Phase 7 substep needs Postgres.")
    audit_queue_max_size: int = Field(default=1000, gt=0, description="Phase 7 substep 2: max in-flight audit events queued for the background drain task. Overflow drops events + bumps kestrel_audit_dropped_total. Decision 7-audit-sync.")
    audit_shutdown_drain_seconds: float = Field(default=5.0, gt=0.0, description="Phase 7 substep 2: lifespan-shutdown grace window for the audit drain task to flush remaining events before the process exits.")

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()