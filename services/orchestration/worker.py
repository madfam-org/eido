"""
GPU Orchestration Worker — 3DGS Pipeline Dispatcher

Reads jobs from the Redis queue and orchestrates the full pipeline:
  0. media-prep       — video/zip → frames, GPS EXIF + DJI SRT → priors (CPU)
  1. colmap-sfm       — Structure-from-Motion + GPS georegistration
  2. gaussian-splatting — 3DGS training (30k iterations)
  3. splat-to-mesh    — Poisson surface reconstruction → .glb
  4. compress         — .ply → .spz (90% reduction)
  5. upload           — artifacts → S3 CDN bucket
  6. callback         — PATCH /api/v1/jobs/captures/{id}/status

Shared-volume layout — every stage mounts the same work dir at /work, so these
paths are a contract *between* stages, not per-stage preferences:

    /work/images/        normalized frames        (media-prep → sfm, 3dgs)
    /work/sparse/0/      COLMAP sparse model      (sfm → 3dgs)
    /work/geo.json       georeference summary     (media-prep → worker)
    /work/geo_ref.txt    model_aligner references (media-prep → sfm)
    /work/splat/         3DGS output              (3dgs → mesh, compress)
    /work/mesh/          .glb                     (mesh → upload)

The worker previously passed OUTPUT_DIR=/work to the SfM stage, overriding that
image's own /work/sparse default. COLMAP then wrote its model to /work/0 while
the 3DGS stage read /work/sparse — so stage 2 could never find stage 1's output.
Nothing overrides a stage's path defaults now; the layout above is the contract.
"""
import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Any

import boto3
import httpx
import redis.asyncio as redis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
JOB_QUEUE_KEY = "eido:jobs:pending"
API_URL = os.getenv("EIDO_API_URL", "http://api:8000")
# Same value as the API's INTERNAL_API_TOKEN; authenticates the status callback.
INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN", "")
S3_ENDPOINT = os.getenv("S3_ENDPOINT", "http://minio:9000")
S3_BUCKET_RAW = os.getenv("S3_BUCKET_RAW", "eido-raw")
S3_BUCKET_CDN = os.getenv("S3_BUCKET_CDN", "eido-cdn")
CDN_BASE_URL = os.getenv("CDN_BASE_URL", "http://localhost:9000/eido-cdn")


@dataclass
class PipelineResult:
    splat_url: str | None = None
    mesh_url: str | None = None
    thumbnail_url: str | None = None
    gaussian_count: int | None = None
    vertex_count: int | None = None
    processing_time_s: float | None = None
    error: str | None = None
    #: Georeference derived by media-prep from the media's own telemetry. None
    #: when the capture carries no GPS — which is an ordinary outcome, not a
    #: failure, and is reported as such rather than left unset.
    geo: dict[str, Any] | None = None


async def _update_capture_status(
    capture_id: str,
    status: str,
    result: PipelineResult | None = None,
) -> None:
    payload: dict[str, Any] = {"status": status}
    if result:
        payload.update({
            "splat_url": result.splat_url,
            "mesh_url": result.mesh_url,
            "thumbnail_url": result.thumbnail_url,
            "gaussian_count": result.gaussian_count,
            "vertex_count": result.vertex_count,
            "processing_time_s": result.processing_time_s,
            "error_message": result.error,
        })
        if result.geo:
            # Telemetry-derived georeference. The capture row's coordinates are
            # set from this, which is the only way is_georeferenced can become
            # true without the operator typing coordinates by hand.
            payload["geo"] = result.geo
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Route is mounted under the jobs router (prefix /api/v1/jobs), so the
        # callback path is /api/v1/jobs/captures/... — NOT /api/v1/captures/...
        # (that 404s and silently stranded every capture at QUEUED).
        resp = await client.patch(
            f"{API_URL}/api/v1/jobs/captures/{capture_id}/status",
            json=payload,
            headers={"X-Internal-Token": INTERNAL_API_TOKEN},
        )
        resp.raise_for_status()


def _run_container(
    image: str, env: dict[str, str], volumes: dict[str, str], gpus: bool = True
) -> subprocess.CompletedProcess:
    """Run an ephemeral Docker container for a pipeline stage."""
    cmd = ["docker", "run", "--rm"]
    if gpus:
        cmd += ["--gpus", "all"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    for host, container in volumes.items():
        cmd += ["-v", f"{host}:{container}"]
    cmd.append(image)
    logger.info("Running container: %s", " ".join(cmd[:6]) + " ...")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=3600)


def _read_geo(work_dir: str) -> dict[str, Any] | None:
    """Read media-prep's geo.json from the shared work volume.

    Returns None only when the file is absent or unreadable — i.e. when we do
    not know. A capture that ran through media-prep and simply had no GPS still
    returns a dict with ``is_georeferenced: false``, so the API can tell "looked
    and found nothing" apart from "never looked".
    """
    geo_path = os.path.join(work_dir, "geo.json")
    try:
        with open(geo_path) as f:
            geo: dict[str, Any] = json.load(f)
    except (OSError, ValueError) as e:
        logger.warning("No readable geo.json at %s: %s", geo_path, e)
        return None
    logger.info("Georeference: %s", geo.get("read_proof", "unknown"))
    return geo


async def _run_pipeline(job: dict[str, Any]) -> PipelineResult:
    """Execute the full 3DGS pipeline for a capture job."""
    import time
    start = time.time()
    capture_id = job["capture_id"]
    raw_prefix = job.get("s3_raw_key", f"raw/{capture_id}/")
    work_dir = f"/tmp/eido/{capture_id}"
    os.makedirs(work_dir, exist_ok=True)

    # Stage 0: media normalization + georeference extraction (CPU only).
    # Runs before SfM because it is what turns an uploaded video or zip into the
    # images every later stage assumes, and it is the only place the media's own
    # GPS telemetry still exists to be read.
    await _update_capture_status(capture_id, "processing_sfm")
    prep_result = _run_container(
        image="eido/media-prep:latest",
        env={
            "S3_ENDPOINT": S3_ENDPOINT,
            "S3_BUCKET": S3_BUCKET_RAW,
            "S3_PREFIX": raw_prefix,
            "S3_FRAMES_PREFIX": f"frames/{capture_id}/",
            "OUTPUT_DIR": "/work",
        },
        volumes={work_dir: "/work"},
        gpus=False,
    )
    if prep_result.returncode != 0:
        return PipelineResult(error=f"Media prep failed: {prep_result.stderr[:500]}")

    geo = _read_geo(work_dir)

    # Stage 1: SfM alignment (+ georegistration when geo_ref.txt exists).
    # No OUTPUT_DIR override — the image's /work/sparse default is the contract.
    sfm_result = _run_container(
        image="eido/colmap-sfm:latest",
        env={"S3_ENDPOINT": S3_ENDPOINT, "S3_BUCKET": S3_BUCKET_RAW, "S3_PREFIX": raw_prefix},
        volumes={work_dir: "/work"},
    )
    if sfm_result.returncode != 0:
        return PipelineResult(error=f"SfM failed: {sfm_result.stderr[:500]}", geo=geo)

    # Stage 2: 3DGS training. data_dir is the COLMAP *project root* — the
    # directory holding images/ and sparse/ — not the sparse model itself.
    await _update_capture_status(capture_id, "processing_3dgs")
    gs_result = _run_container(
        image="eido/gaussian-splatting:latest",
        env={"INPUT_DIR": "/work", "OUTPUT_DIR": "/work/splat", "ITERATIONS": "30000"},
        volumes={work_dir: "/work"},
    )
    if gs_result.returncode != 0:
        return PipelineResult(error=f"3DGS failed: {gs_result.stderr[:500]}", geo=geo)

    # Stage 3: Splat-to-mesh conversion
    await _update_capture_status(capture_id, "processing_mesh")
    mesh_result = _run_container(
        image="eido/splat-to-mesh:latest",
        env={"INPUT_PLY": "/work/splat/point_cloud.ply", "OUTPUT_GLB": "/work/mesh/output.glb"},
        volumes={work_dir: "/work"},
    )
    if mesh_result.returncode != 0:
        return PipelineResult(error=f"Mesh conversion failed: {mesh_result.stderr[:500]}", geo=geo)

    # Stage 4: .spz compression (point_cloud.ply → output.spz, CPU-only)
    await _update_capture_status(capture_id, "processing_compress")
    compress_result = _run_container(
        image="eido/spz-compress:latest",
        env={
            "INPUT_PLY": "/work/splat/point_cloud.ply",
            "OUTPUT_SPZ": "/work/splat/output.spz",
            "META_JSON": "/work/splat/compress-meta.json",
        },
        volumes={work_dir: "/work"},
        gpus=False,
    )
    if compress_result.returncode != 0:
        return PipelineResult(error=f"SPZ compression failed: {compress_result.stderr[:500]}", geo=geo)

    gaussian_count = None
    try:
        with open(f"{work_dir}/splat/compress-meta.json") as f:
            gaussian_count = json.load(f).get("gaussian_count")
    except (OSError, ValueError):
        logger.warning("compress-meta.json missing for %s; gaussian_count unknown", capture_id)

    # Stage 5: Upload artifacts to S3 CDN
    s3 = boto3.client("s3", endpoint_url=S3_ENDPOINT, region_name="us-east-1")

    def _upload(local: str, key: str) -> str:
        s3.upload_file(local, S3_BUCKET_CDN, key)
        return f"{CDN_BASE_URL}/{key}"

    splat_url = _upload(f"{work_dir}/splat/output.spz", f"captures/{capture_id}/output.spz")
    mesh_url = _upload(f"{work_dir}/mesh/output.glb", f"captures/{capture_id}/output.glb")

    elapsed = time.time() - start
    return PipelineResult(
        splat_url=splat_url,
        mesh_url=mesh_url,
        gaussian_count=gaussian_count,
        processing_time_s=round(elapsed, 1),
        geo=geo,
    )


async def worker_loop() -> None:
    """Main Redis consumer loop."""
    r = redis.from_url(REDIS_URL, decode_responses=True)
    logger.info("Eido orchestration worker started. Listening on queue: %s", JOB_QUEUE_KEY)

    while True:
        try:
            _, raw = await r.brpop(JOB_QUEUE_KEY, timeout=5)
            if raw is None:
                continue

            job = json.loads(raw)
            capture_id = job.get("capture_id", "unknown")
            logger.info("Processing job: %s", capture_id)

            try:
                result = await _run_pipeline(job)
                if result.error:
                    await _update_capture_status(capture_id, "failed", result)
                else:
                    await _update_capture_status(capture_id, "ready", result)
            except Exception as e:
                logger.exception("Pipeline error for capture %s", capture_id)
                await _update_capture_status(capture_id, "failed", PipelineResult(error=str(e)))

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Worker loop error: %s", e)
            await asyncio.sleep(5)

    await r.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(worker_loop())
