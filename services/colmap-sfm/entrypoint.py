"""
COLMAP Structure-from-Motion Entrypoint

Downloads raw images from S3, runs COLMAP feature extraction + matching +
sparse reconstruction, and outputs a COLMAP sparse model to the work directory.
"""
import os
import subprocess
import sys
from pathlib import Path

import boto3

S3_ENDPOINT = os.environ["S3_ENDPOINT"]
S3_BUCKET = os.environ["S3_BUCKET"]
S3_PREFIX = os.environ["S3_PREFIX"]
INPUT_DIR = Path(os.environ["INPUT_DIR"])
OUTPUT_DIR = Path(os.environ["OUTPUT_DIR"])


def download_images() -> None:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        region_name="us-east-1",
    )
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            filename = Path(key).name
            if filename.lower().endswith((".jpg", ".jpeg", ".png", ".heic")):
                dest = INPUT_DIR / filename
                s3.download_file(S3_BUCKET, key, str(dest))
                print(f"[SfM] Downloaded: {filename}")

    count = len(list(INPUT_DIR.glob("*")))
    print(f"[SfM] {count} images ready for reconstruction.")
    if count < 3:
        print("[SfM] ERROR: Need at least 3 images for SfM.", file=sys.stderr)
        sys.exit(1)


def run_colmap() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    db_path = OUTPUT_DIR / "database.db"

    stages = [
        (
            "Feature extraction",
            ["colmap", "feature_extractor",
             "--database_path", str(db_path),
             "--image_path", str(INPUT_DIR),
             "--ImageReader.single_camera", "1",
             "--SiftExtraction.use_gpu", "0"],
        ),
        (
            "Sequential matching",
            ["colmap", "sequential_matcher",
             "--database_path", str(db_path)],
        ),
        (
            "Sparse reconstruction",
            ["colmap", "mapper",
             "--database_path", str(db_path),
             "--image_path", str(INPUT_DIR),
             "--output_path", str(OUTPUT_DIR)],
        ),
    ]

    for stage_name, cmd in stages:
        print(f"[SfM] {stage_name}...")
        result = subprocess.run(cmd, capture_output=False)
        if result.returncode != 0:
            print(f"[SfM] FAILED at: {stage_name}", file=sys.stderr)
            sys.exit(result.returncode)

    # Verify output
    model_dirs = [d for d in OUTPUT_DIR.iterdir() if d.is_dir()]
    if not model_dirs:
        print("[SfM] ERROR: COLMAP produced no sparse model.", file=sys.stderr)
        sys.exit(1)

    print(f"[SfM] Reconstruction complete. Models: {[d.name for d in model_dirs]}")


def main() -> None:
    print(f"[SfM] Starting pipeline — Input: {INPUT_DIR}, Output: {OUTPUT_DIR}")
    download_images()
    run_colmap()


if __name__ == "__main__":
    main()
