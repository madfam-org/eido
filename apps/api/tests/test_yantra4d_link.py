"""RFC 0059 wave 0: eido's Yantra4D address is the real one, and eido sends nothing there.

Yantra4D runs no in-cluster service; its API is https://api.yantra4d.com. eido has
no Yantra4D client until the mount (wave 3), so the one route that names the
target, the handoff retry, must refuse it rather than pretend to send.
"""

from eido_api.config import Settings
from eido_api.models import HandoffTarget

from .test_capture_access import _bearer, _capture, client_for  # noqa: F401  (fixture)


def test_default_yantra4d_address_is_the_public_api(monkeypatch):
    monkeypatch.delenv("YANTRA4D_URL", raising=False)
    assert Settings().yantra4d_url == "https://api.yantra4d.com"


def test_a_yantra4d_handoff_retry_is_refused_and_sends_nothing(client_for):  # noqa: F811
    """The owner of a public capture asks to send it to Yantra4D: 400, nothing sent."""
    from unittest.mock import AsyncMock, patch

    from eido_api.services import handoff

    capture = _capture(is_public=True)
    with (
        patch.object(handoff, "_dispatch_blueprint_harvester", AsyncMock()) as bh,
        patch.object(handoff, "_dispatch_factlas", AsyncMock()) as fa,
        patch.object(handoff, "_dispatch_ceq", AsyncMock()) as ceq,
    ):
        resp = client_for(capture).post(
            f"/api/v1/handoffs/{capture.id}/retry",
            json={"target": HandoffTarget.YANTRA4D.value},
            headers=_bearer("sub-owner"),
        )
    assert resp.status_code == 400
    assert "not supported" in resp.json()["detail"]
    for sent in (bh, fa, ceq):
        sent.assert_not_awaited()
