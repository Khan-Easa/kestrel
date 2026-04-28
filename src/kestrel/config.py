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
    executor_docker_image: str = Field(default="kestrel-runtime:0.2.0", description="Image tag the docker backend launches per request. Ignored when executor_backend != 'docker'.")

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()