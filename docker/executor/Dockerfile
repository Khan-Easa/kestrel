# syntax=docker/dockerfile:1.7
FROM python:3.11-slim@sha256:6d85378d88a19cd4d76079817532d62232be95757cb45945a99fec8e8084b9c2

LABEL org.opencontainers.image.source="https://github.com/Khan-Easa/kestrel"
LABEL org.opencontainers.image.description="Kestrel sandboxed Python execution runtime"
LABEL org.opencontainers.image.licenses="MIT"

RUN groupadd --system --gid 65534 sandbox 2>/dev/null || true \
&& useradd  --system --uid 65534 --gid 65534 --no-create-home --shell /usr/sbin/nologin sandbox 2>/dev/null || true

WORKDIR /sandbox
USER 65534:65534

CMD ["python3", "-c", "print('kestrel runtime')"]