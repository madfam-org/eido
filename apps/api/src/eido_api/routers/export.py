"""
Export & Embed Router — format downloads and embeddable viewer codes.

GET  /api/v1/export/{capture_id}?format=glb|obj|usdz|ply|spz — signed download URL
GET  /api/v1/export/{capture_id}/embed                        — embed iframe snippet
GET  /embed/{capture_id}                                      — embeddable viewer (no auth)
"""
import html
import json
import logging
from typing import Annotated
from uuid import UUID

import boto3
from botocore.client import Config
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eido_api.config import get_settings
from eido_api.db.session import get_db
from eido_api.models import Capture, CaptureStatus
from eido_api.services.access import Viewer, get_optional_viewer, get_readable_capture

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()

SUPPORTED_FORMATS = {"glb", "obj", "usdz", "ply", "spz"}
# Raw formats need a paid tier. The tier comes from the verified token (Dhanam
# entitlement claim); any value not listed here, Janua's `community` included,
# counts as the lowest tier.
GATED_FORMATS = {"ply", "obj"}
TIERS_ALLOWED_GATED_FORMATS = {"pro", "studio"}

FORMAT_CDN_KEY_MAP = {
    "glb": "output.glb",
    "spz": "output.spz",
    "ply": "point_cloud.ply",
    "obj": "output.obj",
    "usdz": "output.usdz",
}


class DownloadResponse(BaseModel):
    capture_id: str
    format: str
    download_url: str
    expires_in_seconds: int = 3600
    file_size_bytes: int | None = None


class EmbedResponse(BaseModel):
    capture_id: str
    embed_url: str
    iframe_snippet: str
    width: int = 800
    height: int = 600


def _presign_get(key: str, expires: int = 3600) -> str:
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4"),
    )
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket_cdn, "Key": key},
        ExpiresIn=expires,
    )


@router.get("/{capture_id}", response_model=DownloadResponse)
async def get_download_url(
    capture_id: UUID,
    format: str = Query("glb", description="Export format: glb|obj|usdz|ply|spz"),
    db: Annotated[AsyncSession, Depends(get_db)] = None,
    viewer: Viewer | None = Depends(get_optional_viewer),
) -> DownloadResponse:
    """
    Return a pre-signed download URL for the specified format.
    A private capture answers 404 to everyone but its owner and eido:admin.
    .ply and .obj need a signed-in caller on the Pro or Studio tier (Dhanam
    entitlement); other formats of a public capture need no token.
    """
    if format not in SUPPORTED_FORMATS:
        raise HTTPException(status_code=422, detail=f"Unsupported format. Choose from: {SUPPORTED_FORMATS}")

    capture = await get_readable_capture(db, capture_id, viewer)
    if capture.status != CaptureStatus.READY:
        raise HTTPException(status_code=409, detail="Capture is still processing.")

    # Tier gating: raw formats (.ply, .obj) are Pro+ only, and need a signed-in
    # caller. SoC: the entitlement is Dhanam's; eido only reads the claim.
    if format in GATED_FORMATS and viewer is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Raw format export (.ply, .obj) requires signing in with a Pro subscription.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    tier = (viewer.janua.tier or "").strip().lower() if viewer else ""
    if format in GATED_FORMATS and tier not in TIERS_ALLOWED_GATED_FORMATS:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Raw format export (.ply, .obj) requires a Pro subscription. Upgrade at eido.cam/upgrade.",
        )

    cdn_key_file = FORMAT_CDN_KEY_MAP.get(format, f"output.{format}")
    cdn_key = f"captures/{capture_id}/{cdn_key_file}"

    try:
        download_url = _presign_get(cdn_key)
    except Exception as e:
        logger.error("Failed to generate download URL: %s", e)
        raise HTTPException(status_code=503, detail="Storage service unavailable.") from e

    return DownloadResponse(
        capture_id=str(capture_id),
        format=format,
        download_url=download_url,
    )


@router.get("/{capture_id}/embed", response_model=EmbedResponse)
async def get_embed_code(
    capture_id: UUID,
    width: int = Query(800, ge=320, le=1920),
    height: int = Query(600, ge=240, le=1080),
    db: Annotated[AsyncSession, Depends(get_db)] = None,
) -> EmbedResponse:
    """Generate an embeddable iframe snippet for a public capture."""
    result = await db.execute(select(Capture).where(Capture.id == capture_id, Capture.is_public == True))  # noqa
    capture = result.scalar_one_or_none()
    if not capture:
        raise HTTPException(status_code=404, detail="Capture not found or not public.")

    embed_url = f"https://eido.cam/embed/{capture_id}"
    # The snippet is pasted into other people's pages: every interpolated value
    # is escaped for an HTML attribute.
    iframe_snippet = (
        f'<iframe src="{html.escape(embed_url, quote=True)}" width="{width}" height="{height}" '
        f'frameborder="0" allow="xr-spatial-tracking" allowfullscreen '
        f'title="{html.escape(capture.title or "", quote=True)}"></iframe>'
    )

    return EmbedResponse(
        capture_id=str(capture_id),
        embed_url=embed_url,
        iframe_snippet=iframe_snippet,
        width=width,
        height=height,
    )


@router.get("/view/{capture_id}", response_class=HTMLResponse, include_in_schema=False)
async def embeddable_viewer(
    capture_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
) -> HTMLResponse:
    """
    Minimal standalone HTML page for embedding in iframes.
    Loads the splat viewer via a tiny self-contained script — no Next.js needed.
    """
    result = await db.execute(select(Capture).where(Capture.id == capture_id, Capture.is_public == True))  # noqa
    capture = result.scalar_one_or_none()
    if not capture:
        return HTMLResponse("<h1>Capture not found or not public.</h1>", status_code=404)

    # Served on the web origin through the /api rewrite, where the pasted Janua
    # token lives in sessionStorage and the CSP allows inline script, so every
    # interpolated value is escaped for its context: HTML text and attributes
    # with html.escape, the script string as JSON with "<" escaped so it cannot
    # close the <script> element.
    title_html = html.escape(capture.title or "", quote=True)
    capture_path = html.escape(str(capture_id), quote=True)
    splat_url_js = json.dumps(capture.splat_url or capture.mesh_url or "").replace("<", "\\u003c")
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title_html} — Eido</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ background: #0a0a0f; overflow: hidden; }}
    canvas {{ display: block; width: 100vw; height: 100vh; }}
    #eido-badge {{
      position: fixed; bottom: 12px; right: 12px; z-index: 10;
      font: 11px/1.4 system-ui; color: #94a3b8;
      background: rgba(0,0,0,0.5); padding: 4px 8px; border-radius: 6px;
      text-decoration: none;
    }}
  </style>
</head>
<body>
  <canvas id="c"></canvas>
  <a href="https://eido.cam/capture/{capture_path}" target="_blank" rel="noopener" id="eido-badge">
    👁 {title_html} — Eido
  </a>
  <script type="module">
    import * as GaussianSplats3D from 'https://cdn.jsdelivr.net/npm/@mkkellogg/gaussian-splats-3d@latest/+esm';
    const viewer = new GaussianSplats3D.Viewer({{
      cameraUp: [0, -1, 0],
      initialCameraPosition: [0, 0, 3],
      initialCameraLookAt: [0, 0, 0],
    }});
    viewer.addSplatScene({splat_url_js}, {{
      progressiveLoad: true,
    }}).then(() => viewer.start());
  </script>
</body>
</html>"""

    return HTMLResponse(content=page)
