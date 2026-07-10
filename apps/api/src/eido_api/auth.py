"""
Janua JWT Authentication Middleware
Verifies RS256 JWTs against Janua's JWKS endpoint at:
  https://auth.madfam.io/.well-known/jwks.json

Per the solarpunk-foundry cross-repo conventions:
  - RS256 only — HS256 is fail-closed after the 2026-04-23 audit
  - No HS256, no hardcoded secrets
  - Every authenticated route uses Depends(get_current_user)
"""
import logging
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from eido_api.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_bearer = HTTPBearer(auto_error=True)

# JWKS cache — refreshed on 401 from upstream
_jwks_cache: dict | None = None


async def _fetch_jwks() -> dict:
    global _jwks_cache
    if _jwks_cache:
        return _jwks_cache
    jwks_url = f"{settings.janua_url}/.well-known/jwks.json"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(jwks_url)
        resp.raise_for_status()
        _jwks_cache = resp.json()
        logger.debug("JWKS fetched from Janua: %d keys", len(_jwks_cache.get("keys", [])))
        return _jwks_cache


class JanuaUser(BaseModel):
    id: str
    org_id: str | None = None
    email: str | None = None
    username: str | None = None
    roles: list[str] = []
    tier: str = "free"   # Populated from Dhanam entitlement claim if present


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> JanuaUser:
    """
    Dependency: verify the Bearer JWT against Janua's JWKS.
    Returns a hydrated JanuaUser on success.
    Raises HTTP 401 on any failure.
    """
    token = credentials.credentials
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        jwks = await _fetch_jwks()
        # jose selects the correct key from JWKS by `kid` in the JWT header
        payload = jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            options={"verify_aud": False},  # Audience verified by Janua; we trust the sig
        )
    except JWTError as exc:
        global _jwks_cache
        _jwks_cache = None  # Invalidate cache on failure — key may have rotated
        logger.warning("JWT verification failed: %s", exc)
        raise credentials_exception from exc

    sub = payload.get("sub")
    if not sub:
        raise credentials_exception

    return JanuaUser(
        id=sub,
        org_id=payload.get("org_id"),
        email=payload.get("email"),
        username=payload.get("preferred_username"),
        roles=payload.get("roles", []),
        tier=payload.get("eido_tier", payload.get("tier", "free")),
    )


# Optional — non-blocking auth (for public routes that optionally identify user)
async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(
        HTTPBearer(auto_error=False)
    ),
) -> JanuaUser | None:
    if not credentials:
        return None
    try:
        return await get_current_user(credentials)
    except HTTPException:
        return None
