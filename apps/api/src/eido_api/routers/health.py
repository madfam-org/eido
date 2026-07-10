"""Health & readiness router.

- ``/health`` (liveness): shallow — is the process up. Stays dependency-free so
  a transient DB/Redis blip does NOT get the container killed mid-flight.
- ``/readyz`` (readiness): deep — can this pod actually serve? Checks DB + Redis
  so a pod that has lost its datastore is pulled from rotation (503) instead of
  500ing live traffic.
"""
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from eido_api.db.redis import get_redis
from eido_api.db.session import async_session_maker

logger = logging.getLogger(__name__)
router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="eido-api", version="0.1.0")


@router.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness: DB + Redis reachable. 200 when both pass, 503 otherwise."""
    checks: dict[str, str] = {}
    ok = True

    try:
        async with async_session_maker() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:  # noqa: BLE001 — readiness must never raise
        ok = False
        checks["database"] = "unreachable"
        logger.warning("readiness: database check failed: %s", e)

    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except Exception as e:  # noqa: BLE001
        ok = False
        checks["redis"] = "unreachable"
        logger.warning("readiness: redis check failed: %s", e)

    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ready" if ok else "not_ready", "checks": checks},
    )
