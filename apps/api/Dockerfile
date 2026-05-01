# ── Eido API — Production Dockerfile ──────────────────────────────────────────
# Multi-stage build: builder installs deps, runner is minimal.
# Base images are SHA-pinned for Kyverno policy compliance.
# python:3.11-slim-bookworm@sha256 — verified 2026-04-30

FROM python:3.11-slim-bookworm@sha256:10fcb9e2a9a4b898e27dcdb36c29b7f8eef01cc4e3c7e2697a94a8c5eded58e8 AS builder

WORKDIR /build

# Install build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps into a prefix we can copy
COPY pyproject.toml .
RUN pip install --no-cache-dir --prefix=/install hatchling && \
    pip install --no-cache-dir --prefix=/install \
    fastapi uvicorn[standard] pydantic pydantic-settings \
    sqlalchemy asyncpg alembic redis httpx boto3 \
    python-multipart python-jose[cryptography] \
    prometheus-client

# Copy source
COPY src/ ./src/

# ── Runner stage ───────────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm@sha256:10fcb9e2a9a4b898e27dcdb36c29b7f8eef01cc4e3c7e2697a94a8c5eded58e8 AS runner

# Non-root user — security hardening
RUN addgroup --system --gid 1001 eido && \
    adduser --system --uid 1001 --gid 1001 --no-create-home eido

WORKDIR /app

# Runtime deps only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local
COPY --from=builder /build/src ./src

# Alembic migrations
COPY alembic.ini .
COPY alembic/ ./alembic/

USER eido

ENV PYTHONPATH=/app/src
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run migrations then start server
CMD ["sh", "-c", "alembic upgrade head && uvicorn eido_api.main:app --host 0.0.0.0 --port 8000 --workers 2"]
