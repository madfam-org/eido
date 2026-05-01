"""
Developer API Tokens Router

Allows users to generate long-lived API tokens for programmatic access.
Tokens are stored as SHA-256 hashes — never in plaintext.

POST /api/v1/tokens/              — create a token (returns plaintext once)
GET  /api/v1/tokens/              — list token metadata (no plaintext)
DELETE /api/v1/tokens/{token_id}  — revoke a token
"""
import hashlib
import logging
import secrets
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, String, Text, Boolean, func, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from eido_api.auth import JanuaUser, get_current_user
from eido_api.db.session import Base, get_db

logger = logging.getLogger(__name__)
router = APIRouter()

_TOKEN_PREFIX = "eido_"
_TOKEN_BYTES = 32


# ── Model ─────────────────────────────────────────────────────────────────────

class APIToken(Base):
    __tablename__ = "api_tokens"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(PGUUID(as_uuid=True), nullable=False)
    name = Column(String(255), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)  # SHA-256 hex
    prefix = Column(String(12), nullable=False)                    # First 8 chars for display
    is_active = Column(Boolean, nullable=False, server_default="true")
    last_used_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True))


# ── Schemas ───────────────────────────────────────────────────────────────────

class TokenCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Human label for this token")


class TokenCreatedResponse(BaseModel):
    id: str
    name: str
    token: str    # Only returned on creation — never again
    prefix: str
    created_at: str
    warning: str = "Store this token securely. It will not be shown again."


class TokenSummary(BaseModel):
    id: str
    name: str
    prefix: str
    is_active: bool
    last_used_at: str | None
    created_at: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/", response_model=TokenCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_token(
    data: TokenCreate,
    user: JanuaUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TokenCreatedResponse:
    """
    Create a developer API token. Returns plaintext token ONCE.
    Only a SHA-256 hash is persisted.
    """
    raw = f"{_TOKEN_PREFIX}{secrets.token_hex(_TOKEN_BYTES)}"
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    prefix = raw[:12]  # "eido_" + 7 chars — enough to identify without exposing

    token = APIToken(
        user_id=user.id,
        name=data.name,
        token_hash=token_hash,
        prefix=prefix,
    )
    db.add(token)
    await db.flush()
    await db.refresh(token)

    logger.info("API token created", extra={"user_id": user.id, "name": data.name})
    return TokenCreatedResponse(
        id=str(token.id),
        name=token.name,
        token=raw,
        prefix=prefix,
        created_at=str(token.created_at),
    )


@router.get("/", response_model=list[TokenSummary])
async def list_tokens(
    user: JanuaUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TokenSummary]:
    """List the authenticated user's API tokens (no plaintext returned)."""
    result = await db.execute(
        select(APIToken).where(
            APIToken.user_id == user.id,
            APIToken.is_active == True,  # noqa: E712
        ).order_by(APIToken.created_at.desc())
    )
    tokens = result.scalars().all()
    return [
        TokenSummary(
            id=str(t.id), name=t.name, prefix=t.prefix,
            is_active=t.is_active,
            last_used_at=str(t.last_used_at) if t.last_used_at else None,
            created_at=str(t.created_at),
        )
        for t in tokens
    ]


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    token_id: UUID,
    user: JanuaUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke (soft-delete) an API token."""
    result = await db.execute(
        select(APIToken).where(APIToken.id == token_id, APIToken.user_id == user.id)
    )
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=404, detail="Token not found.")
    token.is_active = False
    logger.info("API token revoked", extra={"token_id": str(token_id), "user_id": user.id})
