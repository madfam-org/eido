"""Capture read access over HTTP: public, owner, eido:admin, everyone else.

Drives the real routes through FastAPI's ``TestClient`` with real RS256 Janua
tokens (only the JWKS fetch is stubbed) and an in-memory stand-in for the
database session, so the whole chain runs: bearer extraction, token checks,
Janua → local user lookup, and the rule in ``services/access.py``:

  - a public capture is readable by anyone;
  - a private capture only by its owner or an ``eido:admin``;
  - to everyone else it answers 404, never 403, so existence is not disclosed.
"""

import html
import json
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.dml import Insert

from eido_api import auth
from eido_api.db.session import get_db
from eido_api.main import app
from eido_api.models import (
    Capture,
    CaptureMode,
    CaptureStatus,
    EcosystemHandoff,
    SocialEdge,
    SpatialAnnotation,
    User,
)
from eido_api.routers.collections import Collection, collection_captures

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


def _bearer(sub: str, **extra: Any) -> dict[str, str]:
    now = int(time.time())
    claims = {
        "sub": sub,
        "iss": "https://auth.madfam.io",
        "aud": "janua.dev",
        "type": "access",
        "roles": _ROLES.get(sub, ["member"]),
        "iat": now,
        "exp": now + 900,
        **extra,
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

    def scalar(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


class _Nested:
    """SAVEPOINT stand-in: an exception inside discards what was added inside."""

    def __init__(self, session: "_FakeSession"):
        self.session = session

    async def __aenter__(self) -> None:
        self.mark = len(self.session.added)

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is not None:
            del self.session.added[self.mark:]
        return False


class _FakeSession:
    """Answers the SELECTs these routes issue, keyed by entity and bound values.

    Users are provisioned into ``self.users``; inserting a user whose
    ``janua_id`` or ``username`` is taken raises ``IntegrityError`` like the
    unique constraints do. ``race_winner`` simulates a parallel request that
    commits the same person's row first.
    """

    def __init__(self, captures: list[Capture], collections: list[Any] = (), links: list[Any] = ()):
        self.captures = list(captures)
        self.collections = list(collections)
        self.links = [SimpleNamespace(collection_id=c, capture_id=k) for c, k in links]
        self.users = list(_USERS)
        self.added: list[Any] = []
        self.race_winner: User | None = None

    async def execute(self, stmt: Any) -> _Result:
        if isinstance(stmt, Insert):
            values = stmt.compile().params
            self.links.append(SimpleNamespace(**values))
            return _Result([])
        desc = stmt.column_descriptions[0]
        entity = desc.get("entity")
        bound = {str(v) for v in stmt.compile().params.values()}
        if entity is Capture:
            return _Result([c for c in self.captures if str(c.id) in bound])
        if entity is User:
            return _Result([u for u in self.users if u.janua_id in bound])
        if entity is Collection:
            return _Result([c for c in self.collections if str(c.id) in bound])
        if entity is None and desc["expr"].table is collection_captures:
            return _Result(
                [link for link in self.links
                 if str(link.collection_id) in bound
                 and ("capture_id_1" not in stmt.compile().params or str(link.capture_id) in bound)]
            )
        if entity in (SpatialAnnotation, EcosystemHandoff, SocialEdge):
            return _Result([])
        raise AssertionError(f"unexpected query for {entity or desc}")

    async def scalar(self, stmt: Any) -> int:
        return 0

    def begin_nested(self) -> _Nested:
        return _Nested(self)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def delete(self, obj: Any) -> None:
        return None

    async def flush(self) -> None:
        for obj in self.added:
            if not isinstance(obj, User) or obj in self.users:
                continue
            if self.race_winner is not None:
                self.users.append(self.race_winner)
                self.race_winner = None
                raise IntegrityError("INSERT INTO users", {}, Exception("duplicate key"))
            if any(u.janua_id == obj.janua_id or u.username == obj.username for u in self.users):
                raise IntegrityError("INSERT INTO users", {}, Exception("duplicate key"))
            obj.id = uuid.uuid4()
            self.users.append(obj)

    async def refresh(self, obj: Any) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(UTC)


@pytest.fixture
def client_for():
    jwks = patch.object(auth, "_fetch_jwks", AsyncMock(return_value=_JWKS))
    jwks.start()

    def make(*captures: Capture, collections: list[Any] = (), links: list[Any] = ()) -> TestClient:
        session = _FakeSession(list(captures), list(collections), list(links))

        async def _db():
            yield session

        app.dependency_overrides[get_db] = _db
        client = TestClient(app)
        client.fake = session  # type: ignore[attr-defined]
        return client

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


# -- invalid tokens degrade to anonymous ------------------------------------------


def _expired(sub: str) -> dict[str, str]:
    now = int(time.time())
    return _bearer(sub, iat=now - 3600, exp=now - 600)


@pytest.mark.parametrize("headers", [_expired("sub-owner"), {"Authorization": "Bearer junk"}])
def test_invalid_token_on_a_private_capture_is_404(client_for, headers):
    capture = _capture(is_public=False)
    assert client_for(capture).get(f"/api/v1/captures/{capture.id}", headers=headers).status_code == 404


@pytest.mark.parametrize("headers", [_expired("sub-owner"), {"Authorization": "Bearer junk"}])
def test_invalid_token_on_a_public_capture_is_200(client_for, headers):
    capture = _capture(is_public=True)
    assert client_for(capture).get(f"/api/v1/captures/{capture.id}", headers=headers).status_code == 200


def test_jwks_outage_on_a_public_capture_is_200_not_500(client_for):
    capture = _capture(is_public=True)
    client = client_for(capture)
    import httpx

    with patch.object(auth, "_fetch_jwks", AsyncMock(side_effect=httpx.ConnectError("down"))):
        resp = client.get(f"/api/v1/captures/{capture.id}", headers=_bearer("sub-owner"))
    assert resp.status_code == 200


def test_jwks_outage_on_a_private_capture_is_404(client_for):
    capture = _capture(is_public=False)
    client = client_for(capture)
    import httpx

    with patch.object(auth, "_fetch_jwks", AsyncMock(side_effect=httpx.ConnectError("down"))):
        resp = client.get(f"/api/v1/captures/{capture.id}", headers=_bearer("sub-owner"))
    assert resp.status_code == 404


# -- provisioning -------------------------------------------------------------------


def test_unknown_sub_is_provisioned_with_a_hashed_username(client_for):
    capture = _capture(is_public=True)
    client = client_for(capture)
    resp = client.get(f"/api/v1/captures/{capture.id}", headers=_bearer("sub-new-person"))
    assert resp.status_code == 200
    created = [u for u in client.fake.users if u.janua_id == "sub-new-person"]
    assert len(created) == 1
    assert created[0].username.startswith("user_") and len(created[0].username) == 21


def test_service_account_subjects_get_distinct_usernames():
    from eido_api.services.provisioning import _generated_username

    a = _generated_username("service-account:client-a")
    b = _generated_username("service-account:client-b")
    assert a != b


def test_provisioning_race_reselects_the_winners_row(client_for):
    """A parallel first request inserted the same person first: re-select, no 500."""
    capture = _capture(is_public=False)
    client = client_for(capture)
    winner = User(id=uuid.uuid4(), janua_id="sub-racer", username="racer", tier="free")
    client.fake.race_winner = winner
    resp = client.get(f"/api/v1/captures/{capture.id}", headers=_bearer("sub-racer"))
    assert resp.status_code == 404  # a stranger to this private capture, and no 500
    assert [u for u in client.fake.users if u.janua_id == "sub-racer"] == [winner]


def test_taken_preferred_username_falls_back_to_the_hashed_one(client_for):
    capture = _capture(is_public=True)
    client = client_for(capture)
    headers = _bearer("sub-copycat", preferred_username="owner")  # OWNER holds "owner"
    assert client.get(f"/api/v1/captures/{capture.id}", headers=headers).status_code == 200
    created = [u for u in client.fake.users if u.janua_id == "sub-copycat"]
    assert len(created) == 1 and created[0].username.startswith("user_")


# -- likes, comments, handoffs, process, publish: same 404 first ----------------------

_STRANGER_POSTS = [
    ("post", "/api/v1/social/captures/{id}/like", None),
    ("post", "/api/v1/social/captures/{id}/comments", {"text": "hi"}),
    ("get", "/api/v1/social/captures/{id}/comments", None),
    ("get", "/api/v1/handoffs/{id}", None),
    ("post", "/api/v1/handoffs/{id}/retry", {"target": "factlas"}),
    ("post", "/api/v1/captures/{id}/process", None),
    ("post", "/api/v1/captures/{id}/publish", None),
]


@pytest.mark.parametrize("method,route,body", _STRANGER_POSTS)
def test_private_capture_is_404_to_a_stranger_everywhere(client_for, method, route, body):
    capture = _capture(is_public=False)
    client = client_for(capture)
    kwargs: dict[str, Any] = {"headers": _bearer("sub-other")}
    if body is not None:
        kwargs["json"] = body
    resp = getattr(client, method)(route.format(id=capture.id), **kwargs)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Capture not found."}


@pytest.mark.parametrize("method,route", [
    ("get", "/api/v1/social/captures/{id}/comments"),
    ("post", "/api/v1/social/captures/{id}/like"),
])
def test_likes_and_comments_work_on_a_readable_capture(client_for, method, route):
    capture = _capture(is_public=True)
    resp = getattr(client_for(capture), method)(
        route.format(id=capture.id), headers=_bearer("sub-other")
    )
    assert resp.status_code == 200


def test_comments_on_a_private_capture_are_404_to_anonymous(client_for):
    capture = _capture(is_public=False)
    resp = client_for(capture).get(f"/api/v1/social/captures/{capture.id}/comments")
    assert resp.status_code == 404


@pytest.mark.parametrize("route", ["/api/v1/captures/{id}/process", "/api/v1/captures/{id}/publish"])
def test_owner_only_actions_are_403_to_a_reader_who_is_not_the_owner(client_for, route):
    capture = _capture(is_public=True)
    resp = client_for(capture).post(route.format(id=capture.id), headers=_bearer("sub-other"))
    assert resp.status_code == 403


def test_handoffs_list_is_403_to_a_reader_who_is_not_owner_or_admin(client_for):
    capture = _capture(is_public=True)
    resp = client_for(capture).get(f"/api/v1/handoffs/{capture.id}", headers=_bearer("sub-other"))
    assert resp.status_code == 403


# -- handoffs never send a private capture ------------------------------------------


@pytest.mark.parametrize("sub", ["sub-owner", "sub-admin"])
def test_retry_on_a_private_capture_is_409(client_for, sub):
    capture = _capture(is_public=False)
    resp = client_for(capture).post(
        f"/api/v1/handoffs/{capture.id}/retry", json={"target": "factlas"}, headers=_bearer(sub)
    )
    assert resp.status_code == 409
    assert "private" in resp.json()["detail"]


def test_retry_on_a_public_capture_is_accepted(client_for):
    capture = _capture(is_public=True)
    with patch("eido_api.services.handoff._dispatch_factlas", AsyncMock()) as dispatch:
        resp = client_for(capture).post(
            f"/api/v1/handoffs/{capture.id}/retry", json={"target": "factlas"},
            headers=_bearer("sub-owner"),
        )
    assert resp.status_code == 202
    dispatch.assert_awaited_once()


def _run_dispatch(rows: list[Capture | None]):
    """Run dispatch_all_handoffs against a database that answers `rows` in
    order (the last one repeats), without waiting between reads."""
    import asyncio

    from eido_api.services import handoff

    reads: list[Capture | None] = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, stmt):
            row = rows[min(len(reads), len(rows) - 1)]
            reads.append(row)
            return _Result([row] if row is not None else [])

    with (
        patch.object(handoff, "async_session_maker", lambda: _Session()),
        patch.object(handoff, "_PUBLISHED_READ_DELAY_S", 0),
        patch.object(handoff, "_dispatch_blueprint_harvester", AsyncMock()) as bh,
        patch.object(handoff, "_dispatch_factlas", AsyncMock()) as fa,
        patch.object(handoff, "_dispatch_ceq", AsyncMock()) as ceq,
    ):
        asyncio.run(handoff.dispatch_all_handoffs("capture-id"))
    return reads, (bh, fa, ceq)


def test_dispatch_all_handoffs_refuses_a_private_capture():
    from eido_api.services import handoff

    reads, targets = _run_dispatch([_capture(is_public=False)])
    assert len(reads) == handoff._PUBLISHED_READ_ATTEMPTS
    for target in targets:
        target.assert_not_awaited()


def test_dispatch_waits_for_the_publish_to_commit():
    """The publish route schedules dispatch before its transaction commits on
    current FastAPI: the first reads still see the private row."""
    private = _capture(is_public=False)
    public = _capture(is_public=True)
    reads, targets = _run_dispatch([private, private, public])
    assert len(reads) == 3
    for target in targets:
        target.assert_awaited_once_with(public)


def test_dispatch_refuses_a_public_capture_that_is_not_ready():
    capture = _capture(is_public=True)
    capture.status = CaptureStatus.PROCESSING_3DGS
    _, targets = _run_dispatch([capture])
    for target in targets:
        target.assert_not_awaited()


def test_dispatch_stops_at_once_when_the_capture_does_not_exist():
    reads, targets = _run_dispatch([None])
    assert len(reads) == 1
    for target in targets:
        target.assert_not_awaited()


# -- export: tier gate --------------------------------------------------------------


def _export(client: TestClient, capture: Capture, fmt: str, headers: dict[str, str]):
    with patch("eido_api.routers.export._presign_get", return_value="https://signed.example/x"):
        return client.get(f"/api/v1/export/{capture.id}?format={fmt}", headers=headers)


@pytest.mark.parametrize("fmt", ["glb", "spz", "usdz"])
def test_ungated_formats_of_a_public_capture_need_no_token(client_for, fmt):
    capture = _capture(is_public=True)
    assert _export(client_for(capture), capture, fmt, {}).status_code == 200


@pytest.mark.parametrize("fmt", ["ply", "obj"])
def test_gated_formats_need_a_signed_in_caller(client_for, fmt):
    capture = _capture(is_public=True)
    assert _export(client_for(capture), capture, fmt, {}).status_code == 401


@pytest.mark.parametrize("tier", ["free", "community", "enterprise-ish", None])
def test_gated_formats_refuse_low_and_unknown_tiers(client_for, tier):
    capture = _capture(is_public=True)
    headers = _bearer("sub-other", **({"eido_tier": tier} if tier else {}))
    assert _export(client_for(capture), capture, "ply", headers).status_code == 402


@pytest.mark.parametrize("tier", ["pro", "studio", "Pro"])
def test_gated_formats_allow_paid_tiers(client_for, tier):
    capture = _capture(is_public=True)
    headers = _bearer("sub-other", eido_tier=tier)
    assert _export(client_for(capture), capture, "obj", headers).status_code == 200


# -- export: HTML escaping ------------------------------------------------------------

_HOSTILE_TITLES = ['</title><script>alert(1)</script>', 'say "hi" onload="alert(1)']


@pytest.mark.parametrize("title", _HOSTILE_TITLES)
def test_embed_viewer_escapes_the_title(client_for, title):
    capture = _capture(is_public=True)
    capture.title = title
    body = client_for(capture).get(f"/api/v1/export/view/{capture.id}").text
    assert "<script>alert(1)" not in body
    assert 'onload="alert(1)' not in body
    assert html.escape(title, quote=True) in body


def test_embed_viewer_escapes_the_splat_url_inside_the_script(client_for):
    capture = _capture(is_public=True)
    capture.splat_url = "https://cdn.eido.cam/x');</script><script>alert(1)//"
    body = client_for(capture).get(f"/api/v1/export/view/{capture.id}").text
    assert "</script><script>alert(1)" not in body
    assert body.count("</script>") == 1


@pytest.mark.parametrize("title", _HOSTILE_TITLES)
def test_iframe_snippet_escapes_the_title(client_for, title):
    capture = _capture(is_public=True)
    capture.title = title
    snippet = client_for(capture).get(f"/api/v1/export/{capture.id}/embed").json()["iframe_snippet"]
    assert f'title="{html.escape(title, quote=True)}"' in snippet
    assert "<script>" not in snippet
    assert 'onload="' not in snippet


# -- collections ------------------------------------------------------------------------


def _collection(author_sub: str, *, is_public: bool = True) -> Collection:
    return Collection(
        id=uuid.uuid4(), author_id=author_sub, title="Album", is_public=is_public,
        is_deleted=False, created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )


def _mixed_collection(client_for):
    private, public = _capture(is_public=False), _capture(is_public=True)
    col = _collection("sub-other")
    client = client_for(private, public, collections=[col],
                        links=[(col.id, private.id), (col.id, public.id)])
    return client, col, private, public


@pytest.mark.parametrize("sub", [None, "sub-other", "sub-bare-admin"])
def test_collection_hides_private_captures_and_their_count(client_for, sub):
    client, col, private, public = _mixed_collection(client_for)
    body = client.get(f"/api/v1/collections/{col.id}", headers=_bearer(sub) if sub else {}).json()
    assert [c["id"] for c in body["captures"]] == [str(public.id)]
    assert body["capture_count"] == 1


@pytest.mark.parametrize("sub", ["sub-owner", "sub-admin"])
def test_collection_shows_private_captures_to_owner_and_eido_admin(client_for, sub):
    client, col, private, public = _mixed_collection(client_for)
    body = client.get(f"/api/v1/collections/{col.id}", headers=_bearer(sub)).json()
    assert {c["id"] for c in body["captures"]} == {str(private.id), str(public.id)}
    assert body["capture_count"] == 2


def test_adding_someone_elses_private_capture_to_a_collection_is_404(client_for):
    private = _capture(is_public=False)
    col = _collection("sub-other")
    client = client_for(private, collections=[col])
    resp = client.post(f"/api/v1/collections/{col.id}/add", json={"capture_id": str(private.id)},
                       headers=_bearer("sub-other"))
    assert resp.status_code == 404
    assert client.fake.links == []


def test_adding_a_public_capture_to_a_collection_is_204(client_for):
    public = _capture(is_public=True)
    col = _collection("sub-other")
    client = client_for(public, collections=[col])
    resp = client.post(f"/api/v1/collections/{col.id}/add", json={"capture_id": str(public.id)},
                       headers=_bearer("sub-other"))
    assert resp.status_code == 204
    assert len(client.fake.links) == 1


def test_a_token_name_too_long_for_the_username_column_is_not_used():
    from eido_api.services import provisioning

    assert provisioning._fits("ada") == "ada"
    assert provisioning._fits("x" * provisioning._USERNAME_MAX) is not None
    assert provisioning._fits("x" * (provisioning._USERNAME_MAX + 1)) is None
    assert provisioning._fits(None) is None
    assert provisioning._fits("") is None
