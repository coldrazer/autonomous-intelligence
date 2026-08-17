FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

LABEL org.opencontainers.image.source="https://github.com/coldrazer/autonomous-intelligence"
LABEL org.opencontainers.image.description="Transaction-safe MCP execution for AI agents"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/autonomous

WORKDIR /opt/autonomous-intelligence

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install --no-cache-dir . \
    && autonomous-intelligence --help >/dev/null \
    && autonomous-intelligence-mcp --help >/dev/null \
    && mkdir -p /home/autonomous \
    && chown 10001:10001 /home/autonomous

USER 10001:10001
WORKDIR /home/autonomous

ENTRYPOINT ["autonomous-intelligence-mcp"]
