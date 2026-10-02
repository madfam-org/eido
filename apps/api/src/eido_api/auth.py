"""
Janua JWT Authentication Middleware
Verifies RS256 JWTs against Janua's JWKS endpoint at:
  https://auth.madfam.io/.well-known/jwks.json

Per the solarpunk-foundry cross-repo conventions:
  - RS256 only — HS256 is fail-closed after the 2026-04-23 audit
  - No HS256, no hardcoded secrets
  - Every authenticated route uses Depends(get_current_user)

Janua's issuer/JWKS contract:
  https://github.com/madfam-org/janua/blob/main/docs/reference/ISSUER_AND_JWKS.md
"""
import logging
import time
from typing import Annotated, Any

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import InvalidKeyError, InvalidTokenError, PyJWTError
from pydantic import BaseModel

from eido_api.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_bearer = HTTPBearer(auto_error=True)

# Janua signs with RS256 only. The allow-list is fixed here and never derived
# from the token header or the JWKS, so `none` and HMAC algorithms (key
# confusion with the public JWK) can never verify.
_JANUA_ALGORITHMS = ["RS256"]

# Clock-skew tolerance between Janua and this pod, applied to exp/nbf/iat.
# PyJWT rejects an `iat` in the future (python-jose did not).
_JANUA_LEEWAY_SECONDS = 30

# python-jose accepted a token without `exp` (it never expired). `aud` and `iss`
# are not checked here: this service has never configured an audience or issuer.
_JANUA_REQUIRED_CLAIMS = ["exp"]

# JWKS cache. Janua rotates its key with a hard cut, so a token naming a `kid`
# that is not in the cached set forces one refetch before it is rejected. Forced
# refetches are rate-limited process-wide, so tokens with forged `kid` values
# cannot turn into a stream of requests to Janua.
_jwks_cache: dict | None = None
_JWKS_FORCED_REFRESH_MIN_INTERVAL_S = 60.0
_jwks_forced_refresh_time: float | None = None


class _UnknownKeyIdError(InvalidTokenError):
    """The token's `kid` is not in the JWKS (possibly a key rotation)."""


async def _fetch_jwks(*, force: bool = False) -> dict:
    global _jwks_cache
    if _jwks_cache and not force:
        return _jwks_cache
    jwks_url = f"{settings.janua_url}/.well-known/jwks.json"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(jwks_url)
        resp.raise_for_status()
        _jwks_cache = resp.json()
        logger.debug("JWKS fetched from Janua: %d keys", len(_jwks_cache.get("keys", [])))
        return _jwks_cache


def _claim_forced_refresh() -> bool:
    """Return True when a forced JWKS refetch is allowed now, and record it."""
    global _jwks_forced_refresh_time
    now = time.monotonic()
    if (
        _jwks_forced_refresh_time is not None
        and (now - _jwks_forced_refresh_time) < _JWKS_FORCED_REFRESH_MIN_INTERVAL_S
    ):
        return False
    _jwks_forced_refresh_time = now
    return True


def _signing_key(jwks: dict, token: str) -> jwt.PyJWK:
    """Select the JWKS key named by the token's `kid` and bind it to RS256.

    Raises:
        PyJWTError: on a malformed header, a missing `kid`, an unknown `kid`
            (`_UnknownKeyIdError`), or a JWK that cannot verify RS256.
    """
    kid = jwt.get_unverified_header(token).get("kid")
    if kid is None:
        raise InvalidTokenError("Token header missing 'kid'")
    jwk: dict[str, Any] | None = next(
        (k for k in jwks.get("keys", []) if isinstance(k, dict) and k.get("kid") == kid),
        None,
    )
    if jwk is None:
        raise _UnknownKeyIdError(f"Signing key {kid!r} not found in JWKS")
    if jwk.get("alg") not in (None, *_JANUA_ALGORITHMS):
        raise InvalidKeyError(f"JWKS key alg {jwk.get('alg')!r} is not allowed")
    if jwk.get("use") not in (None, "sig"):
        raise InvalidKeyError(f"JWKS key use {jwk.get('use')!r} is not 'sig'")
    return jwt.PyJWK(jwk, algorithm=_JANUA_ALGORITHMS[0])


async def _verify_token(token: str) -> dict[str, Any]:
    """Verify a Janua RS256 JWT and return its claims. Raises PyJWTError."""
    jwks = await _fetch_jwks()
    try:
        key = _signing_key(jwks, token)
    except _UnknownKeyIdError:
        if not _claim_forced_refresh():
            raise
        logger.info("Token kid not in cached JWKS; refetching once (key rotation)")
        key = _signing_key(await _fetch_jwks(force=True), token)
    payload: dict[str, Any] = jwt.decode(
        token,
        key,
        algorithms=_JANUA_ALGORITHMS,
        leeway=_JANUA_LEEWAY_SECONDS,
        options={"require": _JANUA_REQUIRED_CLAIMS, "verify_aud": False},
    )
    return payload


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
        # Key selected by `kid`, RS256 only, `exp` required, 30 s leeway.
        # Audience is not verified (no audience is configured for this service).
        payload = await _verify_token(token)
    except PyJWTError as exc:
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
