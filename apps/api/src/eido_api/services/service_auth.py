"""Janua M2M service identity (OAuth2 client-credentials).

Eido authenticates its own outbound calls to Janua-protected sibling services
with a client-credentials access token — Factlas requires a valid Janua bearer
on ``POST /api/v1/observations`` (writes are gated by ``require_user``), and a
bare unauthenticated handoff 401s.

The token endpoint is resolved from Janua's OIDC discovery document
(``/.well-known/openid-configuration``) so we never hardcode the path; set
``JANUA_TOKEN_URL`` to override. Tokens are cached until shortly before expiry.

Graceful degradation: with no ``JANUA_CLIENT_ID`` / ``JANUA_CLIENT_SECRET``
configured, :func:`get_service_token` returns ``None`` and callers fall back to
posting unauthenticated (the previous behaviour), so this never hard-fails a
handoff on a misconfigured deployment.
"""
import logging
import time

import httpx

from eido_api.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_TIMEOUT = httpx.Timeout(15.0)
_EARLY_REFRESH_S = 60.0

# Process-local cache. Keyed globals are fine — Settings is a process singleton.
_state: dict = {"token": None, "expires_at": 0.0, "token_endpoint": None}


async def _resolve_token_endpoint(client: httpx.AsyncClient) -> str:
    if settings.janua_token_url:
        return settings.janua_token_url
    if _state["token_endpoint"]:
        return _state["token_endpoint"]
    discovery = f"{settings.janua_url.rstrip('/')}/.well-known/openid-configuration"
    resp = await client.get(discovery)
    resp.raise_for_status()
    endpoint = resp.json()["token_endpoint"]
    _state["token_endpoint"] = endpoint
    return endpoint


async def get_service_token() -> str | None:
    """Return a cached Janua service token, refreshing near expiry.

    Returns ``None`` when no client credentials are configured.
    """
    if not (settings.janua_client_id and settings.janua_client_secret):
        return None

    now = time.monotonic()
    if _state["token"] and now < _state["expires_at"]:
        return _state["token"]

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        token_endpoint = await _resolve_token_endpoint(client)
        resp = await client.post(
            token_endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.janua_client_id,
                "client_secret": settings.janua_client_secret,
            },
        )
        resp.raise_for_status()
        body = resp.json()

    _state["token"] = body["access_token"]
    ttl = float(body.get("expires_in", 3600))
    _state["expires_at"] = now + max(0.0, ttl - _EARLY_REFRESH_S)
    return _state["token"]


async def auth_headers() -> dict[str, str]:
    """``Authorization`` header for an authenticated handoff, or ``{}``.

    Never raises: a token-fetch failure logs and returns ``{}`` so the caller
    still attempts the handoff (and surfaces the downstream 401 in its own log)
    rather than crashing the dispatch.
    """
    try:
        token = await get_service_token()
    except Exception as exc:  # noqa: BLE001 - degrade, don't crash the dispatcher
        logger.warning("Janua service-token fetch failed: %s", exc)
        return {}
    return {"Authorization": f"Bearer {token}"} if token else {}
