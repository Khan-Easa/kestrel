# syntax=docker/dockerfile:1.7
# Same pinned base as the runtime image (docker/executor/Dockerfile) for consistency.
FROM python:3.11-slim@sha256:6d85378d88a19cd4d76079817532d62232be95757cb45945a99fec8e8084b9c2

LABEL org.opencontainers.image.source="https://github.com/Khan-Easa/kestrel"
LABEL org.opencontainers.image.description="Kestrel API service — sandboxed Python execution control plane"
LABEL org.opencontainers.image.licenses="MIT"

# --- Docker CLI (client only; talks to the HOST daemon via the mounted socket) ---
# Static binary, no daemon, no apt bloat. The API is the trusted control plane
# (Brief §2); the sandbox containers it launches stay socket-less (Brief §3).
ARG DOCKER_CLI_VERSION=27.5.1
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker /tmp/docker.tgz \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# --- uv (copied from the official image) ---
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer first — cached unless pyproject.toml / uv.lock change.
# README.md is required because pyproject's `readme` field points at it (hatchling).
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# Alembic config (migrations themselves live under src/, already copied above).
COPY alembic.ini ./
COPY docker/api/entrypoint.sh /usr/local/bin/kestrel-entrypoint
RUN chmod +x /usr/local/bin/kestrel-entrypoint

# Put the project venv on PATH so alembic / uvicorn resolve directly.
ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000

# Liveness probe — uses stdlib so it needs no extra packages (curl was purged).
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

ENTRYPOINT ["/usr/local/bin/kestrel-entrypoint"]