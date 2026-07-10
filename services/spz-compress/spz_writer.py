"""SPZ v2 encoder — compresses a 3DGS .ply point cloud into Niantic's .spz.

Pure Python + numpy port of the reference encoder's legacy (gzip,
single-stream) profile, byte-for-byte compatible with
nianticlabs/spz `packGaussians` + `serializePackedGaussians`:

  header (16B): u32 magic 0x5053474e | u32 version=2 | u32 numPoints |
                u8 shDegree | u8 fractionalBits=12 | u8 flags | u8 reserved
  body (SoA):   positions (3×24-bit LE fixed-point, 12 fractional bits)
                alphas    (u8, sigmoid-activated)
                colors    (u8, f_dc*0.15*255 + 127.5)
                scales    (u8, (log_scale+10)*16)
                rotations (u8×3, normalized quat xyz, w folded positive — v2
                           "first-three" packing)
                sh        (u8, x*128+128 bucket-rounded: 5 bits deg-1,
                           4 bits deg-2+)
  ...all gzip-compressed as one stream.

Coordinates are packed as-is (no basis conversion), matching the reference
PackOptions default of CoordinateSystem::UNSPECIFIED.

v2 is the profile every deployed spz reader supports; the current reference
writer emits v4 (zstd streams) but keeps this exact layout as its legacy read
path.
"""
from __future__ import annotations

import gzip
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MAGIC = 0x5053474E  # "NGSP" little-endian
VERSION = 2
FRACTIONAL_BITS = 12
COLOR_SCALE = 0.15
FLAG_ANTIALIASED = 0x1
SH1_BITS = 5
SH_REST_BITS = 4

# SH coefficients (per channel) beyond the DC term, by degree.
_DIM_FOR_DEGREE = {0: 0, 1: 3, 2: 8, 3: 15}


@dataclass
class GaussianCloud:
    """Unpacked 3DGS attributes, reference-layout.

    positions (N,3) · scales (N,3) log-encoded · rotations (N,4) quaternion
    in xyzw order · alphas (N,) pre-sigmoid logits · colors (N,3) SH DC
    (f_dc) · sh (N, dim*3) coeff-major with interleaved RGB
    (sh[:, k*3 + channel] = coefficient k of channel).
    """

    positions: np.ndarray
    scales: np.ndarray
    rotations: np.ndarray
    alphas: np.ndarray
    colors: np.ndarray
    sh: np.ndarray
    sh_degree: int
    antialiased: bool = False

    @property
    def num_points(self) -> int:
        return int(self.positions.shape[0])


def dim_for_degree(degree: int) -> int:
    return _DIM_FOR_DEGREE[degree]


def _degree_for_rest_count(n_rest_per_channel: int) -> int:
    for degree, dim in _DIM_FOR_DEGREE.items():
        if dim == n_rest_per_channel:
            return degree
    raise ValueError(f"unsupported f_rest count per channel: {n_rest_per_channel}")


def read_gaussian_ply(path: str | Path) -> GaussianCloud:
    """Read an INRIA-convention 3DGS binary PLY into a GaussianCloud.

    Expected vertex properties: x y z [nx ny nz] f_dc_0..2 f_rest_*
    opacity scale_0..2 rot_0..3 — all float32, binary_little_endian.
    rot_0..3 is stored (w,x,y,z) in the 3DGS convention and is reordered
    to the xyzw layout the spz reference uses. f_rest is channel-major in
    the PLY and is transposed to spz's coeff-major interleaved-RGB layout.
    """
    path = Path(path)
    with open(path, "rb") as f:
        if f.readline().strip() != b"ply":
            raise ValueError(f"{path}: not a PLY file")
        fmt = f.readline().strip()
        if fmt != b"format binary_little_endian 1.0":
            raise ValueError(f"{path}: unsupported PLY format: {fmt.decode(errors='replace')}")

        num_points = 0
        names: list[str] = []
        while True:
            line = f.readline()
            if not line:
                raise ValueError(f"{path}: unexpected EOF in header")
            line = line.strip()
            if line == b"end_header":
                break
            parts = line.decode().split()
            if parts[:2] == ["element", "vertex"]:
                num_points = int(parts[2])
            elif parts[0] == "element":
                raise ValueError(f"{path}: unsupported element: {parts[1]}")
            elif parts[0] == "property":
                if parts[1] != "float":
                    raise ValueError(f"{path}: non-float property {parts[2]}")
                names.append(parts[2])

        data = np.fromfile(f, dtype="<f4", count=num_points * len(names))
    if data.size != num_points * len(names):
        raise ValueError(f"{path}: truncated body")
    data = data.reshape(num_points, len(names))
    col = {name: i for i, name in enumerate(names)}

    for req in ("x", "y", "z", "f_dc_0", "f_dc_1", "f_dc_2", "opacity",
                "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"):
        if req not in col:
            raise ValueError(f"{path}: missing property {req}")

    positions = data[:, [col["x"], col["y"], col["z"]]]
    colors = data[:, [col["f_dc_0"], col["f_dc_1"], col["f_dc_2"]]]
    alphas = data[:, col["opacity"]]
    scales = data[:, [col["scale_0"], col["scale_1"], col["scale_2"]]]
    # (w,x,y,z) in the PLY → xyzw
    rotations = data[:, [col["rot_1"], col["rot_2"], col["rot_3"], col["rot_0"]]]

    n_rest = sum(1 for n in names if n.startswith("f_rest_"))
    if n_rest % 3 != 0:
        raise ValueError(f"{path}: f_rest count {n_rest} not divisible by 3")
    dim = n_rest // 3
    sh_degree = _degree_for_rest_count(dim)
    if dim > 0:
        rest_cols = [col[f"f_rest_{i}"] for i in range(n_rest)]
        rest = data[:, rest_cols].reshape(num_points, 3, dim)  # channel-major
        sh = rest.transpose(0, 2, 1).reshape(num_points, dim * 3)  # coeff-major, RGB interleaved
    else:
        sh = np.zeros((num_points, 0), dtype=np.float32)

    return GaussianCloud(
        positions=positions.astype(np.float32),
        scales=scales.astype(np.float32),
        rotations=rotations.astype(np.float32),
        alphas=alphas.astype(np.float32),
        colors=colors.astype(np.float32),
        sh=sh.astype(np.float32),
        sh_degree=sh_degree,
    )


def _to_uint8(x: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(x), 0.0, 255.0).astype(np.uint8)


def _quantize_sh(x: np.ndarray, bucket_size: int) -> np.ndarray:
    """quantizeSH: 8-bit quantization rounded to bucket centers (0 stays centered)."""
    q = np.rint(x * 128.0).astype(np.int32) + 128
    q = (q + bucket_size // 2) // bucket_size * bucket_size
    return np.clip(q, 0, 255).astype(np.uint8)


def _pack_positions(positions: np.ndarray) -> bytes:
    scale = float(1 << FRACTIONAL_BITS)
    fixed = np.rint(positions.reshape(-1) * scale).astype(np.int32)
    out = np.empty((fixed.size, 3), dtype=np.uint8)
    out[:, 0] = fixed & 0xFF
    out[:, 1] = (fixed >> 8) & 0xFF
    out[:, 2] = (fixed >> 16) & 0xFF
    return out.tobytes()


def _pack_rotations_first_three(rotations: np.ndarray) -> bytes:
    """v2 packing: normalize, fold w positive, store xyz as u8."""
    q = rotations.astype(np.float64)
    norm = np.linalg.norm(q, axis=1, keepdims=True)
    norm[norm == 0.0] = 1.0
    q = q / norm
    sign = np.where(q[:, 3:4] < 0, -127.5, 127.5)
    xyz = q[:, :3] * sign + 127.5
    return _to_uint8(xyz).tobytes()


def _pack_sh(sh: np.ndarray, sh_degree: int) -> bytes:
    if sh_degree == 0:
        return b""
    packed = np.empty_like(sh, dtype=np.uint8)
    # First 9 slots (3 coeffs × RGB) are degree-1; the rest are degree-2+.
    n_deg1 = min(9, sh.shape[1])
    packed[:, :n_deg1] = _quantize_sh(sh[:, :n_deg1], 1 << (8 - SH1_BITS))
    if sh.shape[1] > 9:
        packed[:, 9:] = _quantize_sh(sh[:, 9:], 1 << (8 - SH_REST_BITS))
    return packed.tobytes()


def pack_spz(cloud: GaussianCloud) -> bytes:
    """Pack a GaussianCloud into uncompressed SPZ v2 body bytes (header included)."""
    header = struct.pack(
        "<IIIBBBB",
        MAGIC,
        VERSION,
        cloud.num_points,
        cloud.sh_degree,
        FRACTIONAL_BITS,
        FLAG_ANTIALIASED if cloud.antialiased else 0,
        0,
    )
    body = b"".join(
        [
            header,
            _pack_positions(cloud.positions),
            _to_uint8(255.0 / (1.0 + np.exp(-cloud.alphas))).tobytes(),
            _to_uint8(cloud.colors * (COLOR_SCALE * 255.0) + 127.5).tobytes(),
            _to_uint8((cloud.scales + 10.0) * 16.0).tobytes(),
            _pack_rotations_first_three(cloud.rotations),
            _pack_sh(cloud.sh, cloud.sh_degree),
        ]
    )
    return body


def write_spz(cloud: GaussianCloud, path: str | Path) -> int:
    """Write a gzip-compressed .spz; returns the compressed byte count."""
    raw = pack_spz(cloud)
    data = gzip.compress(raw, compresslevel=9)
    Path(path).write_bytes(data)
    return len(data)


def compress_ply_to_spz(input_ply: str | Path, output_spz: str | Path) -> GaussianCloud:
    """Convenience: read a 3DGS PLY and write .spz next to it. Returns the cloud."""
    cloud = read_gaussian_ply(input_ply)
    write_spz(cloud, output_spz)
    return cloud
