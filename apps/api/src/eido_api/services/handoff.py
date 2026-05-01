"""
Ecosystem Handoff Service — SoC Dispatch Layer

Fires webhooks from Eido to:
  • Blueprint Harvester — canonical metadata + CDN file pointers for indexing
  • Yantra4D           — metric-scaled mesh for parametric CAD manipulation
  • Factlas            — georeferenced 3D Tiles for urban digital twin ingestion
  • CEQ                — 360° turntable render trigger for marketing generation

Each dispatch is logged in the ecosystem_handoffs table for audit and retry.
"""
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select

from eido_api.config import get_settings
from eido_api.db.session import async_session_maker
from eido_api.models import Capture, CaptureStatus, EcosystemHandoff, HandoffTarget

logger = logging.getLogger(__name__)
settings = get_settings()

_TIMEOUT = httpx.Timeout(30.0)


async def _log_handoff(
    capture_id: str,
    target: HandoffTarget,
    status: str,
    payload: dict[str, Any],
    upstream_job_id: str | None = None,
) -> None:
    async with async_session_maker() as db:
        handoff = EcosystemHandoff(
            capture_id=capture_id,
            target=target,
            status=status,
            payload=payload,
            upstream_job_id=upstream_job_id,
            responded_at=datetime.now(UTC),
        )
        db.add(handoff)
        await db.commit()


async def _dispatch_blueprint_harvester(capture: Capture) -> None:
    """
    Push canonical metadata + CDN file pointers to Blueprint Harvester's
    permanent data lake for global vector indexing and archival.
    """
    payload = {
        "eido_id": str(capture.id),
        "author_id": str(capture.author_id),
        "title": capture.title,
        "mesh_url": capture.mesh_url,
        "splat_url": capture.splat_url,
        "thumbnail_url": capture.thumbnail_url,
        "license": capture.license,
        "scale_metric": capture.scale_metric or "millimeters",
        "tags": capture.tags or [],
        "vertex_count": capture.vertex_count,
        "gaussian_count": capture.gaussian_count,
        "source": "eido",
    }
    url = f"{settings.blueprint_harvester_url}/api/v1/assets/ingest_eido"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            await _log_handoff(str(capture.id), HandoffTarget.BLUEPRINT_HARVESTER, "accepted", payload, data.get("job_id"))
            logger.info("Handoff → Blueprint Harvester: OK", extra={"capture_id": str(capture.id)})
    except Exception as e:
        await _log_handoff(str(capture.id), HandoffTarget.BLUEPRINT_HARVESTER, "failed", payload)
        logger.error("Handoff → Blueprint Harvester FAILED: %s", e)


async def _dispatch_factlas(capture: Capture) -> None:
    """
    For georeferenced drone captures, push spatial coordinates to Factlas
    so the urban digital twin can be updated.
    Only fires when capture.is_georeferenced is True.
    """
    if not capture.is_georeferenced:
        return

    payload = {
        "lat": capture.latitude,
        "lon": capture.longitude,
        "type": "drone_capture",
        "properties": {
            "eido_id": str(capture.id),
            "title": capture.title,
            "mesh_url": capture.mesh_url,
            "altitude_m": capture.altitude_m,
        },
        "provider": "eido",
        "confidence": 0.95,
    }
    url = f"{settings.factlas_url}/api/v1/observations"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            await _log_handoff(str(capture.id), HandoffTarget.FACTLAS, "accepted", payload)
            logger.info("Handoff → Factlas: OK", extra={"capture_id": str(capture.id)})
    except Exception as e:
        await _log_handoff(str(capture.id), HandoffTarget.FACTLAS, "failed", payload)
        logger.error("Handoff → Factlas FAILED: %s", e)


async def _dispatch_ceq(capture: Capture) -> None:
    """
    Feed the capture's mesh URL into CEQ's ComfyUI synthesis workflow
    to trigger automated 360° turntable render generation for marketing.
    """
    payload = {
        "prompt": f"360-degree turntable render of: {capture.title}",
        "source_query": capture.title,
        "source_platform": "eido",
        "preferred_format": "mp4",
        "webhook_url": f"{settings.blueprint_harvester_url}/api/v1/assets/marketing_complete",
        "context": {
            "mesh_url": capture.mesh_url,
            "thumbnail_url": capture.thumbnail_url,
            "eido_id": str(capture.id),
        },
    }
    url = f"{settings.ceq_url}/v1/synthesis/from_query"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            await _log_handoff(str(capture.id), HandoffTarget.CEQ, "accepted", payload, str(data.get("job_id", "")))
            logger.info("Handoff → CEQ: OK", extra={"capture_id": str(capture.id)})
    except Exception as e:
        await _log_handoff(str(capture.id), HandoffTarget.CEQ, "failed", payload)
        logger.error("Handoff → CEQ FAILED: %s", e)


async def dispatch_all_handoffs(capture_id: str) -> None:
    """
    Master dispatch — fires all applicable ecosystem handoffs for a published capture.
    Runs as a FastAPI background task after publish is confirmed.
    """
    async with async_session_maker() as db:
        result = await db.execute(select(Capture).where(Capture.id == capture_id))
        capture = result.scalar_one_or_none()
        if not capture:
            logger.error("dispatch_all_handoffs: capture %s not found", capture_id)
            return

    # Always fire: Blueprint Harvester (permanent ledger)
    await _dispatch_blueprint_harvester(capture)

    # Conditional: Factlas (only georeferenced drone captures)
    await _dispatch_factlas(capture)

    # Always fire: CEQ (marketing automation)
    await _dispatch_ceq(capture)

    logger.info("All ecosystem handoffs dispatched", extra={"capture_id": capture_id})
