"""
Jobs Router — GPU processing job status and management.

GET  /api/v1/jobs/{job_id}     — poll job status
GET  /api/v1/jobs/             — list jobs for current user
PATCH /api/v1/captures/{id}/status — internal callback from orchestration worker
"""
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eido_api.config import get_settings
from eido_api.db.session import get_db
from eido_api.models import Capture, CaptureStatus, User
from eido_api.services.provisioning import get_provisioned_user

settings = get_settings()

logger = logging.getLogger(__name__)
router = APIRouter()


class JobStatusResponse(BaseModel):
    capture_id: str
    status: str
    processing_stage: str | None = None
    progress_pct: int | None = None
    splat_url: str | None = None
    mesh_url: str | None = None
    thumbnail_url: str | None = None
    error_message: str | None = None
    processing_time_s: float | None = None


class StatusPatchRequest(BaseModel):
    """Internal-only: called by orchestration worker to update capture status."""
    status: str
    splat_url: str | None = None
    mesh_url: str | None = None
    thumbnail_url: str | None = None
    gaussian_count: int | None = None
    vertex_count: int | None = None
    processing_time_s: float | None = None
    error_message: str | None = None


_STATUS_STAGE_MAP = {
    CaptureStatus.UPLOADING: ("Uploading raw data", 5),
    CaptureStatus.QUEUED: ("Queued for processing", 10),
    CaptureStatus.PROCESSING_SFM: ("Structure-from-Motion alignment", 30),
    CaptureStatus.PROCESSING_3DGS: ("3D Gaussian Splatting (30k iterations)", 60),
    CaptureStatus.PROCESSING_MESH: ("Splat-to-mesh conversion", 85),
    CaptureStatus.READY: ("Complete", 100),
    CaptureStatus.FAILED: ("Failed", None),
}


@router.get("/{capture_id}", response_model=JobStatusResponse)
async def get_job_status(
    capture_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobStatusResponse:
    """Poll processing status for a capture. Public endpoint (no auth required)."""
    result = await db.execute(select(Capture).where(Capture.id == capture_id))
    capture = result.scalar_one_or_none()
    if not capture:
        raise HTTPException(status_code=404, detail="Capture not found.")

    stage, pct = _STATUS_STAGE_MAP.get(capture.status, ("Unknown", None))
    return JobStatusResponse(
        capture_id=str(capture.id),
        status=capture.status.value if capture.status else "unknown",
        processing_stage=stage,
        progress_pct=pct,
        splat_url=capture.splat_url,
        mesh_url=capture.mesh_url,
        thumbnail_url=capture.thumbnail_url,
        error_message=capture.error_message,
        processing_time_s=capture.processing_time_s,
    )


@router.get("/", response_model=list[JobStatusResponse])
async def list_my_jobs(
    user: Annotated[User, Depends(get_provisioned_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
) -> list[JobStatusResponse]:
    """List the authenticated user's captures + their processing status."""
    q = (
        select(Capture)
        .where(Capture.author_id == user.id)
        .order_by(Capture.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    captures = (await db.execute(q)).scalars().all()
    results = []
    for c in captures:
        stage, pct = _STATUS_STAGE_MAP.get(c.status, ("Unknown", None))
        results.append(JobStatusResponse(
            capture_id=str(c.id),
            status=c.status.value if c.status else "unknown",
            processing_stage=stage,
            progress_pct=pct,
            splat_url=c.splat_url,
            mesh_url=c.mesh_url,
            thumbnail_url=c.thumbnail_url,
            error_message=c.error_message,
            processing_time_s=c.processing_time_s,
        ))
    return results


# Internal endpoint — called by the orchestration worker. Guarded by a shared
# service token (X-Internal-Token) rather than a user session: previously any
# caller on the network could flip a capture to READY/public and fan out the
# ecosystem handoffs.
@router.patch("/captures/{capture_id}/status", include_in_schema=False)
async def update_capture_status(
    capture_id: UUID,
    data: StatusPatchRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_internal_token: Annotated[str | None, Header()] = None,
) -> dict:
    expected = settings.internal_api_token
    if not expected or x_internal_token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing internal service token.",
        )

    result = await db.execute(select(Capture).where(Capture.id == capture_id))
    capture = result.scalar_one_or_none()
    if not capture:
        raise HTTPException(status_code=404, detail="Capture not found.")

    try:
        capture.status = CaptureStatus(data.status)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid status: {data.status}")

    for field in ["splat_url", "mesh_url", "thumbnail_url", "gaussian_count",
                  "vertex_count", "processing_time_s", "error_message"]:
        val = getattr(data, field)
        if val is not None:
            setattr(capture, field, val)

    await db.flush()
    logger.info("Capture %s status → %s", capture_id, data.status)

    # On READY: fire auto-tagging (Selva) + ecosystem handoffs in background
    if data.status == CaptureStatus.READY.value and capture.is_public:
        from eido_api.services.auto_tag import auto_tag_capture
        from eido_api.services.handoff import dispatch_all_handoffs
        import asyncio
        asyncio.create_task(auto_tag_capture(str(capture_id)))
        asyncio.create_task(dispatch_all_handoffs(str(capture_id)))

    return {"ok": True}

