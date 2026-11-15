"""
Janua JWT Authentication Middleware
Verifies RS256 JWTs against Janua's JWKS endpoint at:
  https://auth.madfam.io/.well-known/jwks.json

Per the solarpunk-foundry cross-repo conventions:
  - RS256 only — HS256 is fail-closed after the 2026-04-23 audit
  - No HS256, no hardcoded secrets
  - Every authenticated route uses Depends(get_current_user)

Claims checked on every token (Janua's issuer/JWKS contract:
  https://github.com/madfam-org/janua/blob/main/docs/reference/ISSUER_AND_JWKS.md):
  - signature: RS256 with the JWKS key named by `kid`;
  - `exp`: present and not passed (30 s leeway);
  - `iss`: equals JANUA_ISSUER (default https://auth.madfam.io);
  - `aud`: names one of JANUA_AUDIENCES (default `eido-api,janua.dev`);
  - `sub`: present.

Janua signs every relying party's tokens with one key, so the signature only
proves "Janua minted this". The audience says for whom:
  - `eido-api` is this service's own audience. A token naming it is accepted
    as issued, except a refresh token (`type`, when present, must be "access").
  - `janua.dev` is Janua's shared platform audience. Janua's own sign-in mints
    a person's session token on it, and so does any OAuth client registered
    without an audience of its own. A token accepted only because of it must
    be a first-party session access token: `type == "access"`, no `client_id`,
    no `scope`, not a machine (client_credentials) token, not a service
    principal (`is_service_account`). Every token Janua issues to an OAuth
    client carries `client_id`, so a token obtained by another application
    cannot be replayed here.

Authorization uses application roles only: Janua's `roles` claim mixes
`<app>:<role>` roles with bare organization roles (`admin`, `owner`,
`member`), and a bare role grants nothing in eido. See `ADMIN_ROLE`.
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

# python-jose accepted a token without `exp` (it never expired). `iss` is
# required and compared by PyJWT; `aud` is required and checked against the
# allowlist in `_check_audience` (PyJWT cannot say which audience matched).
_JANUA_REQUIRED_CLAIMS = ["exp", "iss", "aud"]

# Janua's shared platform audience (its `JWT_AUDIENCE`). Tokens accepted only
# because of it are held to the first-party session rules in `_check_audience`.
_SHARED_SESSION_AUDIENCES = frozenset({"janua.dev"})

# Application role that grants eido-wide administration. A bare `admin` is an
# organization role in Janua and grants nothing here (owner ruling B2).
ADMIN_ROLE = "eido:admin"

# Machine (client_credentials) token markers, per janua docs/service-tokens.md.
_TOKEN_USE_CLIENT_CREDENTIALS = "client_credentials"
_ACTOR_TYPE_SERVICE_ACCOUNT = "service_account"
_SERVICE_ACCOUNT_SUBJECT_PREFIX = "service-account:"
_TOKEN_TYPE_ACCESS = "access"


def parse_audiences(raw: str) -> frozenset[str]:
    """Parse the comma-separated JANUA_AUDIENCES allowlist.

    Fails loudly at import on an empty list (every caller would be rejected)
    or a wildcard ("accept any audience" is what this check exists to close).
    """
    audiences = frozenset(a.strip() for a in raw.split(",") if a.strip())
    if not audiences:
        raise RuntimeError("JANUA_AUDIENCES is empty; set at least one accepted audience.")
    if "*" in audiences:
        raise RuntimeError("JANUA_AUDIENCES must list audiences explicitly; '*' is not allowed.")
    return audiences


_JANUA_ISSUER = settings.janua_issuer
_ALLOWED_AUDIENCES = parse_audiences(settings.janua_audiences)

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
        issuer=_JANUA_ISSUER,
        leeway=_JANUA_LEEWAY_SECONDS,
        options={"require": _JANUA_REQUIRED_CLAIMS, "verify_aud": False},
    )
    _check_audience(payload)
    return payload


def _token_audiences(payload: dict[str, Any]) -> list[str]:
    aud = payload.get("aud")
    if isinstance(aud, str):
        return [aud]
    if isinstance(aud, list) and all(isinstance(a, str) for a in aud):
        return aud
    raise InvalidTokenError("Token 'aud' is missing or malformed")


def _is_machine_token(payload: dict[str, Any]) -> bool:
    """Any one client_credentials marker classifies the token as a machine's."""
    sub = payload.get("sub")
    return (
        payload.get("token_use") == _TOKEN_USE_CLIENT_CREDENTIALS
        or payload.get("actor_type") == _ACTOR_TYPE_SERVICE_ACCOUNT
        or (isinstance(sub, str) and sub.startswith(_SERVICE_ACCOUNT_SUBJECT_PREFIX))
    )


def _check_audience(payload: dict[str, Any]) -> None:
    """Enforce the audience allowlist and the shared-audience session rules.

    Raises:
        InvalidTokenError: with the reason, which is logged, never returned.
    """
    audiences = set(_token_audiences(payload))
    own = audiences & (_ALLOWED_AUDIENCES - _SHARED_SESSION_AUDIENCES)
    shared = audiences & _ALLOWED_AUDIENCES & _SHARED_SESSION_AUDIENCES
    if not own and not shared:
        raise InvalidTokenError(f"Token audience {sorted(audiences)!r} is not accepted")

    token_type = payload.get("type")
    if own:
        # Issued for eido: accepted as issued, but never a refresh token.
        if token_type is not None and token_type != _TOKEN_TYPE_ACCESS:
            raise InvalidTokenError(f"Token type {token_type!r} is not an access token")
        return

    # Accepted only because of the shared audience: first-party session only.
    if token_type != _TOKEN_TYPE_ACCESS:
        raise InvalidTokenError(f"Shared-audience token type {token_type!r} is not 'access'")
    if _is_machine_token(payload):
        raise InvalidTokenError("Machine token on the shared audience; it must name eido's audience")
    if payload.get("is_service_account"):
        raise InvalidTokenError("Service principal token on the shared audience")
    if payload.get("client_id") or payload.get("scope"):
        raise InvalidTokenError(
            "Token issued to an OAuth client on the shared audience; it must name eido's audience"
        )


class JanuaUser(BaseModel):
    id: str
    org_id: str | None = None
    email: str | None = None
    username: str | None = None
    roles: list[str] = []
    tier: str = "free"   # Populated from Dhanam entitlement claim if present

    @property
    def is_admin(self) -> bool:
        """True only for the `eido:admin` application role; a bare `admin` grants nothing."""
        return ADMIN_ROLE in self.roles


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
        # Key selected by `kid`, RS256 only, `exp` required, 30 s leeway,
        # `iss` and `aud` checked (see the module docstring).
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
