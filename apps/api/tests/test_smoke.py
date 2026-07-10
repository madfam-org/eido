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
    assert "/readyz" in paths
    assert any(p.startswith("/api/v1/captures") for p in paths)


def test_worker_callback_path_matches_a_mounted_route():
    """The orchestration worker PATCHes the capture-status callback; if its path
    doesn't match a mounted route the callback 404s and strands every capture at
    QUEUED (exactly the bug this guards). The status route lives under the jobs
    router (prefix /api/v1/jobs), so the worker must target /api/v1/jobs/captures.
    """
    from eido_api.main import app

    status_routes = {
        r.path for r in app.routes
        if getattr(r, "path", "").endswith("/captures/{capture_id}/status")
    }
    assert "/api/v1/jobs/captures/{capture_id}/status" in status_routes


def test_stage_map_covers_every_processing_status():
    """Every PROCESSING_* status must have a progress-panel entry, or the UI
    shows 'Unknown' mid-pipeline (the compress stage was missing)."""
    from eido_api.models import CaptureStatus
    from eido_api.routers.jobs import _STATUS_STAGE_MAP

    for st in CaptureStatus:
        assert st in _STATUS_STAGE_MAP, f"{st} missing from _STATUS_STAGE_MAP"


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
