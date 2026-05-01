# ── Eido Orchestration Worker — Dockerfile ────────────────────────────────────
# Lightweight dispatcher — runs on CPU only.
# GPU containers are spawned ephemerally via the Vast.ai / AWS API.
# python:3.11-slim-bookworm@sha256 — verified 2026-04-30

FROM python:3.11-slim-bookworm@sha256:10fcb9e2a9a4b898e27dcdb36c29b7f8eef01cc4e3c7e2697a94a8c5eded58e8

RUN addgroup --system --gid 1001 eido && \
    adduser --system --uid 1001 --gid 1001 --no-create-home eido

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    redis \
    boto3 \
    httpx \
    vastai

WORKDIR /app
COPY services/orchestration/worker.py .

USER eido

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import redis; r = redis.from_url('${REDIS_URL}'); r.ping()" || exit 1

CMD ["python3", "worker.py"]
