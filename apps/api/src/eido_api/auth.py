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
  - signature: RS256 with the JWKS key named by `kid`; a token without a
    `kid` (Janua's first-party session tokens carry none) is verified against
    every published RS256 signing key (RSA, `use` absent or `sig`, `alg`
    absent or RS256) and accepted if one verifies;
  - `exp`: present and not passed (30 s leeway);
  - `iss`: equals JANUA_ISSUER (default https://auth.madfam.io);
  - `aud`: names one of JANUA_AUDIENCES (default `janua.dev` only);
  - `type`: equals "access" (refresh and ID tokens are refused);
  - `sub`: present.

Janua signs every relying party's tokens with one key, so the signature only
proves "Janua minted this". The audience says for whom:
  - `janua.dev` is Janua's shared platform audience. Janua's own sign-in mints
    a person's session token on it, and so does any OAuth client registered
    without an audience of its own. A token accepted only because of it must
    be a first-party session access token: no `client_id`, no `scope`, not a
    machine (client_credentials) token, not a service principal
    (`is_service_account`). Every token Janua issues to an OAuth client
    carries `client_id`, so on THIS path a token obtained by another
    application cannot be replayed here.
  - Any other audience an operator adds to JANUA_AUDIENCES (e.g. `eido-api`)
    is accepted as issued, OAuth-client and machine tokens included, so the
    replay protection above does NOT hold on this path. Janua reserves only
    the audiences listed in its registry (`reserved_oauth_boundaries.py`);
    `eido-api` is not on it, so today any Janua user can register an OAuth
    client claiming it and obtain tokens for it. Add an audience only after
    it is on that registry AND no existing client row already carries it.
    None is configured by default.

Authorization uses application roles only, and only from a first-party
session token. Janua's `roles` claim on a session token carries real
application-role grants (`<app>:<role>`) next to bare organization roles
(`admin`, `owner`, `member`); a bare role grants nothing in eido. On a token
issued to an OAuth client, `roles` also carries organization role names,
which organization admins choose freely (one can be named `eido:admin`), so
such a token never grants `eido:admin`. The verifier records which path
accepted the token (`JanuaUser.first_party_session`); see `ADMIN_ROLE`.
"""
import asyncio
import logging
import time
from typing import Annotated, Any

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import InvalidKeyError, InvalidSignatureError, InvalidTokenError, PyJWTError
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

# JWKS cache. Janua rotates its key with a hard cut, so a token whose key is
# not in the cached set (an unknown `kid`, or a kid-less token no cached key
# verifies) forces one refetch before it is rejected. Forced refetches share
# one process-wide rate limit, so junk tokens cannot turn into a stream of
# requests to Janua. The cache is also re-read after a TTL, so a retired key
# stops being trusted without waiting for a failure.
_jwks_cache: dict | None = None
_jwks_fetched_at: float | None = None
_JWKS_TTL_S = 3600.0
_JWKS_FORCED_REFRESH_MIN_INTERVAL_S = 60.0
_jwks_forced_refresh_time: float | None = None
# After a failed refresh of a cached key set, the next attempt waits this long
# (the cached keys keep working meanwhile; see _fetch_jwks).
_JWKS_RETRY_AFTER_FAILURE_S = 60.0
# With nothing cached nobody can sign in, so a failed first download is
# remembered only briefly: long enough that requests do not each wait out a
# timeout, short enough that one blip at pod start does not block sign-in.
_JWKS_COLD_RETRY_AFTER_S = 5.0
# Upper bound on one download, connection to last byte.
_JWKS_DOWNLOAD_DEADLINE_S = 15.0
_jwks_failed_at: float | None = None
_jwks_lock: asyncio.Lock | None = None
_jwks_lock_loop: asyncio.AbstractEventLoop | None = None


def _download_lock() -> asyncio.Lock:
    """The single-flight lock of the running event loop.

    An asyncio.Lock belongs to the loop it first waited on, so it is made per
    loop rather than at import (one loop per process in production; the tests
    run many).
    """
    global _jwks_lock, _jwks_lock_loop
    loop = asyncio.get_running_loop()
    if _jwks_lock is None or _jwks_lock_loop is not loop:
        _jwks_lock = asyncio.Lock()
        _jwks_lock_loop = loop
    return _jwks_lock


class JWKSUnavailableError(Exception):
    """Janua's key set could not be fetched, so no token can be verified now.

    Deliberately not a PyJWTError: the token may be perfectly valid, so the
    caller is told the service is unavailable (503), not that the token is
    bad (401).
    """


class _UnknownKeyIdError(InvalidTokenError):
    """The token's `kid` is not in the JWKS (possibly a key rotation)."""


async def _download_jwks() -> dict:
    jwks_url = f"{settings.janua_url}/.well-known/jwks.json"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(jwks_url)
        resp.raise_for_status()
        jwks: dict = resp.json()
        return jwks


def _usable_jwks(jwks: Any) -> dict:
    """The downloaded document when it is a key set with at least one key.

    Anything else (an empty object, a list, an empty `keys`) is a failed
    download, not a key set to cache: caching it would reject every token
    until the next refresh.
    """
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list) or not jwks["keys"]:
        raise ValueError("Janua's JWKS response holds no keys")
    return jwks


def _cached_jwks_if_fresh() -> dict | None:
    if not _jwks_cache or _jwks_fetched_at is None:
        return None
    if (time.monotonic() - _jwks_fetched_at) >= _JWKS_TTL_S:
        return None
    return _jwks_cache


async def _fetch_jwks(*, force: bool = False) -> dict:
    """The cached JWKS, re-read when forced, missing, or older than the TTL.

    One download at a time, bounded by `_JWKS_DOWNLOAD_DEADLINE_S`:
      - an expired cache is refreshed by ONE request; the others keep using
        the cached keys meanwhile instead of waiting for it;
      - with nothing cached, requests wait for the one download in flight.

    A failed download never turns into a download per request:
      - a TTL refresh that fails keeps serving the cached set (logged), so a
        Janua blip does not log everyone out, and is tried again only after
        `_JWKS_RETRY_AFTER_FAILURE_S`;
      - with nothing cached, the failure is remembered for
        `_JWKS_COLD_RETRY_AFTER_S` and callers get `JWKSUnavailableError`
        at once instead of each waiting out a timeout.
    A response that is not a key set with at least one key is a failure.
    A forced fetch (key rotation, already rate limited by its callers) that
    fails raises `JWKSUnavailableError` and leaves the cache as it was.
    """
    global _jwks_cache, _jwks_fetched_at, _jwks_failed_at
    lock = _download_lock()
    if not force:
        cached = _cached_jwks_if_fresh()
        if cached is not None:
            return cached
        if _jwks_cache and lock.locked():
            # Expired, and another request is already refreshing it.
            return _jwks_cache
    async with lock:
        if not force:
            # Another request may have refreshed, or failed, while this one waited.
            cached = _cached_jwks_if_fresh()
            if cached is not None:
                return cached
            if (
                not _jwks_cache
                and _jwks_failed_at is not None
                and (time.monotonic() - _jwks_failed_at) < _JWKS_COLD_RETRY_AFTER_S
            ):
                raise JWKSUnavailableError("Janua's JWKS could not be fetched; retrying shortly")
        try:
            jwks = _usable_jwks(
                await asyncio.wait_for(_download_jwks(), timeout=_JWKS_DOWNLOAD_DEADLINE_S)
            )
        except (httpx.HTTPError, ValueError, TimeoutError) as exc:
            now = time.monotonic()
            if _jwks_cache and not force:
                # Keep the cached keys and look again after the retry
                # interval, not on the next request.
                _jwks_fetched_at = now - _JWKS_TTL_S + _JWKS_RETRY_AFTER_FAILURE_S
                logger.warning("JWKS TTL refresh failed; keeping the cached key set", exc_info=True)
                return _jwks_cache
            if not _jwks_cache:
                _jwks_failed_at = now
            raise JWKSUnavailableError("Janua's JWKS could not be fetched") from exc
        _jwks_cache = jwks
        _jwks_fetched_at = time.monotonic()
        _jwks_failed_at = None
        logger.debug("JWKS fetched from Janua: %d keys", len(jwks["keys"]))
        return jwks


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


def _header_kid(token: str) -> Any:
    """The token's `kid`, or None when it has none (absent or empty)."""
    kid = jwt.get_unverified_header(token).get("kid")
    return None if kid is None or kid == "" else kid


def _is_signing_key(jwk: Any) -> bool:
    """A published key that may verify an RS256 signature: RSA, not published
    for another use (`use` absent or `sig`) or another algorithm (`alg` absent
    or RS256)."""
    return (
        isinstance(jwk, dict)
        and jwk.get("kty") == "RSA"
        and jwk.get("use") in (None, "sig")
        and jwk.get("alg") in (None, *_JANUA_ALGORITHMS)
    )


def _published_signing_keys(jwks: dict) -> list[jwt.PyJWK]:
    """Every RS256 signing key in the JWKS, for a token that names no `kid`.

    Janua's first-party session tokens carry no `kid` (observed 2026-11-14).

    Raises:
        InvalidKeyError: when the JWKS publishes no usable RS256 signing key.
    """
    keys: list[jwt.PyJWK] = []
    for jwk in jwks.get("keys", []):
        if not _is_signing_key(jwk):
            continue
        try:
            keys.append(jwt.PyJWK(jwk, algorithm=_JANUA_ALGORITHMS[0]))
        except PyJWTError:
            continue
    if not keys:
        raise InvalidKeyError("No RS256 signing key published for a token without 'kid'")
    return keys


def _signing_key(jwks: dict, token: str, kid: Any) -> jwt.PyJWK:
    """Select the JWKS key named by the token's `kid` and bind it to RS256.

    Raises:
        PyJWTError: on an unknown `kid` (`_UnknownKeyIdError`) or a JWK that
            cannot verify RS256.
    """
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


def _decode(token: str, key: jwt.PyJWK) -> dict[str, Any]:
    payload: dict[str, Any] = jwt.decode(
        token,
        key,
        algorithms=_JANUA_ALGORITHMS,
        issuer=_JANUA_ISSUER,
        leeway=_JANUA_LEEWAY_SECONDS,
        options={"require": _JANUA_REQUIRED_CLAIMS, "verify_aud": False},
    )
    return payload


def _decode_with_any(token: str, keys: list[jwt.PyJWK]) -> dict[str, Any]:
    """Decode with the first key whose signature verifies.

    Only a signature mismatch moves on to the next key. Any other failure
    (algorithm not allowed, malformed token, or a claim check after the
    signature verified) is final.
    """
    for key in keys:
        try:
            return _decode(token, key)
        except InvalidSignatureError:
            continue
    raise InvalidSignatureError("No published signing key verifies this token")


async def _verify_token(token: str) -> tuple[dict[str, Any], bool]:
    """Verify a Janua RS256 JWT. Raises PyJWTError.

    Returns the claims and whether the token was accepted as a first-party
    session token (see `_check_audience`).

    A token naming a `kid` is checked against that key only, with one
    rate-limited refetch when the `kid` is unknown (key rotation). A token
    without a `kid` is checked against every published RS256 signing key;
    when none verifies, the JWKS is refetched once under the same rate limit
    (Janua rotated its key) and the token is tried once more.
    """
    jwks = await _fetch_jwks()
    kid = _header_kid(token)
    if kid is None:
        try:
            payload = _decode_with_any(token, _published_signing_keys(jwks))
        except InvalidSignatureError:
            if not _claim_forced_refresh():
                raise
            logger.info("No cached key verifies a kid-less token; refetching once (key rotation)")
            refreshed = await _fetch_jwks(force=True)
            payload = _decode_with_any(token, _published_signing_keys(refreshed))
    else:
        try:
            key = _signing_key(jwks, token, kid)
        except _UnknownKeyIdError:
            if not _claim_forced_refresh():
                raise
            logger.info("Token kid not in cached JWKS; refetching once (key rotation)")
            key = _signing_key(await _fetch_jwks(force=True), token, kid)
        payload = _decode(token, key)
    first_party_session = _check_audience(payload)
    return payload, first_party_session


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


def _check_audience(payload: dict[str, Any]) -> bool:
    """Enforce `type`, the audience allowlist and the shared-audience rules.

    Returns True when the token was accepted as a first-party session token
    (shared audience, no `client_id`): the only kind whose `roles` may grant
    `eido:admin`. A token accepted on an operator-configured audience returns
    False whatever it carries.

    Raises:
        InvalidTokenError: with the reason, which is logged, never returned.
    """
    audiences = set(_token_audiences(payload))
    own = audiences & (_ALLOWED_AUDIENCES - _SHARED_SESSION_AUDIENCES)
    shared = audiences & _ALLOWED_AUDIENCES & _SHARED_SESSION_AUDIENCES
    if not own and not shared:
        raise InvalidTokenError(f"Token audience {sorted(audiences)!r} is not accepted")

    # Both paths: an access token only. Refresh tokens carry `type: refresh`;
    # Janua's ID tokens carry no `type` at all.
    token_type = payload.get("type")
    if token_type != _TOKEN_TYPE_ACCESS:
        raise InvalidTokenError(f"Token type {token_type!r} is not 'access'")

    if own:
        # Operator-configured audience: accepted as issued, never admin.
        return False

    # Accepted only because of the shared audience: first-party session only.
    if _is_machine_token(payload):
        raise InvalidTokenError("Machine token on the shared audience; it must name eido's audience")
    if payload.get("is_service_account"):
        raise InvalidTokenError("Service principal token on the shared audience")
    if payload.get("client_id") or payload.get("scope"):
        raise InvalidTokenError(
            "Token issued to an OAuth client on the shared audience; it must name eido's audience"
        )
    return True


class JanuaUser(BaseModel):
    id: str
    org_id: str | None = None
    email: str | None = None
    username: str | None = None
    roles: list[str] = []
    tier: str = "free"   # Populated from Dhanam entitlement claim if present
    # Set by the verifier, never from a claim: True only when the token was
    # accepted as a first-party Janua session token (shared audience, no
    # `client_id`). Only such a token's `roles` are application-role grants.
    first_party_session: bool = False

    @property
    def is_admin(self) -> bool:
        """`eido:admin` on a first-party session token, and nothing else.

        A bare `admin` grants nothing; `eido:admin` on a token issued to an
        OAuth client grants nothing either (organization admins name those
        roles freely).
        """
        return self.first_party_session and ADMIN_ROLE in self.roles


def _str_claim(payload: dict[str, Any], name: str) -> str | None:
    value = payload.get(name)
    return value if isinstance(value, str) else None


def _roles_claim(payload: dict[str, Any]) -> list[str]:
    """`roles` as a list of strings; anything else (null, a string, mixed) is empty."""
    roles = payload.get("roles")
    if isinstance(roles, list) and all(isinstance(r, str) for r in roles):
        return list(roles)
    return []


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
        # Key selected by `kid` (or every RS256 signing key when there is no
        # `kid`), RS256 only, `exp` required, 30 s leeway,
        # `iss` and `aud` checked (see the module docstring).
        payload, first_party_session = await _verify_token(token)
    except PyJWTError as exc:
        logger.warning("JWT verification failed: %s", exc)
        raise credentials_exception from exc
    except JWKSUnavailableError as exc:
        # Not the caller's fault and not a verdict on the token.
        logger.error("JWT verification unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sign-in verification is temporarily unavailable. Try again shortly.",
            headers={"Retry-After": str(int(_JWKS_COLD_RETRY_AFTER_S))},
        ) from exc

    sub = payload.get("sub")
    if not sub or not isinstance(sub, str):
        logger.warning("JWT verification failed: missing or non-string 'sub'")
        raise credentials_exception

    return JanuaUser(
        id=sub,
        org_id=_str_claim(payload, "org_id"),
        email=_str_claim(payload, "email"),
        username=_str_claim(payload, "preferred_username"),
        roles=_roles_claim(payload),
        # Only eido's own tier claim. Janua's bare `tier` is a summary across
        # every product (paid for anything reads as "pro"), so it never
        # unlocks an eido feature.
        tier=_str_claim(payload, "eido_tier") or "free",
        first_party_session=first_party_session,
    )


# Optional — non-blocking auth (for public routes that optionally identify user)
async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(
        HTTPBearer(auto_error=False)
    ),
) -> JanuaUser | None:
    """The caller when a valid token is present, else None (anonymous).

    Any failure to verify degrades to anonymous, a JWKS fetch error included,
    so a public page never turns into a 500. A present but invalid token is
    never treated as authenticated: it gets exactly what an anonymous caller
    gets.
    """
    if not credentials:
        return None
    try:
        return await get_current_user(credentials)
    except HTTPException:
        return None
    except Exception:
        logger.warning("Optional auth could not verify the token; treating as anonymous",
                       exc_info=True)
        return None
