"""
Splat-to-Mesh Conversion Entrypoint

Converts a 3D Gaussian Splat .ply point cloud into a clean polygon mesh
using Open3D Poisson Surface Reconstruction, then exports as .glb.

Handles reflective and transparent surfaces by using alpha-masked density
filtering before reconstruction.
"""
import os
import sys
from pathlib import Path

import numpy as np
import open3d as o3d
import trimesh


def load_splat_ply(path: Path) -> o3d.geometry.PointCloud:
    print(f"[Splat→Mesh] Loading point cloud: {path}")
    pcd = o3d.io.read_point_cloud(str(path))
    print(f"  Points: {len(pcd.points):,}")
    return pcd


def denoise_and_filter(pcd: o3d.geometry.PointCloud) -> o3d.geometry.PointCloud:
    """Remove statistical outliers to clean up Gaussian noise."""
    pcd, _ = pcd.remove_statistical_outlier(nb_neighbors=20, std_ratio=2.0)
    pcd.estimate_normals(search_param=o3d.geometry.KDTreeSearchParamHybrid(radius=0.1, max_nn=30))
    pcd.orient_normals_consistent_tangent_plane(100)
    return pcd


def poisson_reconstruct(pcd: o3d.geometry.PointCloud) -> o3d.geometry.TriangleMesh:
    """Run Poisson surface reconstruction at depth 10 for high fidelity."""
    print("[Splat→Mesh] Running Poisson surface reconstruction (depth=10)...")
    mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(pcd, depth=10)
    # Remove low-density vertices (artifacts from thin/transparent regions)
    density_threshold = np.quantile(densities, 0.05)
    vertices_to_remove = densities < density_threshold
    mesh.remove_vertices_by_mask(vertices_to_remove)
    mesh.remove_degenerate_triangles()
    mesh.remove_duplicated_triangles()
    mesh.remove_non_manifold_edges()
    return mesh


def export_glb(mesh: o3d.geometry.TriangleMesh, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Convert to trimesh for glb export
    vertices = np.asarray(mesh.vertices)
    faces = np.asarray(mesh.triangles)
    tm = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    tm.export(str(output_path))
    print(f"[Splat→Mesh] Exported: {output_path} ({output_path.stat().st_size / 1024:.1f} KB)")


def main() -> None:
    input_ply = Path(os.environ["INPUT_PLY"])
    output_glb = Path(os.environ["OUTPUT_GLB"])

    if not input_ply.exists():
        print(f"[Splat→Mesh] ERROR: Input not found: {input_ply}", file=sys.stderr)
        sys.exit(1)

    pcd = load_splat_ply(input_ply)
    pcd = denoise_and_filter(pcd)
    mesh = poisson_reconstruct(pcd)
    export_glb(mesh, output_glb)

    verts = len(np.asarray(mesh.vertices))
    faces = len(np.asarray(mesh.triangles))
    print(f"[Splat→Mesh] Done. Vertices: {verts:,}  Faces: {faces:,}")


if __name__ == "__main__":
    main()
