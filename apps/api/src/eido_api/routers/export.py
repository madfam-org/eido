"""
Export & Embed Router — format downloads and embeddable viewer codes.

GET  /api/v1/export/{capture_id}?format=glb|obj|usdz|ply|spz — signed download URL
GET  /api/v1/export/{capture_id}/embed                        — embed iframe snippet
GET  /embed/{capture_id}                                      — embeddable viewer (no auth)
"""
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
from eido_api.models import Capture, CaptureStatus, User
from eido_api.services.provisioning import get_optional_provisioned_user

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()

SUPPORTED_FORMATS = {"glb", "obj", "usdz", "ply", "spz"}
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
    user: User | None = Depends(get_optional_provisioned_user),
) -> DownloadResponse:
    """
    Return a pre-signed download URL for the specified format.
    Private captures require authentication.
    .ply and .obj formats require a Pro or Studio tier (Dhanam entitlement).
    """
    if format not in SUPPORTED_FORMATS:
        raise HTTPException(status_code=422, detail=f"Unsupported format. Choose from: {SUPPORTED_FORMATS}")

    result = await db.execute(select(Capture).where(Capture.id == capture_id))
    capture = result.scalar_one_or_none()
    if not capture:
        raise HTTPException(status_code=404, detail="Capture not found.")
    if not capture.is_public and (not user or str(capture.author_id) != str(user.id)):
        raise HTTPException(status_code=403, detail="Private capture — authentication required.")
    if capture.status != CaptureStatus.READY:
        raise HTTPException(status_code=409, detail="Capture is still processing.")

    # Tier gating: raw formats (.ply, .obj) are Pro+ only
    # SoC: entitlement check is delegated to Dhanam, not owned by Eido
    if format in ("ply", "obj") and user and user.tier == "free":
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
    iframe_snippet = (
        f'<iframe src="{embed_url}" width="{width}" height="{height}" '
        f'frameborder="0" allow="xr-spatial-tracking" allowfullscreen '
        f'title="{capture.title}"></iframe>'
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

    splat_url = capture.splat_url or capture.mesh_url or ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{capture.title} — Eido</title>
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
  <a href="https://eido.cam/capture/{capture_id}" target="_blank" id="eido-badge">
    👁 {capture.title} — Eido
  </a>
  <script type="module">
    import * as GaussianSplats3D from 'https://cdn.jsdelivr.net/npm/@mkkellogg/gaussian-splats-3d@latest/+esm';
    const viewer = new GaussianSplats3D.Viewer({{
      cameraUp: [0, -1, 0],
      initialCameraPosition: [0, 0, 3],
      initialCameraLookAt: [0, 0, 0],
    }});
    viewer.addSplatScene('{splat_url}', {{
      progressiveLoad: true,
    }}).then(() => viewer.start());
  </script>
</body>
</html>"""

    return HTMLResponse(content=html)
