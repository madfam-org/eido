# ── Eido SPZ Compression — Dockerfile ─────────────────────────────────────────
# CPU-only stage: 3DGS point_cloud.ply → output.spz (Niantic SPZ v2).
# python:3.11-slim-bookworm — digest refreshed 2026-07-10
FROM python:3.11-slim-bookworm@sha256:f5cf0344c9886ff24d34797578d5d7dd6e8911ae0fe5962bb55d0f89603ec361

RUN addgroup --system --gid 1001 eido && \
    adduser --system --uid 1001 --gid 1001 --no-create-home eido

RUN pip install --no-cache-dir numpy

WORKDIR /app
COPY services/spz-compress/spz_writer.py services/spz-compress/entrypoint.py ./

USER eido

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV INPUT_PLY=/work/splat/point_cloud.ply
ENV OUTPUT_SPZ=/work/splat/output.spz

ENTRYPOINT ["python3", "entrypoint.py"]
