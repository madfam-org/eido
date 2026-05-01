"""
Search Router — full-text and tag-based capture discovery.

GET /api/v1/search/captures?q=&tags=&mode=&georeferenced=&skip=&limit=
"""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from eido_api.db.session import get_db
from eido_api.models import Capture, CaptureMode, CaptureStatus

logger = logging.getLogger(__name__)
router = APIRouter()


class SearchResult(BaseModel):
    id: str
    title: str
    description: str | None
    mode: str
    thumbnail_url: str | None
    splat_url: str | None
    mesh_url: str | None
    tags: list[str]
    vertex_count: int | None
    gaussian_count: int | None
    is_georeferenced: bool
    license: str | None


@router.get("/captures", response_model=list[SearchResult])
async def search_captures(
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = Query(None, description="Full-text search on title and description"),
    tags: list[str] = Query(default=[], description="Filter by tags (AND)"),
    mode: CaptureMode | None = Query(None, description="Filter by capture mode"),
    georeferenced: bool | None = Query(None, description="Filter to georeferenced captures only"),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> list[SearchResult]:
    """
    Search public captures by text, tags, mode, and georeferencing.
    Results ordered by recency.
    """
    query = select(Capture).where(
        Capture.is_public == True,  # noqa: E712
        Capture.status == CaptureStatus.READY,
    )

    # Full-text filter (ILIKE on title + description)
    if q:
        term = f"%{q.strip()}%"
        query = query.where(
            or_(
                Capture.title.ilike(term),
                Capture.description.ilike(term),
            )
        )

    # Tag filter — each tag must be present (AND semantics)
    for tag in tags:
        query = query.where(Capture.tags.contains([tag]))

    if mode:
        query = query.where(Capture.mode == mode)

    if georeferenced is not None:
        query = query.where(Capture.is_georeferenced == georeferenced)

    query = query.order_by(Capture.created_at.desc()).offset(skip).limit(limit)
    captures = (await db.execute(query)).scalars().all()

    return [
        SearchResult(
            id=str(c.id),
            title=c.title,
            description=c.description,
            mode=c.mode.value if c.mode else "3dgs",
            thumbnail_url=c.thumbnail_url,
            splat_url=c.splat_url,
            mesh_url=c.mesh_url,
            tags=c.tags or [],
            vertex_count=c.vertex_count,
            gaussian_count=c.gaussian_count,
            is_georeferenced=c.is_georeferenced or False,
            license=c.license,
        )
        for c in captures
    ]
