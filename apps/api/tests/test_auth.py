"""Janua JWT verification in ``eido_api.auth`` (PyJWT), without network.

Real RS256 tokens are signed with throwaway keys and verified against a JWKS
built from their public halves. Only the JWKS HTTP fetch is stubbed, so these
tests exercise the PyJWT checks as ``get_current_user`` configures them: the
key named by ``kid``, the fixed ``["RS256"]`` allow-list, required ``exp``, a
30 s leeway, and one rate-limited JWKS refetch on an unknown ``kid``.
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
        "iss": "https://issuer.example.com",
        "aud": "example-api",
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


def _alg_none_token() -> str:
    return _signing_input({"alg": "none", "typ": "JWT", "kid": "key-a"}) + "."


def _hs256_with_public_key_token() -> str:
    """Algorithm confusion: HS256 keyed with the published RSA public key (built by hand)."""
    secret = _KEY_A.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    signing_input = _signing_input({"alg": "HS256", "typ": "JWT", "kid": "key-a"})
    signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


@pytest.fixture(autouse=True)
def _reset_jwks_state():
    auth._jwks_cache = None
    auth._jwks_forced_refresh_time = None
    yield
    auth._jwks_cache = None
    auth._jwks_forced_refresh_time = None


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


def test_audience_and_issuer_are_not_enforced():
    """Unchanged from python-jose: no audience or issuer is configured for eido."""
    token = _token(aud="some-other-api", iss="https://other-issuer.example.com")
    assert _current_user(token).id == "user-1"


# -- key selection ---------------------------------------------------------------


def test_wrong_kid_rejected():
    _assert_rejected(_token(kid="key-unknown"))


def test_missing_kid_rejected():
    _assert_rejected(_token(kid=None))


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


def test_optional_user_is_none_for_invalid_token():
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=_alg_none_token())
    with patch.object(auth, "_fetch_jwks", AsyncMock(return_value=_JWKS)):
        assert asyncio.run(auth.get_optional_user(creds)) is None
