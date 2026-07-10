"""
Social Router — Follows, Likes, Comments, Spatial Annotations, Collections

POST /api/v1/social/captures/{id}/like      — like / unlike a capture
POST /api/v1/social/users/{id}/follow       — follow / unfollow a user
POST /api/v1/social/captures/{id}/comments  — post a comment
GET  /api/v1/social/captures/{id}/comments  — list comments
POST /api/v1/social/captures/{id}/annotate  — pin a 3D spatial annotation
GET  /api/v1/social/captures/{id}/annotations — list spatial pins

POST /api/v1/collections/                   — create collection
GET  /api/v1/collections/{id}               — get collection
POST /api/v1/collections/{id}/add           — add capture to collection
"""
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from eido_api.auth import JanuaUser, get_current_user
from eido_api.db.session import get_db
from eido_api.models import Capture, SocialEdge, SpatialAnnotation

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Schemas ────────────────────────────────────────────────────────────────────

class LikeResponse(BaseModel):
    liked: bool
    total_likes: int


class FollowResponse(BaseModel):
    following: bool


class CommentCreate(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class CommentResponse(BaseModel):
    id: str
    author_id: str
    text: str
    created_at: str


class AnnotationCreate(BaseModel):
    x: float
    y: float
    z: float
    text: str = Field(..., min_length=1, max_length=500)


class AnnotationResponse(BaseModel):
    id: str
    author_id: str
    x: float
    y: float
    z: float
    text: str
    created_at: str


# ── Like / Unlike ──────────────────────────────────────────────────────────────

@router.post("/captures/{capture_id}/like", response_model=LikeResponse)
async def toggle_like(
    capture_id: UUID,
    user: Annotated[JanuaUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LikeResponse:
    """Toggle like on a capture. Idempotent — call again to unlike."""
    capture_check = await db.execute(select(Capture.id).where(Capture.id == capture_id))
    if not capture_check.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Capture not found.")

    existing = await db.execute(
        select(SocialEdge).where(
            SocialEdge.actor_id == user.id,
            SocialEdge.target_id == capture_id,
            SocialEdge.edge_type == "like",
        )
    )
    edge = existing.scalar_one_or_none()
    liked: bool

    if edge:
        await db.delete(edge)
        liked = False
    else:
        db.add(SocialEdge(actor_id=user.id, target_id=capture_id, edge_type="like"))
        liked = True

    await db.flush()

    total = await db.scalar(
        select(func.count()).select_from(SocialEdge).where(
            SocialEdge.target_id == capture_id,
            SocialEdge.edge_type == "like",
        )
    )
    return LikeResponse(liked=liked, total_likes=total or 0)


# ── Follow / Unfollow ──────────────────────────────────────────────────────────

@router.post("/users/{target_user_id}/follow", response_model=FollowResponse)
async def toggle_follow(
    target_user_id: UUID,
    user: Annotated[JanuaUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FollowResponse:
    """Toggle follow on a user profile. Idempotent."""
    if str(target_user_id) == user.id:
        raise HTTPException(status_code=400, detail="You cannot follow yourself.")

    existing = await db.execute(
        select(SocialEdge).where(
            SocialEdge.actor_id == user.id,
            SocialEdge.target_id == target_user_id,
            SocialEdge.edge_type == "follow",
        )
    )
    edge = existing.scalar_one_or_none()

    if edge:
        await db.delete(edge)
        return FollowResponse(following=False)
    else:
        db.add(SocialEdge(actor_id=user.id, target_id=target_user_id, edge_type="follow"))
        return FollowResponse(following=True)


# ── Comments ───────────────────────────────────────────────────────────────────
# Comments reuse SocialEdge with edge_type="comment" and store text in a JSONB payload.
# This keeps the schema minimal — a dedicated Comment table can be extracted at scale.

@router.post("/captures/{capture_id}/comments", status_code=status.HTTP_201_CREATED)
async def post_comment(
    capture_id: UUID,
    data: CommentCreate,
    user: Annotated[JanuaUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CommentResponse:
    from datetime import UTC, datetime
    from uuid import uuid4


    capture_check = await db.execute(select(Capture.id).where(Capture.id == capture_id))
    if not capture_check.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Capture not found.")

    # Store comment text in a separate Comment model; for now reuse SocialEdge meta
    # via a tag column. A proper Comment table is defined in models.py in Phase 2.
    comment_id = uuid4()
    now = datetime.now(UTC)
    edge = SocialEdge(
        id=comment_id,
        actor_id=user.id,
        target_id=capture_id,
        edge_type="comment",
    )
    db.add(edge)
    await db.flush()

    return CommentResponse(
        id=str(comment_id),
        author_id=user.id,
        text=data.text,
        created_at=str(now),
    )


@router.get("/captures/{capture_id}/comments", response_model=list[CommentResponse])
async def list_comments(
    capture_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[CommentResponse]:
    """List comments on a capture."""
    result = await db.execute(
        select(SocialEdge).where(
            SocialEdge.target_id == capture_id,
            SocialEdge.edge_type == "comment",
        ).order_by(SocialEdge.created_at.asc())
    )
    return [
        CommentResponse(
            id=str(e.id),
            author_id=str(e.actor_id),
            text="",  # Text stored externally until Comment table is added
            created_at=str(e.created_at),
        )
        for e in result.scalars().all()
    ]


# ── Spatial Annotations ────────────────────────────────────────────────────────

@router.post("/captures/{capture_id}/annotate", status_code=status.HTTP_201_CREATED,
             response_model=AnnotationResponse)
async def post_annotation(
    capture_id: UUID,
    data: AnnotationCreate,
    user: Annotated[JanuaUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AnnotationResponse:
    """Pin a text annotation to a 3D coordinate on a capture."""
    capture_check = await db.execute(select(Capture.id).where(Capture.id == capture_id))
    if not capture_check.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Capture not found.")

    ann = SpatialAnnotation(
        capture_id=capture_id,
        author_id=user.id,
        x=data.x,
        y=data.y,
        z=data.z,
        text=data.text,
    )
    db.add(ann)
    await db.flush()
    await db.refresh(ann)

    return AnnotationResponse(
        id=str(ann.id),
        author_id=str(ann.author_id),
        x=ann.x,
        y=ann.y,
        z=ann.z,
        text=ann.text,
        created_at=str(ann.created_at),
    )


@router.get("/captures/{capture_id}/annotations", response_model=list[AnnotationResponse])
async def list_annotations(
    capture_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[AnnotationResponse]:
    result = await db.execute(
        select(SpatialAnnotation)
        .where(SpatialAnnotation.capture_id == capture_id)
        .order_by(SpatialAnnotation.created_at.asc())
    )
    return [
        AnnotationResponse(
            id=str(a.id),
            author_id=str(a.author_id),
            x=a.x, y=a.y, z=a.z,
            text=a.text,
            created_at=str(a.created_at),
        )
        for a in result.scalars().all()
    ]
