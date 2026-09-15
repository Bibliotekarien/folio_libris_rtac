# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# uv: compile bytecode for faster startup, copy (not link) into the image
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install dependencies first (cached unless pyproject.toml / uv.lock change)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Application code
COPY application.py metrics.py ./

# Per-library settings (libraries/<sigel>/settings.json) contain FOLIO
# credentials and are NOT baked into the image — mount them at /app/libraries
# at runtime (see docker-compose.yml) or override with LIBRARIES_PATH.

ENV PATH="/app/.venv/bin:$PATH"

# Drop privileges: run as a non-root user. (Mounted libraries/<sigel>/settings.json
# must be readable by this user — e.g. world-readable, the default for new files.)
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 5000
CMD ["uvicorn", "application:application", "--host", "0.0.0.0", "--port", "5000"]
