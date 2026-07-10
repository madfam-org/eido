"""Unit tests for Janua M2M service-token auth (no network).

Verifies the graceful-degradation contract: with no client credentials
configured, the service never mints a token and handoffs proceed without an
Authorization header (the pre-M2M behaviour).
"""
import asyncio


def test_no_token_when_unconfigured(monkeypatch):
    from eido_api.config import get_settings
    from eido_api.services import service_auth

    settings = get_settings()
    monkeypatch.setattr(settings, "janua_client_id", "", raising=False)
    monkeypatch.setattr(settings, "janua_client_secret", "", raising=False)

    assert asyncio.run(service_auth.get_service_token()) is None


def test_auth_headers_empty_when_unconfigured(monkeypatch):
    from eido_api.config import get_settings
    from eido_api.services import service_auth

    settings = get_settings()
    monkeypatch.setattr(settings, "janua_client_id", "", raising=False)
    monkeypatch.setattr(settings, "janua_client_secret", "", raising=False)

    assert asyncio.run(service_auth.auth_headers()) == {}


def test_auth_headers_never_raises_on_fetch_error(monkeypatch):
    """A token-fetch failure degrades to {} rather than crashing the dispatcher."""
    from eido_api.services import service_auth

    async def _boom():
        raise RuntimeError("janua unreachable")

    monkeypatch.setattr(service_auth, "get_service_token", _boom)
    assert asyncio.run(service_auth.auth_headers()) == {}
