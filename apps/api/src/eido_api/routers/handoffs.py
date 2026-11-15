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

from eido_api.db.session import get_db
from eido_api.models import EcosystemHandoff, HandoffTarget
from eido_api.services.access import Viewer, get_actionable_capture, get_viewer

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
    viewer: Annotated[Viewer, Depends(get_viewer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[HandoffSummary]:
    """List all ecosystem dispatch records for a capture (owner or eido:admin).

    404 to anyone who cannot read the capture, 403 to other readers.
    """
    await get_actionable_capture(db, capture_id, viewer, allow_admin=True)

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
    viewer: Annotated[Viewer, Depends(get_viewer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Manually re-dispatch a failed ecosystem handoff (owner or eido:admin).

    Only a public capture is ever sent to another platform: publishing is what
    releases it. A private one answers 409, to its owner and to admins alike.
    """
    capture = await get_actionable_capture(db, capture_id, viewer, allow_admin=True)
    if not capture.is_public:
        raise HTTPException(
            status_code=409,
            detail="This capture is private. Publish it before sending it to other platforms.",
        )

    from eido_api.services.handoff import (
        _dispatch_blueprint_harvester,
        _dispatch_ceq,
        _dispatch_factlas,
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
