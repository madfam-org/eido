"""
Handoffs Router — Ecosystem dispatch status and manual re-dispatch.

GET  /api/v1/handoffs/{capture_id}        — list all handoffs for a capture
POST /api/v1/handoffs/{capture_id}/retry  — re-dispatch a failed handoff to a target
"""
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eido_api.auth import JanuaUser, get_current_user
from eido_api.db.session import get_db
from eido_api.models import Capture, EcosystemHandoff, HandoffTarget

logger = logging.getLogger(__name__)
router = APIRouter()


class HandoffSummary(BaseModel):
    id: str
    target: str
    status: str
    upstream_job_id: str | None
    dispatched_at: str
    responded_at: str | None


class RetryRequest(BaseModel):
    target: HandoffTarget


@router.get("/{capture_id}", response_model=list[HandoffSummary])
async def list_handoffs(
    capture_id: UUID,
    user: Annotated[JanuaUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[HandoffSummary]:
    """List all ecosystem dispatch records for a capture (owner only)."""
    capture_result = await db.execute(select(Capture).where(Capture.id == capture_id))
    capture = capture_result.scalar_one_or_none()
    if not capture:
        raise HTTPException(status_code=404, detail="Capture not found.")
    if str(capture.author_id) != user.id and "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="Not the capture owner.")

    result = await db.execute(
        select(EcosystemHandoff)
        .where(EcosystemHandoff.capture_id == capture_id)
        .order_by(EcosystemHandoff.dispatched_at.desc())
    )
    handoffs = result.scalars().all()
    return [
        HandoffSummary(
            id=str(h.id),
            target=h.target.value if h.target else str(h.target),
            status=h.status,
            upstream_job_id=h.upstream_job_id,
            dispatched_at=str(h.dispatched_at),
            responded_at=str(h.responded_at) if h.responded_at else None,
        )
        for h in handoffs
    ]


@router.post("/{capture_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_handoff(
    capture_id: UUID,
    data: RetryRequest,
    background_tasks: BackgroundTasks,
    user: Annotated[JanuaUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Manually re-dispatch a failed ecosystem handoff."""
    capture_result = await db.execute(select(Capture).where(Capture.id == capture_id))
    capture = capture_result.scalar_one_or_none()
    if not capture:
        raise HTTPException(status_code=404, detail="Capture not found.")
    if str(capture.author_id) != user.id and "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="Not the capture owner.")

    from eido_api.services.handoff import (
        _dispatch_blueprint_harvester, _dispatch_ceq, _dispatch_factlas,
    )

    dispatcher_map = {
        HandoffTarget.BLUEPRINT_HARVESTER: _dispatch_blueprint_harvester,
        HandoffTarget.FACTLAS: _dispatch_factlas,
        HandoffTarget.CEQ: _dispatch_ceq,
    }
    dispatcher = dispatcher_map.get(data.target)
    if not dispatcher:
        raise HTTPException(status_code=400, detail=f"Retry not supported for target: {data.target}")

    background_tasks.add_task(dispatcher, capture)
    logger.info("Manual handoff retry: capture=%s target=%s", capture_id, data.target)
    return {"status": "retrying", "capture_id": str(capture_id), "target": data.target}
