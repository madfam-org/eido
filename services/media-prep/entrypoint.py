"""
Media-prep — pipeline stage 0 (CPU only).

Turns whatever the user actually uploaded into the two things the rest of the
pipeline assumes it already has:

  1. **A directory of images.** eido.cam's upload page advertises ".zip / video",
     but every downstream stage globs image extensions off the raw S3 prefix, so
     a `.mp4` and a `.zip` both yielded zero images and the run died at
     "Need at least 3 images". This stage extracts archives and decodes video to
     frames.
  2. **Georeference.** Nothing in Eido read GPS EXIF, DJI SRT telemetry, or any
     other position source, so `is_georeferenced` could only ever be set from
     coordinates typed into the ingest call — and the upload page never sent
     any. The Factlas handoff is gated on that flag, so it had never fired.

Outputs to OUTPUT_DIR:
  - `images/`   — normalized frames, also uploaded to the frames S3 prefix
  - `geo.json`  — anchor point, coverage-envelope polygon, per-image priors
  - `geo_ref.txt` — COLMAP `model_aligner --ref_images_path` input, written
    only when at least three frames carry a fix (COLMAP's minimum for a
    similarity alignment)

Exit codes: 0 success, 1 unusable input. A capture with no georeference is NOT
an error — it is an ordinary non-georeferenced capture, and geo.json records
that explicitly rather than leaving the question open.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import boto3
from geo import (
    GeoPrior,
    anchor_from_priors,
    footprint_polygon,
    geo_prior_from_exif,
    hull_area_m2,
    parse_dji_srt,
)
from PIL import ExifTags, Image

S3_ENDPOINT = os.environ["S3_ENDPOINT"]
S3_BUCKET = os.environ["S3_BUCKET"]
S3_PREFIX = os.environ["S3_PREFIX"]
S3_FRAMES_PREFIX = os.environ.get("S3_FRAMES_PREFIX", "")
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/work"))
IMAGE_DIR = OUTPUT_DIR / "images"
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/tmp/media-prep"))

#: Seconds between decoded video frames. ~2 fps gives SfM the overlap it needs
#: without flooding it: a 3-minute orbit yields ~360 frames, which COLMAP's
#: sequential matcher handles comfortably.
FRAME_INTERVAL_S = float(os.environ.get("FRAME_INTERVAL_S", "0.5"))
MAX_FRAMES = int(os.environ.get("MAX_FRAMES", "600"))
MIN_IMAGES = int(os.environ.get("MIN_IMAGES", "3"))

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".mts", ".insv"}
ARCHIVE_EXTS = {".zip"}


def _log(msg: str) -> None:
    print(f"[media-prep] {msg}", flush=True)


def _s3():
    return boto3.client("s3", endpoint_url=S3_ENDPOINT, region_name="us-east-1")


# ── Fetch ──────────────────────────────────────────────────────────────────────

def download_raw() -> list[Path]:
    """Pull every uploaded object under the raw prefix, whatever its type."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    s3 = _s3()
    found: list[Path] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            dest = DOWNLOAD_DIR / Path(key).name
            s3.download_file(S3_BUCKET, key, str(dest))
            found.append(dest)
            _log(f"downloaded {dest.name} ({obj.get('Size', 0)} bytes)")
    return found


# ── Normalize ──────────────────────────────────────────────────────────────────

def expand_archives(paths: list[Path]) -> list[Path]:
    """Extract zips in place, returning the flattened file list."""
    out: list[Path] = []
    for path in paths:
        if path.suffix.lower() not in ARCHIVE_EXTS:
            out.append(path)
            continue
        target = DOWNLOAD_DIR / f"{path.stem}__extracted"
        target.mkdir(parents=True, exist_ok=True)
        _log(f"extracting archive {path.name}")
        try:
            with zipfile.ZipFile(path) as zf:
                for member in zf.infolist():
                    if member.is_dir():
                        continue
                    name = Path(member.filename).name
                    # Flatten, and never honour a path from the archive —
                    # a "../" member would otherwise write outside the target.
                    if not name or name.startswith("."):
                        continue
                    with zf.open(member) as src, open(target / name, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    out.append(target / name)
        except zipfile.BadZipFile:
            _log(f"WARNING: {path.name} is not a readable zip — skipping")
    return out


def decode_video(path: Path, dest_dir: Path, start_index: int) -> list[Path]:
    """Decode a video to stills with ffmpeg at FRAME_INTERVAL_S."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(dest_dir / f"{path.stem}_%05d.jpg")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-vf", f"fps=1/{FRAME_INTERVAL_S}",
        "-frames:v", str(MAX_FRAMES),
        "-q:v", "2",
        "-start_number", str(start_index),
        pattern,
    ]
    _log(f"decoding {path.name} at 1 frame / {FRAME_INTERVAL_S}s")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        _log(f"WARNING: ffmpeg failed on {path.name}: {result.stderr[:300]}")
        return []
    frames = sorted(dest_dir.glob(f"{path.stem}_*.jpg"))
    _log(f"decoded {len(frames)} frames from {path.name}")
    return frames


def normalize(paths: list[Path]) -> tuple[list[Path], list[GeoPrior]]:
    """Produce the image set, plus any priors that only the source media had.

    Video telemetry is the case that needs care: the GPS lives in a sibling
    `.SRT`, not in the decoded frames, so it has to be read here and mapped onto
    frames by index — after decoding there is nothing left to read it from.
    """
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    images: list[Path] = []
    video_priors: list[GeoPrior] = []

    srt_by_stem = {p.stem: p for p in paths if p.suffix.lower() == ".srt"}

    for path in paths:
        ext = path.suffix.lower()
        if ext in IMAGE_EXTS:
            dest = IMAGE_DIR / path.name
            shutil.copy2(path, dest)
            images.append(dest)
        elif ext in VIDEO_EXTS:
            frames = decode_video(path, IMAGE_DIR, start_index=len(images) + 1)
            images.extend(frames)
            srt = srt_by_stem.get(path.stem)
            if srt and frames:
                try:
                    priors = parse_dji_srt(srt.read_text(errors="replace"))
                except OSError as exc:
                    _log(f"WARNING: could not read {srt.name}: {exc}")
                    priors = []
                if priors:
                    video_priors.extend(_map_srt_to_frames(priors, frames))
                    _log(f"mapped {len(priors)} SRT fixes onto {len(frames)} frames")

    return images, video_priors


def _map_srt_to_frames(priors: list[GeoPrior], frames: list[Path]) -> list[GeoPrior]:
    """Distribute SRT fixes across decoded frames proportionally.

    SRT records are per-video-frame while we decode at an interval, so the two
    sequences have different lengths. Both cover the same wall-clock span, so
    index-proportional sampling assigns each decoded frame the nearest fix in
    time. This resamples real fixes — it never interpolates between them.
    """
    if not priors or not frames:
        return []
    mapped: list[GeoPrior] = []
    for i, frame in enumerate(frames):
        idx = min(int(i * len(priors) / len(frames)), len(priors) - 1)
        p = priors[idx]
        mapped.append(GeoPrior(image=frame.name, lat=p.lat, lon=p.lon, alt_m=p.alt_m, source=p.source))
    return mapped


# ── Georeference ───────────────────────────────────────────────────────────────

def _gps_ifd_by_name(path: Path) -> dict[str, Any]:
    """Read an image's GPS IFD, re-keyed from numeric tags to tag names."""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return {}
            gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    except Exception:  # noqa: BLE001 - unreadable/odd EXIF must not kill the run
        return {}
    if not gps:
        return {}
    return {ExifTags.GPSTAGS.get(tag, str(tag)): value for tag, value in gps.items()}


def collect_priors(images: list[Path], video_priors: list[GeoPrior]) -> list[GeoPrior]:
    """EXIF priors from stills, plus SRT priors already mapped to video frames."""
    by_image = {p.image: p for p in video_priors}
    for path in images:
        if path.name in by_image:
            continue  # SRT telemetry already covers this frame
        prior = geo_prior_from_exif(path.name, _gps_ifd_by_name(path))
        if prior:
            by_image[path.name] = prior
    return [by_image[name] for name in sorted(by_image)]


def write_geo_outputs(priors: list[GeoPrior], image_count: int) -> dict[str, Any]:
    """Write geo.json (+ geo_ref.txt when COLMAP can use it); return the summary."""
    anchor = anchor_from_priors(priors)
    footprint = footprint_polygon(priors) if anchor else None

    degenerate = bool(footprint.pop("_degenerate", False)) if footprint else False
    area_m2 = hull_area_m2(footprint["coordinates"][0]) if footprint else 0.0

    geo: dict[str, Any] = {
        "schema": "eido.capture.geo/1",
        "is_georeferenced": anchor is not None,
        "anchor": anchor,
        "footprint": footprint,
        "footprint_kind": "capture_envelope",
        "footprint_area_m2": round(area_m2, 2),
        "footprint_is_buffered": degenerate,
        "image_count": image_count,
        "prior_count": len(priors),
        # A read-proof, per the ecosystem seam rule: a consumer must never be
        # unable to tell "no images had a fix" from "we never looked".
        "read_proof": f"images={image_count} priors={len(priors)}",
        "priors": [
            {"image": p.image, "lat": p.lat, "lon": p.lon, "alt_m": p.alt_m, "source": p.source}
            for p in priors
        ],
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "geo.json").write_text(json.dumps(geo, indent=2))

    # COLMAP model_aligner reference file: "<image_name> <lat> <lon> <alt>".
    # Below three references COLMAP cannot fit a similarity transform, so we
    # skip the file entirely rather than write one the aligner will reject.
    if len(priors) >= 3:
        lines = [f"{p.image} {p.lat:.8f} {p.lon:.8f} {p.alt_m if p.alt_m is not None else 0.0:.3f}"
                 for p in priors]
        (OUTPUT_DIR / "geo_ref.txt").write_text("\n".join(lines) + "\n")
        _log(f"wrote geo_ref.txt with {len(priors)} GPS references for model_aligner")
    else:
        _log(f"only {len(priors)} GPS references — too few to georegister the model (need 3)")

    return geo


# ── Publish ────────────────────────────────────────────────────────────────────

def upload_frames(images: list[Path]) -> None:
    """Mirror normalized frames to S3 so a re-run does not re-decode the video."""
    if not S3_FRAMES_PREFIX:
        return
    s3 = _s3()
    for path in images:
        s3.upload_file(str(path), S3_BUCKET, f"{S3_FRAMES_PREFIX}{path.name}")
    _log(f"uploaded {len(images)} frames to s3://{S3_BUCKET}/{S3_FRAMES_PREFIX}")


def main() -> None:
    _log(f"start — raw s3://{S3_BUCKET}/{S3_PREFIX} → {OUTPUT_DIR}")

    raw = download_raw()
    if not raw:
        _log("ERROR: no objects found under the raw prefix.")
        sys.exit(1)

    expanded = expand_archives(raw)
    images, video_priors = normalize(expanded)

    if len(images) < MIN_IMAGES:
        _log(
            f"ERROR: {len(images)} usable image(s) after normalization; need {MIN_IMAGES}. "
            f"Uploaded: {[p.name for p in raw][:10]}"
        )
        sys.exit(1)

    priors = collect_priors(images, video_priors)
    geo = write_geo_outputs(priors, len(images))
    upload_frames(images)

    if geo["is_georeferenced"]:
        a = geo["anchor"]
        _log(
            f"georeferenced: anchor=({a['lat']:.6f}, {a['lon']:.6f}) "
            f"envelope={geo['footprint_area_m2']}m² from {geo['prior_count']}/{len(images)} frames"
        )
    else:
        _log(f"not georeferenced: no GPS in any of {len(images)} images (this is not an error)")

    _log(f"done — {len(images)} images ready in {IMAGE_DIR}")


if __name__ == "__main__":
    main()
