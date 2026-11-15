"""Capture read access over HTTP: public, owner, eido:admin, everyone else.

Drives the real routes through FastAPI's ``TestClient`` with real RS256 Janua
tokens (only the JWKS fetch is stubbed) and an in-memory stand-in for the
database session, so the whole chain runs: bearer extraction, token checks,
Janua → local user lookup, and the rule in ``services/access.py``:

  - a public capture is readable by anyone;
  - a private capture only by its owner or an ``eido:admin``;
  - to everyone else it answers 404, never 403, so existence is not disclosed.
"""

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from eido_api import auth
from eido_api.db.session import get_db
from eido_api.main import app
from eido_api.models import (
    Capture,
    CaptureMode,
    CaptureStatus,
    EcosystemHandoff,
    SpatialAnnotation,
    User,
)

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWK: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(_KEY.public_key()))
_JWK.update({"kid": "key-a", "alg": "RS256", "use": "sig"})
_JWKS = {"keys": [_JWK]}

OWNER = User(id=uuid.uuid4(), janua_id="sub-owner", username="owner", tier="free")
OTHER = User(id=uuid.uuid4(), janua_id="sub-other", username="other", tier="free")
ADMIN = User(id=uuid.uuid4(), janua_id="sub-admin", username="admin", tier="free")
BARE_ADMIN = User(id=uuid.uuid4(), janua_id="sub-bare-admin", username="bare", tier="free")
_USERS = [OWNER, OTHER, ADMIN, BARE_ADMIN]

_ROLES = {
    "sub-owner": ["member"],
    "sub-other": ["member"],
    "sub-admin": ["eido:admin"],
    "sub-bare-admin": ["admin", "owner"],
}


def _bearer(sub: str) -> dict[str, str]:
    now = int(time.time())
    claims = {
        "sub": sub,
        "iss": "https://auth.madfam.io",
        "aud": "janua.dev",
        "type": "access",
        "roles": _ROLES[sub],
        "iat": now,
        "exp": now + 900,
    }
    token = jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": "key-a"})
    return {"Authorization": f"Bearer {token}"}


def _capture(*, is_public: bool) -> Capture:
    return Capture(
        id=uuid.uuid4(),
        author_id=OWNER.id,
        title="Courtyard",
        mode=CaptureMode.GAUSSIAN_SPLATTING,
        status=CaptureStatus.READY,
        splat_url="https://cdn.eido.cam/captures/x/output.spz",
        mesh_url="https://cdn.eido.cam/captures/x/output.glb",
        is_public=is_public,
        tags=[],
        created_at=datetime.now(UTC),
    )


class _Result:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeSession:
    """Answers the few SELECTs these routes issue, keyed by entity and bound values."""

    def __init__(self, captures: list[Capture]):
        self.captures = captures
        self.added: list[Any] = []

    async def execute(self, stmt: Any) -> _Result:
        entity = stmt.column_descriptions[0]["entity"]
        bound = {str(v) for v in stmt.compile().params.values()}
        if entity is Capture:
            return _Result([c for c in self.captures if str(c.id) in bound])
        if entity is User:
            return _Result([u for u in _USERS if u.janua_id in bound])
        if entity in (SpatialAnnotation, EcosystemHandoff):
            return _Result([])
        raise AssertionError(f"unexpected query for {entity}")

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def refresh(self, obj: Any) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(UTC)


@pytest.fixture
def client_for():
    jwks = patch.object(auth, "_fetch_jwks", AsyncMock(return_value=_JWKS))
    jwks.start()

    def make(capture: Capture) -> TestClient:
        session = _FakeSession([capture])

        async def _db():
            yield session

        app.dependency_overrides[get_db] = _db
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()
    jwks.stop()


# The formerly unguarded reads, plus the export download (which used to answer
# 403 for a private capture, disclosing that it exists).
_READ_ROUTES = [
    "/api/v1/captures/{id}",
    "/api/v1/jobs/{id}",
    "/api/v1/social/captures/{id}/annotations",
    "/api/v1/export/{id}?format=glb",
]


def _get(client: TestClient, route: str, capture: Capture, sub: str | None):
    headers = _bearer(sub) if sub else {}
    if "/export/" in route:
        with patch("eido_api.routers.export._presign_get", return_value="https://signed.example/x"):
            return client.get(route.format(id=capture.id), headers=headers)
    return client.get(route.format(id=capture.id), headers=headers)


@pytest.mark.parametrize("route", _READ_ROUTES)
@pytest.mark.parametrize("sub", [None, "sub-other", "sub-bare-admin"])
def test_private_capture_is_404_to_everyone_else(client_for, route, sub):
    capture = _capture(is_public=False)
    resp = _get(client_for(capture), route, capture, sub)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Capture not found."}


@pytest.mark.parametrize("route", _READ_ROUTES)
@pytest.mark.parametrize("sub", ["sub-owner", "sub-admin"])
def test_private_capture_is_readable_by_owner_and_eido_admin(client_for, route, sub):
    capture = _capture(is_public=False)
    assert _get(client_for(capture), route, capture, sub).status_code == 200


@pytest.mark.parametrize("route", _READ_ROUTES)
@pytest.mark.parametrize("sub", [None, "sub-other", "sub-owner"])
def test_public_capture_is_readable_by_anyone(client_for, route, sub):
    capture = _capture(is_public=True)
    assert _get(client_for(capture), route, capture, sub).status_code == 200


def test_missing_capture_is_404(client_for):
    capture = _capture(is_public=True)
    client = client_for(capture)
    resp = client.get(f"/api/v1/captures/{uuid.uuid4()}", headers=_bearer("sub-admin"))
    assert resp.status_code == 404


# -- annotating requires the same access as reading -------------------------------

_PIN = {"x": 0.1, "y": 0.2, "z": 0.3, "text": "door"}


def _annotate(client: TestClient, capture: Capture, sub: str | None):
    headers = _bearer(sub) if sub else {}
    return client.post(
        f"/api/v1/social/captures/{capture.id}/annotate", json=_PIN, headers=headers
    )


def test_annotate_requires_a_token(client_for):
    capture = _capture(is_public=True)
    assert _annotate(client_for(capture), capture, None).status_code in (401, 403)


@pytest.mark.parametrize("sub", ["sub-other", "sub-bare-admin"])
def test_annotate_private_capture_is_404_to_everyone_else(client_for, sub):
    capture = _capture(is_public=False)
    resp = _annotate(client_for(capture), capture, sub)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Capture not found."}


@pytest.mark.parametrize("sub", ["sub-owner", "sub-admin"])
def test_annotate_private_capture_by_owner_and_eido_admin(client_for, sub):
    capture = _capture(is_public=False)
    resp = _annotate(client_for(capture), capture, sub)
    assert resp.status_code == 201
    assert resp.json()["text"] == "door"


@pytest.mark.parametrize("sub", ["sub-other", "sub-owner"])
def test_annotate_public_capture_by_any_signed_in_user(client_for, sub):
    capture = _capture(is_public=True)
    assert _annotate(client_for(capture), capture, sub).status_code == 201


# -- admin gate: `eido:admin`, never a bare `admin` --------------------------------


def test_bare_admin_does_not_pass_the_handoffs_admin_gate(client_for):
    capture = _capture(is_public=True)
    resp = client_for(capture).get(
        f"/api/v1/handoffs/{capture.id}", headers=_bearer("sub-bare-admin")
    )
    assert resp.status_code == 403


def test_eido_admin_passes_the_handoffs_admin_gate(client_for):
    capture = _capture(is_public=True)
    resp = client_for(capture).get(f"/api/v1/handoffs/{capture.id}", headers=_bearer("sub-admin"))
    assert resp.status_code == 200
    assert resp.json() == []
