"""
Eido API — Reality Capture, 3DGS Orchestration & Ecosystem Handoffs
"To see is to know." — eido.cam
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app

from eido_api.config import get_settings
from eido_api.db.redis import close_redis, init_redis
from eido_api.db.session import close_db, init_db
from eido_api.routers import (
    captures,
    collections,
    export,
    handoffs,
    health,
    jobs,
    search,
    social,
)

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Eido API starting — the lens opens.")
    await init_db()
    await init_redis()
    yield
    await close_redis()
    await close_db()
    logger.info("Eido API shutdown — form returns to void.")


app = FastAPI(
    title="Eido API",
    description="Reality Capture · 3D Gaussian Splatting · Ecosystem Handoffs",
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url=None,
)

# Explicit CORS allowlist — wildcards banned per 2026-04-23 security audit
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)

# Prometheus metrics
app.mount("/metrics", make_asgi_app())

# ── Routers ────────────────────────────────────────────────────────────────────
app.include_router(health.router, tags=["health"])
app.include_router(captures.router,     prefix="/api/v1/captures",     tags=["captures"])
app.include_router(jobs.router,         prefix="/api/v1/jobs",         tags=["jobs"])
app.include_router(handoffs.router,     prefix="/api/v1/handoffs",     tags=["handoffs"])
app.include_router(social.router,       prefix="/api/v1/social",       tags=["social"])
app.include_router(search.router,       prefix="/api/v1/search",       tags=["search"])
app.include_router(export.router,       prefix="/api/v1/export",       tags=["export"])
app.include_router(collections.router,  prefix="/api/v1/collections",  tags=["collections"])
# Self-issued `eido_` API tokens were removed: Janua is the identity master and
# issues machine tokens (README §3). The parallel token system was never
# validated anywhere, so it was dead code as well as an SoC violation. The
# orphaned `api_tokens` table should be dropped in a follow-up migration.


@app.get("/")
async def root() -> dict:
    return {
        "service": "eido-api",
        "tagline": "Capture Reality. Command Form.",
        "version": settings.app_version,
    }
