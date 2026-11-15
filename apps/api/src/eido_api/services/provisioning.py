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
import hashlib
from typing import Annotated

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from eido_api.auth import JanuaUser, get_current_user, get_optional_user
from eido_api.db.session import get_db
from eido_api.models import User


def _generated_username(janua_sub: str) -> str:
    """A username derived from a hash of the whole `sub`.

    A prefix of `sub` collides for service accounts (every machine subject
    starts with `service-account:`); a hash of the full value does not.
    """
    return f"user_{hashlib.sha256(janua_sub.encode()).hexdigest()[:16]}"


async def _select_user(db: AsyncSession, janua_sub: str) -> User | None:
    result = await db.execute(select(User).where(User.janua_id == janua_sub))
    user: User | None = result.scalar_one_or_none()
    return user


async def _insert_user(db: AsyncSession, janua: JanuaUser, username: str) -> User | None:
    """Insert in a SAVEPOINT; on a unique violation roll it back and return None."""
    user = User(
        janua_id=janua.id,
        username=username,
        display_name=janua.username or username,
        tier=janua.tier or "free",
    )
    try:
        async with db.begin_nested():
            db.add(user)
            await db.flush()
    except IntegrityError:
        return None
    await db.refresh(user)
    return user


async def get_or_create_user(db: AsyncSession, janua: JanuaUser) -> User:
    """Return the local ``User`` for a Janua identity, creating it if absent.

    Two first requests from the same person can race to insert the row. The
    loser's insert hits the unique constraint inside a SAVEPOINT, which is
    rolled back, and the row the winner inserted is selected instead. If the
    token's ``preferred_username`` is already taken by someone else, the
    hash-derived username is used.
    """
    user = await _select_user(db, janua.id)
    if user is not None:
        return user

    generated = _generated_username(janua.id)
    for username in dict.fromkeys([janua.username or generated, generated]):
        created = await _insert_user(db, janua, username)
        if created is not None:
            return created
        existing = await _select_user(db, janua.id)
        if existing is not None:
            return existing
    raise RuntimeError("Could not provision a local user for this Janua identity")


async def get_provisioned_user(
    janua: Annotated[JanuaUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """FastAPI dependency: verified Janua token → local ``User`` row."""
    return await get_or_create_user(db, janua)


async def get_optional_provisioned_user(
    janua: Annotated[JanuaUser | None, Depends(get_optional_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User | None:
    """Optional variant: local ``User`` when a valid token is present, else None.

    Owner checks on mixed public/private endpoints must compare local UUIDs, not
    the Janua ``sub`` — comparing ``capture.author_id`` (a ``users.id`` FK) to the
    Janua ``sub`` string 403'd the real owner from their own private capture.
    """
    if janua is None:
        return None
    return await get_or_create_user(db, janua)
