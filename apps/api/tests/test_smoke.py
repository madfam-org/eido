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


def test_alembic_env_renders_offline():
    """The container entrypoint runs `alembic upgrade head` before uvicorn,
    so a broken alembic/env.py is a production crash loop (it kept importing
    the tokens router deleted in #3 — 50+ restarts before anyone saw a log).
    Offline SQL rendering exercises env.py and every migration without a DB.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    api_dir = Path(__file__).resolve().parents[1]
    env = {**os.environ, "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:5432/x"}
    res = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        capture_output=True, text=True, env=env, cwd=api_dir, timeout=120,
    )
    assert res.returncode == 0, res.stderr[-2000:]
    assert "CREATE TABLE captures" in res.stdout
