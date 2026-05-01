"""
Collections Router — Albums / Curated Groups of Captures

POST /api/v1/collections/              — create a collection
GET  /api/v1/collections/{id}          — get collection detail + captures
PUT  /api/v1/collections/{id}          — update title / description / privacy
DELETE /api/v1/collections/{id}        — soft delete
POST /api/v1/collections/{id}/add      — add a capture
DELETE /api/v1/collections/{id}/captures/{cid} — remove a capture
GET  /api/v1/collections/              — list public collections (paginated)
GET  /api/v1/collections/mine          — list authenticated user's collections
"""
import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, ForeignKey, String, Table, Text, Boolean, func, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from eido_api.auth import JanuaUser, get_current_user, get_optional_user
from eido_api.db.session import Base, get_db
from eido_api.models import Capture, CaptureStatus

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Models (defined here to keep collections self-contained) ──────────────────

class Collection(Base):
    __tablename__ = "collections"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    author_id = Column(PGUUID(as_uuid=True), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text)
    is_public = Column(Boolean, nullable=False, server_default="false")
    cover_capture_id = Column(PGUUID(as_uuid=True), nullable=True)
    is_deleted = Column(Boolean, nullable=False, server_default="false")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


collection_captures = Table(
    "collection_captures",
    Base.metadata,
    Column("collection_id", PGUUID(as_uuid=True), ForeignKey("collections.id"), primary_key=True),
    Column("capture_id", PGUUID(as_uuid=True), ForeignKey("captures.id"), primary_key=True),
    Column("added_at", DateTime(timezone=True), server_default=func.now()),
    Column("position", String(50), server_default="0"),
)


# ── Schemas ────────────────────────────────────────────────────────────────────

class CollectionCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    is_public: bool = False


class CollectionUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    is_public: bool | None = None
    cover_capture_id: UUID | None = None


class CollectionSummary(BaseModel):
    id: str
    author_id: str
    title: str
    description: str | None
    is_public: bool
    capture_count: int
    cover_thumbnail_url: str | None
    created_at: str


class CollectionDetail(CollectionSummary):
    captures: list[dict]


class AddCaptureRequest(BaseModel):
    capture_id: UUID


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _get_owned_collection(
    collection_id: UUID, user: JanuaUser, db: AsyncSession
) -> Collection:
    result = await db.execute(
        select(Collection).where(
            Collection.id == collection_id,
            Collection.is_deleted == False,  # noqa: E712
        )
    )
    col = result.scalar_one_or_none()
    if not col:
        raise HTTPException(status_code=404, detail="Collection not found.")
    if str(col.author_id) != user.id and "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="Not the collection owner.")
    return col


async def _capture_count(collection_id: UUID, db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count()).select_from(collection_captures).where(
            collection_captures.c.collection_id == collection_id
        )
    )
    return result.scalar() or 0


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/", response_model=CollectionSummary, status_code=status.HTTP_201_CREATED)
async def create_collection(
    data: CollectionCreate,
    user: Annotated[JanuaUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CollectionSummary:
    """Create a new collection (album) of captures."""
    col = Collection(
        id=uuid4(),
        author_id=user.id,
        title=data.title,
        description=data.description,
        is_public=data.is_public,
    )
    db.add(col)
    await db.flush()
    return CollectionSummary(
        id=str(col.id), author_id=str(col.author_id),
        title=col.title, description=col.description,
        is_public=col.is_public, capture_count=0,
        cover_thumbnail_url=None, created_at=str(col.created_at),
    )


@router.get("/mine", response_model=list[CollectionSummary])
async def list_my_collections(
    user: Annotated[JanuaUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> list[CollectionSummary]:
    """List all collections owned by the authenticated user."""
    result = await db.execute(
        select(Collection).where(
            Collection.author_id == user.id,
            Collection.is_deleted == False,  # noqa: E712
        ).order_by(Collection.updated_at.desc()).offset(skip).limit(limit)
    )
    cols = result.scalars().all()
    return [
        CollectionSummary(
            id=str(c.id), author_id=str(c.author_id),
            title=c.title, description=c.description,
            is_public=c.is_public,
            capture_count=await _capture_count(c.id, db),
            cover_thumbnail_url=None, created_at=str(c.created_at),
        )
        for c in cols
    ]


@router.get("/", response_model=list[CollectionSummary])
async def list_public_collections(
    db: Annotated[AsyncSession, Depends(get_db)],
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> list[CollectionSummary]:
    """Browse public collections."""
    result = await db.execute(
        select(Collection).where(
            Collection.is_public == True,  # noqa: E712
            Collection.is_deleted == False,  # noqa: E712
        ).order_by(Collection.updated_at.desc()).offset(skip).limit(limit)
    )
    cols = result.scalars().all()
    return [
        CollectionSummary(
            id=str(c.id), author_id=str(c.author_id),
            title=c.title, description=c.description,
            is_public=c.is_public,
            capture_count=await _capture_count(c.id, db),
            cover_thumbnail_url=None, created_at=str(c.created_at),
        )
        for c in cols
    ]


@router.get("/{collection_id}", response_model=CollectionDetail)
async def get_collection(
    collection_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: JanuaUser | None = Depends(get_optional_user),
) -> CollectionDetail:
    """Get a collection with its captures."""
    result = await db.execute(
        select(Collection).where(
            Collection.id == collection_id,
            Collection.is_deleted == False,  # noqa: E712
        )
    )
    col = result.scalar_one_or_none()
    if not col:
        raise HTTPException(status_code=404, detail="Collection not found.")
    if not col.is_public and (not user or str(col.author_id) != user.id):
        raise HTTPException(status_code=403, detail="Private collection.")

    # Fetch ordered captures
    cc_result = await db.execute(
        select(collection_captures).where(
            collection_captures.c.collection_id == collection_id
        ).order_by(collection_captures.c.position, collection_captures.c.added_at)
    )
    capture_ids = [row.capture_id for row in cc_result.fetchall()]

    captures_data = []
    for cid in capture_ids:
        cap_result = await db.execute(
            select(Capture).where(Capture.id == cid, Capture.status == CaptureStatus.READY)
        )
        cap = cap_result.scalar_one_or_none()
        if cap:
            captures_data.append({
                "id": str(cap.id), "title": cap.title,
                "thumbnail_url": cap.thumbnail_url, "mode": cap.mode.value if cap.mode else "3dgs",
            })

    return CollectionDetail(
        id=str(col.id), author_id=str(col.author_id),
        title=col.title, description=col.description,
        is_public=col.is_public, capture_count=len(capture_ids),
        cover_thumbnail_url=None, created_at=str(col.created_at),
        captures=captures_data,
    )


@router.put("/{collection_id}", response_model=CollectionSummary)
async def update_collection(
    collection_id: UUID,
    data: CollectionUpdate,
    user: Annotated[JanuaUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CollectionSummary:
    col = await _get_owned_collection(collection_id, user, db)
    for field, val in data.model_dump(exclude_unset=True).items():
        setattr(col, field, val)
    col.updated_at = datetime.now(UTC)
    await db.flush()
    return CollectionSummary(
        id=str(col.id), author_id=str(col.author_id),
        title=col.title, description=col.description,
        is_public=col.is_public,
        capture_count=await _capture_count(col.id, db),
        cover_thumbnail_url=None, created_at=str(col.created_at),
    )


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_collection(
    collection_id: UUID,
    user: Annotated[JanuaUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    col = await _get_owned_collection(collection_id, user, db)
    col.is_deleted = True
    col.updated_at = datetime.now(UTC)


@router.post("/{collection_id}/add", status_code=status.HTTP_204_NO_CONTENT)
async def add_capture_to_collection(
    collection_id: UUID,
    data: AddCaptureRequest,
    user: Annotated[JanuaUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    col = await _get_owned_collection(collection_id, user, db)

    cap_result = await db.execute(select(Capture.id).where(Capture.id == data.capture_id))
    if not cap_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Capture not found.")

    # Idempotent insert
    existing = await db.execute(
        select(collection_captures).where(
            collection_captures.c.collection_id == collection_id,
            collection_captures.c.capture_id == data.capture_id,
        )
    )
    if not existing.fetchone():
        await db.execute(
            collection_captures.insert().values(
                collection_id=collection_id,
                capture_id=data.capture_id,
            )
        )
    col.updated_at = datetime.now(UTC)


@router.delete("/{collection_id}/captures/{capture_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_capture_from_collection(
    collection_id: UUID,
    capture_id: UUID,
    user: Annotated[JanuaUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    col = await _get_owned_collection(collection_id, user, db)
    await db.execute(
        collection_captures.delete().where(
            collection_captures.c.collection_id == collection_id,
            collection_captures.c.capture_id == capture_id,
        )
    )
    col.updated_at = datetime.now(UTC)
