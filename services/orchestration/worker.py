"""
GPU Orchestration Worker — 3DGS Pipeline Dispatcher

Reads jobs from the Redis queue and orchestrates the full pipeline:
  1. colmap-sfm       — Structure-from-Motion alignment
  2. gaussian-splatting — 3DGS training (30k iterations)
  3. splat-to-mesh    — Poisson surface reconstruction → .glb
  4. compress         — .ply → .spz (90% reduction)
  5. upload           — artifacts → S3 CDN bucket
  6. callback         — PATCH /api/v1/captures/{id}/status
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
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.patch(
            f"{API_URL}/api/v1/captures/{capture_id}/status",
            json=payload,
            headers={"X-Internal-Token": INTERNAL_API_TOKEN},
        )


def _run_container(image: str, env: dict[str, str], volumes: dict[str, str]) -> subprocess.CompletedProcess:
    """Run an ephemeral Docker container for a pipeline stage."""
    cmd = ["docker", "run", "--rm", "--gpus", "all"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    for host, container in volumes.items():
        cmd += ["-v", f"{host}:{container}"]
    cmd.append(image)
    logger.info("Running container: %s", " ".join(cmd[:6]) + " ...")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=3600)


async def _run_pipeline(job: dict[str, Any]) -> PipelineResult:
    """Execute the full 3DGS pipeline for a capture job."""
    import time
    start = time.time()
    capture_id = job["capture_id"]
    raw_prefix = job.get("s3_raw_key", f"raw/{capture_id}/")
    work_dir = f"/tmp/eido/{capture_id}"
    os.makedirs(work_dir, exist_ok=True)

    # Stage 1: SfM alignment
    await _update_capture_status(capture_id, "processing_sfm")
    sfm_result = _run_container(
        image="eido/colmap-sfm:latest",
        env={"S3_ENDPOINT": S3_ENDPOINT, "S3_BUCKET": S3_BUCKET_RAW, "S3_PREFIX": raw_prefix, "OUTPUT_DIR": "/work"},
        volumes={work_dir: "/work"},
    )
    if sfm_result.returncode != 0:
        return PipelineResult(error=f"SfM failed: {sfm_result.stderr[:500]}")

    # Stage 2: 3DGS training
    await _update_capture_status(capture_id, "processing_3dgs")
    gs_result = _run_container(
        image="eido/gaussian-splatting:latest",
        env={"INPUT_DIR": "/work/sparse", "OUTPUT_DIR": "/work/splat", "ITERATIONS": "30000"},
        volumes={work_dir: "/work"},
    )
    if gs_result.returncode != 0:
        return PipelineResult(error=f"3DGS failed: {gs_result.stderr[:500]}")

    # Stage 3: Splat-to-mesh conversion
    await _update_capture_status(capture_id, "processing_mesh")
    mesh_result = _run_container(
        image="eido/splat-to-mesh:latest",
        env={"INPUT_PLY": "/work/splat/point_cloud.ply", "OUTPUT_GLB": "/work/mesh/output.glb"},
        volumes={work_dir: "/work"},
    )
    if mesh_result.returncode != 0:
        return PipelineResult(error=f"Mesh conversion failed: {mesh_result.stderr[:500]}")

    # Stage 4: Upload artifacts to S3 CDN
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
        processing_time_s=round(elapsed, 1),
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
