"""
Capture Router — Ingest, Publish & Portfolio Listing

POST /api/v1/captures/ingest   — register a new capture + pre-signed S3 upload URL
POST /api/v1/captures/{id}/publish — mark capture as public, fire ecosystem handoffs
GET  /api/v1/captures/         — paginated portfolio listing (public)
GET  /api/v1/captures/{id}     — single capture detail
"""
import logging
import uuid
from datetime import datetime, UTC
from typing import Any

import boto3
from botocore.client import Config
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eido_api.config import get_settings
from eido_api.db.session import get_db
from eido_api.db.redis import enqueue_job
from eido_api.models import Capture, CaptureMode, CaptureStatus, User
from eido_api.services.provisioning import get_provisioned_user

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()


# ── Schemas ────────────────────────────────────────────────────────────────────

class IngestRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    mode: CaptureMode = CaptureMode.GAUSSIAN_SPLATTING
    file_name: str = Field(..., description="Original filename for S3 key generation")
    file_size_bytes: int = Field(..., gt=0)
    latitude: float | None = None
    longitude: float | None = None
    altitude_m: float | None = None
    tags: list[str] = []
    license: str = "CC-BY-4.0"


class IngestResponse(BaseModel):
    capture_id: str
    upload_url: str          # Pre-signed S3 PUT URL
    upload_key: str          # S3 object key to PUT to
    expires_in_seconds: int = 3600


class CaptureResponse(BaseModel):
    id: str
    title: str
    description: str | None
    mode: str
    status: str
    splat_url: str | None
    mesh_url: str | None
    thumbnail_url: str | None
    vertex_count: int | None
    gaussian_count: int | None
    scale_metric: str | None
    is_public: bool
    license: str | None
    tags: list[str]
    is_georeferenced: bool
    created_at: str


# ── Helpers ────────────────────────────────────────────────────────────────────

def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4"),
    )


def _presign_put(key: str, expires: int = 3600) -> str:
    s3 = _s3_client()
    return s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.s3_bucket_raw, "Key": key},
        ExpiresIn=expires,
    )


def _capture_to_response(c: Capture) -> CaptureResponse:
    return CaptureResponse(
        id=str(c.id),
        title=c.title,
        description=c.description,
        mode=c.mode.value if c.mode else "3dgs",
        status=c.status.value if c.status else "uploading",
        splat_url=c.splat_url,
        mesh_url=c.mesh_url,
        thumbnail_url=c.thumbnail_url,
        vertex_count=c.vertex_count,
        gaussian_count=c.gaussian_count,
        scale_metric=c.scale_metric,
        is_public=c.is_public,
        license=c.license,
        tags=c.tags or [],
        is_georeferenced=c.is_georeferenced or False,
        created_at=str(c.created_at),
    )


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/ingest", response_model=IngestResponse, status_code=status.HTTP_201_CREATED)
async def ingest_capture(
    data: IngestRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_provisioned_user),
) -> IngestResponse:
    """
    Register a new capture and return a pre-signed S3 URL for direct upload.
    After upload, the mobile client calls the processing queue endpoint.
    """
    capture_id = uuid.uuid4()
    s3_key = f"raw/{capture_id}/{data.file_name}"

    # Create DB record
    capture = Capture(
        id=capture_id,
        author_id=user.id,
        title=data.title,
        description=data.description,
        mode=data.mode,
        status=CaptureStatus.UPLOADING,
        latitude=data.latitude,
        longitude=data.longitude,
        altitude_m=data.altitude_m,
        is_georeferenced=bool(data.latitude and data.longitude),
        tags=data.tags,
        license=data.license,
    )
    db.add(capture)
    await db.flush()
    await db.refresh(capture)

    # Generate pre-signed upload URL
    try:
        upload_url = _presign_put(s3_key)
    except Exception as e:
        logger.error("Failed to generate pre-signed URL: %s", e)
        raise HTTPException(status_code=503, detail="Storage service unavailable.")

    logger.info("Capture registered", extra={"capture_id": str(capture_id), "mode": data.mode})
    return IngestResponse(
        capture_id=str(capture_id),
        upload_url=upload_url,
        upload_key=s3_key,
    )


@router.post("/{capture_id}/process", status_code=status.HTTP_202_ACCEPTED)
async def trigger_processing(
    capture_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_provisioned_user),
) -> dict[str, Any]:
    """
    Enqueue a GPU processing job for this capture.
    Called by the mobile app after S3 upload completes.
    """
    result = await db.execute(select(Capture).where(Capture.id == capture_id))
    capture = result.scalar_one_or_none()
    if not capture:
        raise HTTPException(status_code=404, detail="Capture not found.")
    if capture.author_id != user.id:
        raise HTTPException(status_code=403, detail="Not the capture owner.")

    capture.status = CaptureStatus.QUEUED
    await db.flush()

    job_data = {
        "type": "3dgs_pipeline",
        "capture_id": str(capture_id),
        "mode": capture.mode.value if capture.mode else "3dgs",
        "s3_raw_key": f"raw/{capture_id}/",
        "is_georeferenced": capture.is_georeferenced,
    }
    background_tasks.add_task(enqueue_job, job_data)

    return {"status": "queued", "capture_id": capture_id, "message": "3DGS pipeline enqueued."}


@router.post("/{capture_id}/publish", status_code=status.HTTP_200_OK)
async def publish_capture(
    capture_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_provisioned_user),
) -> dict[str, Any]:
    """
    Mark a processed capture as public and fire all ecosystem handoff webhooks.
    """
    result = await db.execute(select(Capture).where(Capture.id == capture_id))
    capture = result.scalar_one_or_none()
    if not capture:
        raise HTTPException(status_code=404, detail="Capture not found.")
    if capture.author_id != user.id:
        raise HTTPException(status_code=403, detail="Not the capture owner.")
    if capture.status != CaptureStatus.READY:
        raise HTTPException(status_code=409, detail=f"Capture is not ready (status: {capture.status}).")

    capture.is_public = True
    await db.flush()

    # Fire ecosystem handoffs in background
    from eido_api.services.handoff import dispatch_all_handoffs
    background_tasks.add_task(dispatch_all_handoffs, str(capture_id))

    logger.info("Capture published", extra={"capture_id": capture_id})
    return {"status": "published", "capture_id": capture_id}


@router.get("/", response_model=list[CaptureResponse])
async def list_captures(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    tag: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[CaptureResponse]:
    """Paginated public portfolio listing."""
    q = select(Capture).where(
        Capture.is_public == True,  # noqa: E712
        Capture.status == CaptureStatus.READY,
    )
    if tag:
        q = q.where(Capture.tags.contains([tag]))
    q = q.order_by(Capture.created_at.desc()).offset(skip).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    return [_capture_to_response(c) for c in rows]


@router.get("/{capture_id}", response_model=CaptureResponse)
async def get_capture(capture_id: str, db: AsyncSession = Depends(get_db)) -> CaptureResponse:
    """Single capture detail."""
    result = await db.execute(select(Capture).where(Capture.id == capture_id))
    capture = result.scalar_one_or_none()
    if not capture:
        raise HTTPException(status_code=404, detail="Capture not found.")
    return _capture_to_response(capture)
