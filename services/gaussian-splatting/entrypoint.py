"""
3DGS Training Entrypoint

Trains a 3D Gaussian Splat model using gsplat (nerfstudio)
from a COLMAP sparse reconstruction, then exports compressed .spz output.
"""
import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    input_dir = Path(os.environ["INPUT_DIR"])
    output_dir = Path(os.environ["OUTPUT_DIR"])
    iterations = int(os.environ.get("ITERATIONS", "30000"))

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[3DGS] Starting training: {iterations} iterations")
    print(f"[3DGS] Input: {input_dir}  Output: {output_dir}")

    result = subprocess.run(
        [
            "python3", "-m", "gsplat.train",
            "--data_dir", str(input_dir),
            "--output_dir", str(output_dir),
            "--max_steps", str(iterations),
            "--export_ply",
        ],
        capture_output=False,
    )

    if result.returncode != 0:
        print("[3DGS] Training FAILED", file=sys.stderr)
        sys.exit(result.returncode)

    print(f"[3DGS] Training complete. Artifacts in {output_dir}")


if __name__ == "__main__":
    main()
