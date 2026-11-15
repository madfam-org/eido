"""
Capture read access: who may see a capture and its artifact URLs.

One rule for every route that returns a capture, its artifacts, or anything
attached to it by capture id:

  - a public capture (``is_public``) is readable by anyone, signed in or not;
  - a private capture is readable only by its owner or an ``eido:admin``;
  - to everyone else it answers 404, the same as a capture that does not
    exist, so a private capture's existence is not disclosed.

Routes that need more than read access (processing, publishing, handoffs)
first apply this rule, so a caller who may not read the capture gets the same
404, and only then answer 403 to a reader who is not allowed to act.

The rule protects the record, not the files: artifacts on the public CDN are
readable by anyone who knows the capture id (see README, Known limits).

Ownership compares local ``users.id`` values (``captures.author_id`` is a
foreign key to it), never the Janua ``sub``. Administration is the
``eido:admin`` application role on the verified token; a bare ``admin``
organization role grants nothing.
"""
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eido_api.auth import JanuaUser, get_current_user, get_optional_user
from eido_api.db.session import get_db
from eido_api.models import Capture, User
from eido_api.services.provisioning import get_or_create_user


@dataclass(frozen=True)
class Viewer:
    """A verified caller: the local user row and the token it came from."""

    user: User
    janua: JanuaUser

    @property
    def is_admin(self) -> bool:
        return self.janua.is_admin


async def get_viewer(
    janua: Annotated[JanuaUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Viewer:
    """FastAPI dependency: a signed-in caller (401 without a valid token)."""
    return Viewer(user=await get_or_create_user(db, janua), janua=janua)


async def get_optional_viewer(
    janua: Annotated[JanuaUser | None, Depends(get_optional_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Viewer | None:
    """FastAPI dependency: the caller when a valid token is present, else None."""
    if janua is None:
        return None
    return Viewer(user=await get_or_create_user(db, janua), janua=janua)


def can_read_capture(capture: Capture, viewer: Viewer | None) -> bool:
    if capture.is_public:
        return True
    if viewer is None:
        return False
    return is_owner(capture, viewer) or viewer.is_admin


async def get_readable_capture(
    db: AsyncSession, capture_id: UUID | str, viewer: Viewer | None
) -> Capture:
    """Load a capture the viewer may read, or raise 404 (missing or private)."""
    result = await db.execute(select(Capture).where(Capture.id == capture_id))
    capture = result.scalar_one_or_none()
    if capture is None or not can_read_capture(capture, viewer):
        raise HTTPException(status_code=404, detail="Capture not found.")
    return capture


def is_owner(capture: Capture, viewer: Viewer) -> bool:
    return str(capture.author_id) == str(viewer.user.id)


async def get_actionable_capture(
    db: AsyncSession, capture_id: UUID | str, viewer: Viewer, *, allow_admin: bool
) -> Capture:
    """Load a capture the viewer may act on.

    404 when the viewer may not read it (or it does not exist); 403 when they
    may read it but are neither its owner nor, where allowed, an eido:admin.
    """
    capture = await get_readable_capture(db, capture_id, viewer)
    if not (is_owner(capture, viewer) or (allow_admin and viewer.is_admin)):
        raise HTTPException(status_code=403, detail="Not the capture owner.")
    return capture
