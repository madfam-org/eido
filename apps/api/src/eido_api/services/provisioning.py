"""
Janua → local user provisioning.

Janua is the identity master, so Eido stores no passwords — but it does need a
local ``users`` row to own captures, annotations, and social edges (those FKs
reference ``users.id``, a local UUID, not the Janua ``sub``). This module maps a
verified ``JanuaUser`` to a local ``User``, creating one on first sight.

Before this existed the ``users`` table was never written, so every capture
ingest violated ``captures.author_id NOT NULL`` and ``list_my_jobs`` compared a
local UUID column against the Janua ``sub`` string (never matching).
"""
from typing import Annotated

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eido_api.auth import JanuaUser, get_current_user
from eido_api.db.session import get_db
from eido_api.models import User


async def get_or_create_user(db: AsyncSession, janua: JanuaUser) -> User:
    """Return the local ``User`` for a Janua identity, creating it if absent."""
    result = await db.execute(select(User).where(User.janua_id == janua.id))
    user = result.scalar_one_or_none()
    if user is not None:
        return user

    # `sub` is globally unique, so a sub-derived username is collision-free when
    # Janua supplies no preferred_username.
    username = janua.username or f"user_{janua.id[:12]}"
    user = User(
        janua_id=janua.id,
        username=username,
        display_name=janua.username or username,
        tier=janua.tier or "free",
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user


async def get_provisioned_user(
    janua: Annotated[JanuaUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """FastAPI dependency: verified Janua token → local ``User`` row."""
    return await get_or_create_user(db, janua)
