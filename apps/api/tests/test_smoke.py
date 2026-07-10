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


def test_app_imports_and_has_routes():
    """The app module must import — this is exactly what the container runs.

    Catches stale router exports and circular imports that per-module tests
    miss (the tokens-router removal in #3 left routers/__init__ importing a
    deleted module, and nothing failed until the image booted).
    """
    from eido_api.main import app

    # app.openapi() forces route materialization (FastAPI registers included
    # routers lazily) and needs no lifespan, DB, or Redis.
    paths = set(app.openapi()["paths"])
    assert "/health" in paths
    assert any(p.startswith("/api/v1/captures") for p in paths)
