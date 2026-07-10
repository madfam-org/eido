"""Smoke tests — verify the API package's config and core enums load.

Deliberately dependency-light (no DB, Redis, network, or crypto imports) so the
CI `test-api` lane always has collectible, passing tests. Deeper unit and
integration tests are tracked separately.
"""


def test_settings_load():
    from eido_api.config import get_settings

    settings = get_settings()
    assert settings.app_version


def test_capture_enums():
    from eido_api.models import CaptureMode, CaptureStatus

    assert CaptureStatus.READY.value == "ready"
    assert CaptureMode.GAUSSIAN_SPLATTING.value == "3dgs"
