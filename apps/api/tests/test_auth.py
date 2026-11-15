"""Janua JWT verification in ``eido_api.auth`` (PyJWT), without network.

Real RS256 tokens are signed with throwaway keys and verified against a JWKS
built from their public halves. Only the JWKS HTTP fetch is stubbed, so these
tests exercise the PyJWT checks as ``get_current_user`` configures them: the
key named by ``kid``, the fixed ``["RS256"]`` allow-list, required ``exp``, a
30 s leeway, one rate-limited JWKS refetch on an unknown ``kid``, the issuer,
the audience allowlist (default ``eido-api,janua.dev``) and the first-party
session rules for the shared ``janua.dev`` audience.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jwt.algorithms import RSAAlgorithm

from eido_api import auth

_KEY_A = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KEY_B = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KEY_UNPUBLISHED = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_EC_KEY = ec.generate_private_key(ec.SECP256R1())


def _jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return jwk


_JWKS = {"keys": [_jwk(_KEY_A, "key-a")]}


def _claims(**overrides: Any) -> dict[str, Any]:
    """Janua-shaped claims; an override of ``None`` drops the claim."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "user-1",
        "email": "user@example.com",
        "org_id": "org-1",
        "roles": ["member"],
        "iss": "https://auth.madfam.io",
        # A first-party Janua session token: the shared audience, an access
        # token, no client_id, no scope.
        "aud": "janua.dev",
        "type": "access",
        "iat": now,
        "exp": now + 900,
    }
    for name, value in overrides.items():
        if value is None:
            claims.pop(name, None)
        else:
            claims[name] = value
    return claims


def _token(private_key: Any = _KEY_A, kid: str | None = "key-a", algorithm: str = "RS256", **overrides: Any) -> str:
    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(_claims(**overrides), private_key, algorithm=algorithm, headers=headers)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _signing_input(header: dict[str, Any]) -> str:
    return f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(_claims()).encode())}"


def _header(alg: str, kid: str | None) -> dict[str, Any]:
    header: dict[str, Any] = {"alg": alg, "typ": "JWT"}
    if kid is not None:
        header["kid"] = kid
    return header


def _alg_none_token(kid: str | None = "key-a") -> str:
    return _signing_input(_header("none", kid)) + "."


def _hs256_with_public_key_token(kid: str | None = "key-a") -> str:
    """Algorithm confusion: HS256 keyed with the published RSA public key (built by hand)."""
    secret = _KEY_A.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    signing_input = _signing_input(_header("HS256", kid))
    signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


@pytest.fixture(autouse=True)
def _reset_jwks_state():
    auth._jwks_cache = None
    auth._jwks_fetched_at = None
    auth._jwks_forced_refresh_time = None
    auth._jwks_failed_at = None
    auth._jwks_lock = None
    yield
    auth._jwks_cache = None
    auth._jwks_fetched_at = None
    auth._jwks_forced_refresh_time = None
    auth._jwks_failed_at = None
    auth._jwks_lock = None


@pytest.fixture
def eido_api_audience(monkeypatch):
    """An operator has reserved `eido-api` in Janua and added it to JANUA_AUDIENCES."""
    monkeypatch.setattr(auth, "_ALLOWED_AUDIENCES", frozenset({"janua.dev", "eido-api"}))


def _current_user(token: str, fetch: AsyncMock | None = None) -> auth.JanuaUser:
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    with patch.object(auth, "_fetch_jwks", fetch or AsyncMock(return_value=_JWKS)):
        return asyncio.run(auth.get_current_user(creds))


def _assert_rejected(token: str, fetch: AsyncMock | None = None) -> None:
    with pytest.raises(HTTPException) as exc_info:
        _current_user(token, fetch)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or expired token."
    assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}


# -- valid tokens --------------------------------------------------------------


def test_valid_token_returns_user():
    user = _current_user(_token(eido_tier="pro"))
    assert user.id == "user-1"
    assert user.org_id == "org-1"
    assert user.email == "user@example.com"
    assert user.roles == ["member"]
    assert user.tier == "pro"


def test_expired_within_leeway_is_accepted():
    now = int(time.time())
    assert _current_user(_token(iat=now - 900, exp=now - 10)).id == "user-1"


def test_defaults_are_the_production_issuer_and_audiences():
    """`eido-api` is not reserved in Janua, so it is not accepted by default."""
    assert auth._JANUA_ISSUER == "https://auth.madfam.io"
    assert auth._ALLOWED_AUDIENCES == frozenset({"janua.dev"})


def test_session_token_on_shared_audience_is_accepted():
    """Janua's own sign-in: `janua.dev`, `type: access`, no client_id, no scope."""
    user = _current_user(_token())
    assert user.id == "user-1"
    assert user.first_party_session is True


def test_eido_audience_rejected_by_default():
    _assert_rejected(_token(aud="eido-api", client_id="eido-web"))


# -- an operator-configured audience (own-audience path) -------------------------


def test_eido_audience_is_accepted_as_issued_when_configured(eido_api_audience):
    """A token for a configured audience may come from an OAuth client or a machine."""
    user = _current_user(_token(aud="eido-api", client_id="eido-web", scope="openid profile"))
    assert user.id == "user-1"
    assert user.first_party_session is False


def test_eido_audience_machine_token_is_accepted_when_configured(eido_api_audience):
    token = _token(
        aud="eido-api",
        sub="service-account:eido-worker",
        token_use="client_credentials",
        actor_type="service_account",
        client_id="eido-worker",
        scope="captures:write",
    )
    assert _current_user(token).id == "service-account:eido-worker"


def test_eido_audience_in_a_list_is_accepted_when_configured(eido_api_audience):
    assert _current_user(_token(aud=["other-api", "eido-api"])).id == "user-1"


def test_eido_audience_refresh_token_rejected(eido_api_audience):
    _assert_rejected(_token(aud="eido-api", type="refresh"))


def test_eido_audience_id_token_without_type_rejected(eido_api_audience):
    """Janua's ID tokens carry no `type`; both paths require `type: access`."""
    _assert_rejected(_token(aud="eido-api", type=None, client_id="eido-web"))


def test_both_audiences_with_client_id_rejected_by_default():
    """`aud` lists both, `client_id` present: with only `janua.dev` accepted it is a client token."""
    _assert_rejected(_token(aud=["janua.dev", "eido-api"], client_id="other-app"))


def test_both_audiences_with_client_id_is_never_admin(eido_api_audience):
    """Configured: accepted on the own-audience path, which never grants eido:admin."""
    user = _current_user(
        _token(aud=["janua.dev", "eido-api"], client_id="other-app", roles=["eido:admin"])
    )
    assert user.first_party_session is False
    assert user.is_admin is False


# -- issuer and audience --------------------------------------------------------


def test_wrong_issuer_rejected():
    _assert_rejected(_token(iss="https://other-issuer.example.com"))


def test_issuer_with_trailing_slash_rejected():
    _assert_rejected(_token(iss="https://auth.madfam.io/"))


def test_missing_issuer_rejected():
    _assert_rejected(_token(iss=None))


def test_audience_not_in_allowlist_rejected():
    _assert_rejected(_token(aud="some-other-api"))


def test_missing_audience_rejected():
    _assert_rejected(_token(aud=None))


def test_shared_audience_with_client_id_rejected():
    """Issued to another OAuth client that has no audience of its own."""
    _assert_rejected(_token(client_id="other-app"))


def test_shared_audience_with_scope_rejected():
    _assert_rejected(_token(scope="openid profile"))


def test_shared_audience_refresh_token_rejected():
    _assert_rejected(_token(type="refresh"))


def test_shared_audience_without_type_rejected():
    """An ID-token shape (no `type`) is refused."""
    _assert_rejected(_token(type=None))


def test_shared_audience_machine_token_rejected():
    _assert_rejected(
        _token(sub="service-account:other-app", token_use="client_credentials")
    )


def test_shared_audience_service_account_subject_alone_rejected():
    _assert_rejected(_token(sub="service-account:other-app"))


def test_shared_audience_service_principal_rejected():
    _assert_rejected(_token(is_service_account=True))


@pytest.mark.parametrize("raw", ["", " , ", "eido-api,*"])
def test_audience_allowlist_rejects_empty_and_wildcard(raw: str):
    with pytest.raises(RuntimeError):
        auth.parse_audiences(raw)


def test_audience_allowlist_parses_comma_separated_values():
    assert auth.parse_audiences(" eido-api , janua.dev ,") == frozenset({"eido-api", "janua.dev"})


# -- roles -----------------------------------------------------------------------


def test_bare_admin_role_is_not_eido_admin():
    assert _current_user(_token(roles=["admin", "owner"])).is_admin is False


def test_application_admin_role_on_a_session_token_is_eido_admin():
    assert _current_user(_token(roles=["member", "eido:admin"])).is_admin is True


def test_other_products_admin_role_is_not_eido_admin():
    assert _current_user(_token(roles=["pravara-mes:admin"])).is_admin is False


def test_oauth_client_token_with_eido_admin_role_is_not_admin(eido_api_audience):
    """OAuth-path `roles` carry organization role names, which org admins choose freely."""
    user = _current_user(_token(aud="eido-api", client_id="some-app", roles=["eido:admin"]))
    assert user.is_admin is False


def test_first_party_session_cannot_be_set_by_a_claim(eido_api_audience):
    token = _token(aud="eido-api", roles=["eido:admin"], first_party_session=True)
    assert _current_user(token).is_admin is False


@pytest.mark.parametrize("roles", [None, "eido:admin", ["eido:admin", 7], {"eido:admin": True}])
def test_malformed_roles_are_treated_as_empty(roles: Any):
    claims_roles = {"roles": roles} if roles is not None else {}
    token = jwt.encode(
        {**_claims(roles=None), **claims_roles}, _KEY_A, algorithm="RS256", headers={"kid": "key-a"}
    )
    user = _current_user(token)
    assert user.roles == []
    assert user.is_admin is False


def test_roles_explicit_null_is_treated_as_empty():
    token = jwt.encode(
        {**_claims(), "roles": None}, _KEY_A, algorithm="RS256", headers={"kid": "key-a"}
    )
    assert _current_user(token).roles == []


def test_non_string_profile_claims_do_not_fail():
    token = _token(email=["x"], org_id=5, preferred_username={"a": 1}, eido_tier=3)
    user = _current_user(token)
    assert (user.email, user.org_id, user.username, user.tier) == (None, None, None, "free")


# -- key selection ---------------------------------------------------------------


def test_wrong_kid_rejected():
    _assert_rejected(_token(kid="key-unknown"))


# -- tokens without a `kid` (Janua's first-party session tokens carry none) ------


def test_kidless_session_token_signed_by_published_key_is_accepted():
    token = _token(kid=None, aud="janua.dev", type="access")
    assert _current_user(token).id == "user-1"


def test_kidless_eido_audience_token_signed_by_published_key_is_accepted(eido_api_audience):
    assert _current_user(_token(kid=None, aud="eido-api", client_id="eido-web")).id == "user-1"


def test_empty_kid_is_treated_as_no_kid():
    assert _current_user(_token(kid="", aud="janua.dev", type="access")).id == "user-1"


def test_kidless_token_signed_by_unpublished_key_rejected():
    _assert_rejected(_token(private_key=_KEY_UNPUBLISHED, kid=None))


def test_kidless_token_still_held_to_the_shared_audience_rules():
    _assert_rejected(_token(kid=None, aud="janua.dev", type="access", client_id="other-app"))


@pytest.mark.parametrize("override", [{"use": "enc"}, {"alg": "RS512"}])
def test_kidless_token_not_verified_against_non_signing_keys(override: dict[str, str]):
    key = {**_jwk(_KEY_A, "key-a"), **override}
    fetch = AsyncMock(return_value={"keys": [key]})
    _assert_rejected(_token(kid=None), fetch)


def test_kidless_token_verifies_against_the_second_signing_key():
    jwks = {"keys": [_jwk(_KEY_A, "key-a"), _jwk(_KEY_B, "key-b")]}
    fetch = AsyncMock(return_value=jwks)
    assert _current_user(_token(private_key=_KEY_B, kid=None), fetch).id == "user-1"


def test_kidless_token_skips_unusable_keys_and_verifies_with_a_signing_key():
    enc = {**_jwk(_KEY_B, "key-enc"), "use": "enc"}
    fetch = AsyncMock(return_value={"keys": [enc, _jwk(_KEY_A, "key-a")]})
    assert _current_user(_token(kid=None), fetch).id == "user-1"


def test_kidless_junk_forces_at_most_one_refetch_per_minute():
    fetch = AsyncMock(return_value=_JWKS)
    for _ in range(3):
        _assert_rejected(_token(private_key=_KEY_UNPUBLISHED, kid=None), fetch)
    forced = [c for c in fetch.await_args_list if c.kwargs == {"force": True}]
    assert len(forced) == 1


def test_kidless_valid_token_does_not_refetch():
    fetch = AsyncMock(return_value=_JWKS)
    _current_user(_token(kid=None), fetch)
    assert fetch.await_count == 1
    assert auth._jwks_forced_refresh_time is None


def test_kidless_token_after_key_rotation_refetches_once_then_verifies():
    """Old key cached; the token is signed by the new key: one refetch, then accepted."""
    rotated = {"keys": [_jwk(_KEY_B, "key-b")]}
    fetch = AsyncMock(side_effect=[_JWKS, rotated])
    assert _current_user(_token(private_key=_KEY_B, kid=None), fetch).id == "user-1"
    assert [c.kwargs for c in fetch.await_args_list] == [{}, {"force": True}]


def test_kidless_junk_within_a_minute_of_a_rotation_refetch_does_not_refetch():
    rotated = {"keys": [_jwk(_KEY_B, "key-b")]}
    fetch = AsyncMock(side_effect=[_JWKS, rotated, rotated])
    _current_user(_token(private_key=_KEY_B, kid=None), fetch)
    _assert_rejected(_token(private_key=_KEY_UNPUBLISHED, kid=None), fetch)
    forced = [c for c in fetch.await_args_list if c.kwargs == {"force": True}]
    assert len(forced) == 1


def test_kidless_and_named_kid_share_one_refetch_rate_limit():
    fetch = AsyncMock(return_value=_JWKS)
    _assert_rejected(_token(private_key=_KEY_UNPUBLISHED, kid=None), fetch)
    _assert_rejected(_token(kid="key-forged"), fetch)
    forced = [c for c in fetch.await_args_list if c.kwargs == {"force": True}]
    assert len(forced) == 1


# -- JWKS cache TTL ----------------------------------------------------------------


def test_jwks_cache_is_reused_within_the_ttl():
    download = AsyncMock(return_value=_JWKS)
    with patch.object(auth, "_download_jwks", download):
        asyncio.run(auth._fetch_jwks())
        asyncio.run(auth._fetch_jwks())
    assert download.await_count == 1


def test_jwks_cache_is_reread_after_the_ttl():
    rotated = {"keys": [_jwk(_KEY_B, "key-b")]}
    download = AsyncMock(side_effect=[_JWKS, rotated])
    with patch.object(auth, "_download_jwks", download):
        asyncio.run(auth._fetch_jwks())
        auth._jwks_fetched_at -= auth._JWKS_TTL_S + 1
        assert asyncio.run(auth._fetch_jwks()) is rotated
    assert download.await_count == 2


def test_failed_ttl_refresh_keeps_the_cached_keys():
    import httpx

    download = AsyncMock(side_effect=[_JWKS, httpx.ConnectError("down")])
    with patch.object(auth, "_download_jwks", download):
        asyncio.run(auth._fetch_jwks())
        auth._jwks_fetched_at -= auth._JWKS_TTL_S + 1
        assert asyncio.run(auth._fetch_jwks()) is _JWKS


def test_first_fetch_failure_raises():
    import httpx

    with patch.object(auth, "_download_jwks", AsyncMock(side_effect=httpx.ConnectError("down"))):
        with pytest.raises(auth.JWKSUnavailableError):
            asyncio.run(auth._fetch_jwks())


def test_failed_ttl_refresh_is_not_retried_on_every_request():
    """While Janua is down past the TTL, the cached keys are served and the
    download is attempted once per retry interval, not once per request."""
    import httpx

    download = AsyncMock(side_effect=[_JWKS, httpx.ConnectError("down"), httpx.ConnectError("down")])
    with patch.object(auth, "_download_jwks", download):
        asyncio.run(auth._fetch_jwks())
        auth._jwks_fetched_at -= auth._JWKS_TTL_S + 1
        for _ in range(5):
            assert asyncio.run(auth._fetch_jwks()) is _JWKS
        assert download.await_count == 2
        # After the retry interval it looks again (and keeps the cache on failure).
        auth._jwks_fetched_at -= auth._JWKS_RETRY_AFTER_FAILURE_S + 1
        assert asyncio.run(auth._fetch_jwks()) is _JWKS
        assert download.await_count == 3


def test_cold_cache_failure_is_remembered_instead_of_refetched_per_request():
    import httpx

    download = AsyncMock(side_effect=[httpx.ConnectError("down"), _JWKS])
    with patch.object(auth, "_download_jwks", download):
        for _ in range(4):
            with pytest.raises(auth.JWKSUnavailableError):
                asyncio.run(auth._fetch_jwks())
        assert download.await_count == 1
        auth._jwks_failed_at -= auth._JWKS_COLD_RETRY_AFTER_S + 1
        assert asyncio.run(auth._fetch_jwks()) is _JWKS
        assert download.await_count == 2


def test_a_failed_first_download_blocks_sign_in_only_briefly():
    """One blip at pod start must not answer 503 for a minute."""
    assert auth._JWKS_COLD_RETRY_AFTER_S <= 5


def _burst(download, n: int = 20):
    async def run():
        return await asyncio.gather(*(auth._fetch_jwks() for _ in range(n)))

    with patch.object(auth, "_download_jwks", download):
        return asyncio.run(run())


def test_concurrent_requests_on_a_cold_cache_share_one_download():
    calls = 0

    async def slow_download():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return _JWKS

    results = _burst(slow_download)
    assert calls == 1
    assert all(r is _JWKS for r in results)


def test_requests_at_ttl_expiry_use_the_cached_keys_while_one_refreshes():
    """An expired cache is refreshed by one request; the others do not wait."""
    rotated = {"keys": [dict(_JWKS["keys"][0], kid="key-new")]}
    calls = 0

    async def slow_download():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return rotated

    with patch.object(auth, "_download_jwks", AsyncMock(return_value=_JWKS)):
        asyncio.run(auth._fetch_jwks())
    auth._jwks_fetched_at -= auth._JWKS_TTL_S + 1
    results = _burst(slow_download)
    assert calls == 1
    assert sum(r is rotated for r in results) == 1
    assert sum(r is _JWKS for r in results) == 19
    assert auth._jwks_cache is rotated


@pytest.mark.parametrize("body", [{}, [], {"keys": []}, {"keys": "x"}, None])
def test_a_response_without_keys_is_a_failed_download(body):
    with patch.object(auth, "_download_jwks", AsyncMock(return_value=body)):
        with pytest.raises(auth.JWKSUnavailableError):
            asyncio.run(auth._fetch_jwks())
    assert auth._jwks_cache is None


def test_a_response_without_keys_does_not_replace_the_cached_keys():
    download = AsyncMock(side_effect=[_JWKS, {"keys": []}])
    with patch.object(auth, "_download_jwks", download):
        asyncio.run(auth._fetch_jwks())
        auth._jwks_fetched_at -= auth._JWKS_TTL_S + 1
        assert asyncio.run(auth._fetch_jwks()) is _JWKS
    assert auth._jwks_cache is _JWKS


def test_a_download_that_never_finishes_is_cut_off():
    async def hang():
        await asyncio.sleep(30)

    with (
        patch.object(auth, "_download_jwks", hang),
        patch.object(auth, "_JWKS_DOWNLOAD_DEADLINE_S", 0.05),
    ):
        with pytest.raises(auth.JWKSUnavailableError):
            asyncio.run(auth._fetch_jwks())


def test_a_failed_forced_refetch_is_503_once_and_leaves_valid_tokens_working():
    """Janua is down and someone sends a kid-less junk token: that request
    gets 503, the cached keys stay, and a valid token still verifies."""
    import httpx

    download = AsyncMock(side_effect=[_JWKS, httpx.ConnectError("down")])
    junk = HTTPAuthorizationCredentials(scheme="Bearer", credentials=_token(_KEY_B, kid=None))
    good = HTTPAuthorizationCredentials(scheme="Bearer", credentials=_token())
    with patch.object(auth, "_download_jwks", download):
        assert asyncio.run(auth.get_current_user(good)).id
        with pytest.raises(HTTPException) as first:
            asyncio.run(auth.get_current_user(junk))
        with pytest.raises(HTTPException) as second:
            asyncio.run(auth.get_current_user(junk))
        assert asyncio.run(auth.get_current_user(good)).id
    assert first.value.status_code == 503
    assert second.value.status_code == 401  # rate limited: no second refetch
    assert download.await_count == 2
    assert auth._jwks_cache is _JWKS


def test_required_auth_answers_503_when_the_jwks_cannot_be_fetched():
    """A valid token is not called invalid because Janua is unreachable."""
    import httpx

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=_token())
    with patch.object(auth, "_download_jwks", AsyncMock(side_effect=httpx.ConnectError("down"))):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(auth.get_current_user(creds))
    assert exc.value.status_code == 503
    assert exc.value.headers["Retry-After"] == str(int(auth._JWKS_COLD_RETRY_AFTER_S))


def test_bare_tier_claim_never_sets_the_eido_tier():
    """Janua's `tier` summarises every product; only `eido_tier` counts here."""
    assert _current_user(_token(tier="pro")).tier == "free"
    assert _current_user(_token(tier="pro", eido_tier="studio")).tier == "studio"


def test_kidless_alg_none_rejected():
    _assert_rejected(_alg_none_token(kid=None))


def test_kidless_hs256_signed_with_public_key_rejected():
    _assert_rejected(_hs256_with_public_key_token(kid=None))


def test_forged_signature_rejected():
    _assert_rejected(_token(private_key=_KEY_UNPUBLISHED))


def test_non_rsa_jwk_rejected():
    ec_jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(_EC_KEY.public_key()))
    ec_jwk.update({"kid": "key-ec", "use": "sig"})
    fetch = AsyncMock(return_value={"keys": [ec_jwk]})
    _assert_rejected(_token(kid="key-ec"), fetch)


def test_malformed_token_rejected():
    _assert_rejected("not-a-jwt")


# -- algorithm allow-list --------------------------------------------------------


def test_alg_none_rejected():
    _assert_rejected(_alg_none_token())


def test_hs256_signed_with_public_key_rejected():
    _assert_rejected(_hs256_with_public_key_token())


def test_rs512_rejected():
    _assert_rejected(_token(algorithm="RS512"))


def test_es256_rejected():
    _assert_rejected(_token(private_key=_EC_KEY, algorithm="ES256"))


# -- claims ----------------------------------------------------------------------


def test_expired_rejected():
    now = int(time.time())
    _assert_rejected(_token(iat=now - 3600, exp=now - 600))


def test_missing_exp_rejected():
    _assert_rejected(_token(exp=None))


def test_not_yet_valid_rejected():
    _assert_rejected(_token(nbf=int(time.time()) + 600))


def test_missing_sub_rejected():
    _assert_rejected(_token(sub=None))


# -- unknown kid: one rate-limited refetch ---------------------------------------


def test_unknown_kid_refetches_once_then_rejects():
    fetch = AsyncMock(return_value=_JWKS)
    _assert_rejected(_token(kid="key-forged"), fetch)
    assert fetch.await_count == 2
    assert fetch.await_args_list[0].kwargs == {}
    assert fetch.await_args_list[1].kwargs == {"force": True}


def test_rotated_key_verifies_after_refetch():
    fetch = AsyncMock(side_effect=[_JWKS, {"keys": [_jwk(_KEY_B, "key-b")]}])
    assert _current_user(_token(private_key=_KEY_B, kid="key-b"), fetch).id == "user-1"
    assert fetch.await_count == 2


def test_known_kid_does_not_refetch():
    fetch = AsyncMock(return_value=_JWKS)
    _current_user(_token(), fetch)
    assert fetch.await_count == 1


def test_refetch_is_rate_limited():
    fetch = AsyncMock(return_value=_JWKS)
    for kid in ("forged-1", "forged-2", "forged-3"):
        _assert_rejected(_token(kid=kid), fetch)
    forced = [c for c in fetch.await_args_list if c.kwargs == {"force": True}]
    assert len(forced) == 1


def test_invalid_token_does_not_drop_the_jwks_cache():
    """python-jose's version cleared the cache on every failure, so each bad token cost a fetch."""
    auth._jwks_cache = _JWKS
    with pytest.raises(HTTPException):
        asyncio.run(
            auth.get_current_user(
                HTTPAuthorizationCredentials(scheme="Bearer", credentials=_token(exp=None))
            )
        )
    assert auth._jwks_cache is _JWKS


# -- optional auth ---------------------------------------------------------------


def test_optional_user_is_none_when_the_jwks_cannot_be_fetched():
    import httpx

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=_token())
    download = AsyncMock(side_effect=httpx.ConnectError("down"))
    with patch.object(auth, "_download_jwks", download):
        # The real path: the download fails, required auth would answer 503,
        # optional auth is anonymous; the failure is remembered, not refetched.
        for _ in range(3):
            assert asyncio.run(auth.get_optional_user(creds)) is None
    assert download.await_count == 1


def test_optional_user_is_none_for_invalid_token():
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=_alg_none_token())
    with patch.object(auth, "_fetch_jwks", AsyncMock(return_value=_JWKS)):
        assert asyncio.run(auth.get_optional_user(creds)) is None
