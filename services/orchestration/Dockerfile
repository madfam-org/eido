# ── Eido Orchestration Worker — Dockerfile ────────────────────────────────────
# Lightweight dispatcher — runs on CPU only.
# GPU containers are spawned ephemerally via the Vast.ai / AWS API.
# python:3.11-slim-bookworm@sha256 — refreshed 2026-07-10 (old manifest GC'd from Docker Hub)

FROM python:3.11-slim-bookworm@sha256:f5cf0344c9886ff24d34797578d5d7dd6e8911ae0fe5962bb55d0f89603ec361

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
