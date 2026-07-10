"""SPZ Compression Entrypoint — pipeline stage 4.

Reads the 3DGS training output (point_cloud.ply), writes the compressed
output.spz the uploader expects, plus a compress-meta.json sidecar the
orchestration worker reads for gaussian_count and size accounting.
"""
import json
import os
import sys
import time
from pathlib import Path

from spz_writer import compress_ply_to_spz


def main() -> None:
    input_ply = Path(os.environ.get("INPUT_PLY", "/work/splat/point_cloud.ply"))
    output_spz = Path(os.environ.get("OUTPUT_SPZ", "/work/splat/output.spz"))
    meta_path = Path(os.environ.get("META_JSON", "/work/splat/compress-meta.json"))

    if not input_ply.exists():
        print(f"[SPZ] Input not found: {input_ply}", file=sys.stderr)
        sys.exit(1)

    start = time.time()
    ply_bytes = input_ply.stat().st_size
    print(f"[SPZ] Compressing {input_ply} ({ply_bytes:,} bytes)")

    output_spz.parent.mkdir(parents=True, exist_ok=True)
    cloud = compress_ply_to_spz(input_ply, output_spz)

    spz_bytes = output_spz.stat().st_size
    elapsed = time.time() - start
    ratio = (1.0 - spz_bytes / ply_bytes) * 100.0 if ply_bytes else 0.0
    meta = {
        "gaussian_count": cloud.num_points,
        "sh_degree": cloud.sh_degree,
        "ply_bytes": ply_bytes,
        "spz_bytes": spz_bytes,
        "reduction_pct": round(ratio, 1),
        "elapsed_s": round(elapsed, 2),
    }
    meta_path.write_text(json.dumps(meta))
    print(f"[SPZ] Wrote {output_spz} ({spz_bytes:,} bytes, {ratio:.1f}% smaller, "
          f"{cloud.num_points:,} gaussians, {elapsed:.2f}s)")


if __name__ == "__main__":
    main()
