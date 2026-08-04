"""
COLMAP Structure-from-Motion Entrypoint

Runs feature extraction, matching and sparse reconstruction over the frames
media-prep normalized into INPUT_DIR, then — when media-prep found GPS priors —
georegisters the model with ``colmap model_aligner``.

Georegistration is what makes the reconstruction *earth-framed* rather than a
shape floating in an arbitrary local frame at an arbitrary scale. Without it a
capture can carry a latitude and longitude and still be un-anchored: the point
is known, the model is not oriented or scaled to it. Eido owns this step because
Eido is the only product holding both the camera poses and the GPS priors.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import boto3

S3_ENDPOINT = os.environ["S3_ENDPOINT"]
S3_BUCKET = os.environ["S3_BUCKET"]
S3_PREFIX = os.environ["S3_PREFIX"]
INPUT_DIR = Path(os.environ["INPUT_DIR"])
OUTPUT_DIR = Path(os.environ["OUTPUT_DIR"])
#: media-prep writes geo_ref.txt beside the images when >=3 frames carry a fix.
GEO_REF = Path(os.environ.get("GEO_REF", "/work/geo_ref.txt"))

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".webp")
MIN_IMAGES = 3


def _count_images(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for p in directory.iterdir() if p.suffix.lower() in IMAGE_EXTS)


def ensure_images() -> int:
    """Use the frames media-prep already normalized; fall back to S3.

    media-prep (stage 0) decodes video, extracts archives and copies stills into
    INPUT_DIR on the shared work volume, so the common path downloads nothing.
    The S3 fallback keeps a direct upload of loose photos working when the
    pipeline is driven without stage 0.
    """
    existing = _count_images(INPUT_DIR)
    if existing >= MIN_IMAGES:
        print(f"[SfM] Using {existing} normalized frames from {INPUT_DIR}")
        return existing

    print(f"[SfM] {INPUT_DIR} has {existing} images — falling back to S3 {S3_PREFIX}")
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    s3 = boto3.client("s3", endpoint_url=S3_ENDPOINT, region_name="us-east-1")
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            filename = Path(key).name
            if filename.lower().endswith(IMAGE_EXTS):
                s3.download_file(S3_BUCKET, key, str(INPUT_DIR / filename))
                print(f"[SfM] Downloaded: {filename}")

    count = _count_images(INPUT_DIR)
    print(f"[SfM] {count} images ready for reconstruction.")
    if count < MIN_IMAGES:
        print(
            f"[SfM] ERROR: need at least {MIN_IMAGES} images for SfM, found {count}. "
            "If the upload was a video or a zip, stage 0 (media-prep) did not run.",
            file=sys.stderr,
        )
        sys.exit(1)
    return count


def _run(stage_name: str, cmd: list[str]) -> None:
    print(f"[SfM] {stage_name}...")
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        print(f"[SfM] FAILED at: {stage_name}", file=sys.stderr)
        sys.exit(result.returncode)


def _sparse_model_dir() -> Path | None:
    """COLMAP writes each reconstruction to <output>/0, <output>/1, ...

    Only numeric subdirectories are models. The previous check accepted *any*
    subdirectory, so an unrelated sibling directory on the shared volume counted
    as a successful reconstruction — a false green.
    """
    if not OUTPUT_DIR.is_dir():
        return None
    models = sorted(
        (d for d in OUTPUT_DIR.iterdir() if d.is_dir() and d.name.isdigit()),
        key=lambda d: int(d.name),
    )
    return models[0] if models else None


def run_colmap() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    db_path = OUTPUT_DIR / "database.db"

    _run("Feature extraction", [
        "colmap", "feature_extractor",
        "--database_path", str(db_path),
        "--image_path", str(INPUT_DIR),
        "--ImageReader.single_camera", "1",
        "--SiftExtraction.use_gpu", "0",
    ])
    _run("Sequential matching", [
        "colmap", "sequential_matcher",
        "--database_path", str(db_path),
    ])
    _run("Sparse reconstruction", [
        "colmap", "mapper",
        "--database_path", str(db_path),
        "--image_path", str(INPUT_DIR),
        "--output_path", str(OUTPUT_DIR),
    ])

    model = _sparse_model_dir()
    if model is None:
        print("[SfM] ERROR: COLMAP produced no sparse model.", file=sys.stderr)
        sys.exit(1)
    print(f"[SfM] Reconstruction complete: {model}")


def georegister() -> None:
    """Align the sparse model to the GPS priors, in place.

    Skipped — not failed — when there are no priors: an un-georeferenced capture
    is an ordinary capture, and the pipeline must still produce a mesh for it.
    A model_aligner failure is likewise non-fatal: the reconstruction is still
    good, it is simply not earth-framed, and the emitted marker says so rather
    than letting a caller assume alignment happened.
    """
    model = _sparse_model_dir()
    if model is None:
        return

    if not GEO_REF.is_file():
        print("[SfM] No GPS references — skipping georegistration (capture stays local-frame)")
        return

    reference_count = sum(1 for line in GEO_REF.read_text().splitlines() if line.strip())
    aligned = OUTPUT_DIR / "aligned"
    aligned.mkdir(parents=True, exist_ok=True)

    print(f"[SfM] Georegistering against {reference_count} GPS references...")
    result = subprocess.run(
        [
            "colmap", "model_aligner",
            "--input_path", str(model),
            "--output_path", str(aligned),
            "--ref_images_path", str(GEO_REF),
            # The reference file holds lat/lon/alt, so COLMAP must convert to a
            # Cartesian frame itself; ENU keeps the result in metres, which is
            # what the mesh and .spz stages downstream assume.
            "--ref_is_gps", "1",
            "--alignment_type", "enu",
            "--robust_alignment", "1",
            "--robust_alignment_max_error", "3.0",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0 or not any(aligned.iterdir()):
        print(
            f"[SfM] WARNING: georegistration failed ({result.stderr[:300]}). "
            "Keeping the unaligned model; the capture is NOT earth-framed.",
            file=sys.stderr,
        )
        shutil.rmtree(aligned, ignore_errors=True)
        return

    # Replace the model in place so every downstream stage consumes the aligned
    # one without needing to know whether alignment happened.
    for item in aligned.iterdir():
        shutil.move(str(item), str(model / item.name))
    shutil.rmtree(aligned, ignore_errors=True)

    (OUTPUT_DIR / "georegistered.marker").write_text(
        f"aligned=enu references={reference_count}\n"
    )
    print(f"[SfM] Model georegistered to ENU metres from {reference_count} references")


def main() -> None:
    print(f"[SfM] Starting pipeline — Input: {INPUT_DIR}, Output: {OUTPUT_DIR}")
    ensure_images()
    run_colmap()
    georegister()


if __name__ == "__main__":
    main()
