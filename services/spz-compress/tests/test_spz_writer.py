"""Roundtrip and format-conformance tests for the SPZ v2 encoder.

The decoder half of the roundtrip re-implements the reference
`unpackGaussians` math (nianticlabs/spz load-spz.cc) so any packing drift
from the spec fails here rather than in a viewer.
"""
import gzip
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spz_writer import (  # noqa: E402
    COLOR_SCALE,
    FRACTIONAL_BITS,
    MAGIC,
    VERSION,
    GaussianCloud,
    dim_for_degree,
    pack_spz,
    read_gaussian_ply,
    write_spz,
)


def make_cloud(n: int = 64, sh_degree: int = 3, seed: int = 7) -> GaussianCloud:
    rng = np.random.default_rng(seed)
    dim = dim_for_degree(sh_degree)
    rot = rng.normal(size=(n, 4)).astype(np.float32)
    return GaussianCloud(
        positions=rng.uniform(-8, 8, size=(n, 3)).astype(np.float32),
        scales=rng.uniform(-9.5, -1.0, size=(n, 3)).astype(np.float32),
        rotations=rot,
        alphas=rng.uniform(-4, 4, size=n).astype(np.float32),
        colors=rng.uniform(-2, 2, size=(n, 3)).astype(np.float32),
        sh=rng.uniform(-0.4, 0.4, size=(n, dim * 3)).astype(np.float32),
        sh_degree=sh_degree,
    )


def write_synthetic_ply(path: Path, cloud: GaussianCloud) -> None:
    """Write an INRIA-convention 3DGS binary PLY from a GaussianCloud."""
    n = cloud.num_points
    dim = dim_for_degree(cloud.sh_degree)
    names = ["x", "y", "z", "nx", "ny", "nz", "f_dc_0", "f_dc_1", "f_dc_2"]
    names += [f"f_rest_{i}" for i in range(dim * 3)]
    names += ["opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"]

    header = ["ply", "format binary_little_endian 1.0", f"element vertex {n}"]
    header += [f"property float {p}" for p in names]
    header += ["end_header"]

    data = np.zeros((n, len(names)), dtype="<f4")
    data[:, 0:3] = cloud.positions
    data[:, 6:9] = cloud.colors
    # PLY f_rest is channel-major; cloud.sh is coeff-major interleaved-RGB
    if dim:
        rest = cloud.sh.reshape(n, dim, 3).transpose(0, 2, 1).reshape(n, dim * 3)
        data[:, 9:9 + dim * 3] = rest
    base = 9 + dim * 3
    data[:, base] = cloud.alphas
    data[:, base + 1:base + 4] = cloud.scales
    # PLY rot_0..3 = (w, x, y, z); cloud.rotations = xyzw
    data[:, base + 4] = cloud.rotations[:, 3]
    data[:, base + 5:base + 8] = cloud.rotations[:, :3]

    with open(path, "wb") as f:
        f.write(("\n".join(header) + "\n").encode())
        f.write(data.tobytes())


def unpack_spz(raw: bytes):
    """Reference-faithful decoder for the packed v2 layout."""
    magic, version, n, sh_degree, frac_bits, flags, _ = struct.unpack_from("<IIIBBBB", raw, 0)
    assert magic == MAGIC and version == VERSION
    dim = dim_for_degree(sh_degree)
    off = 16

    pos_b = np.frombuffer(raw, dtype=np.uint8, count=n * 9, offset=off).reshape(n * 3, 3)
    off += n * 9
    fixed = (
        pos_b[:, 0].astype(np.int32)
        | (pos_b[:, 1].astype(np.int32) << 8)
        | (pos_b[:, 2].astype(np.int32) << 16)
    )
    fixed = np.where(fixed & 0x800000, fixed - (1 << 24), fixed)  # sign-extend 24-bit
    positions = (fixed / (1 << frac_bits)).reshape(n, 3)

    alphas_u8 = np.frombuffer(raw, dtype=np.uint8, count=n, offset=off); off += n
    a = np.clip(alphas_u8 / 255.0, 1e-6, 1 - 1e-6)
    alphas = np.log(a / (1 - a))  # inverse sigmoid

    colors_u8 = np.frombuffer(raw, dtype=np.uint8, count=n * 3, offset=off); off += n * 3
    colors = ((colors_u8 / 255.0) - 0.5).reshape(n, 3) / COLOR_SCALE

    scales_u8 = np.frombuffer(raw, dtype=np.uint8, count=n * 3, offset=off); off += n * 3
    scales = (scales_u8 / 16.0 - 10.0).reshape(n, 3)

    rot_u8 = np.frombuffer(raw, dtype=np.uint8, count=n * 3, offset=off).reshape(n, 3); off += n * 3
    xyz = rot_u8 / 127.5 - 1.0
    w = np.sqrt(np.clip(1.0 - np.sum(xyz * xyz, axis=1), 0.0, 1.0))
    rotations = np.concatenate([xyz, w[:, None]], axis=1)

    sh_u8 = np.frombuffer(raw, dtype=np.uint8, count=n * dim * 3, offset=off)
    sh = ((sh_u8.astype(np.float32) - 128.0) / 128.0).reshape(n, dim * 3)

    return positions, scales, rotations, alphas, colors, sh, sh_degree


def normalize_quats(q: np.ndarray) -> np.ndarray:
    q = q / np.linalg.norm(q, axis=1, keepdims=True)
    return np.where(q[:, 3:4] < 0, -q, q)  # fold w positive


def test_header_and_section_sizes():
    cloud = make_cloud(n=10, sh_degree=2)
    raw = pack_spz(cloud)
    dim = dim_for_degree(2)
    expected = 16 + 10 * 9 + 10 + 10 * 3 + 10 * 3 + 10 * 3 + 10 * dim * 3
    assert len(raw) == expected
    magic, version, n, deg, frac, flags, reserved = struct.unpack_from("<IIIBBBB", raw, 0)
    assert (magic, version, n, deg, frac, reserved) == (MAGIC, VERSION, 10, 2, FRACTIONAL_BITS, 0)


def test_roundtrip_tolerances():
    cloud = make_cloud(n=256, sh_degree=3)
    positions, scales, rotations, alphas, colors, sh, deg = unpack_spz(pack_spz(cloud))

    assert deg == 3
    # positions: 12 fractional bits → half-step 2^-13
    np.testing.assert_allclose(positions, cloud.positions, atol=1.5 / (1 << 13))
    # scales: 1/16-log-unit quantization → half-step 1/32
    np.testing.assert_allclose(scales, cloud.scales, atol=1.0 / 32 + 1e-6)
    # colors: u8 over the 0.15-scaled wide range
    np.testing.assert_allclose(colors, cloud.colors, atol=0.5 / (COLOR_SCALE * 255.0) + 1e-6)
    # alphas compare post-sigmoid (u8 resolution)
    sig = lambda x: 1 / (1 + np.exp(-x))  # noqa: E731
    np.testing.assert_allclose(sig(alphas), sig(cloud.alphas), atol=1.0 / 255)
    # rotations: per-component compare is ill-conditioned where w≈0 (w is
    # recovered as sqrt(1-|xyz|²) from u8 xyz — a v2 format property, the
    # reference decoder behaves identically). Compare as rotations instead:
    # |<q_dec, q_true>| = cos(half the rotation angle between them).
    q_true = normalize_quats(cloud.rotations.astype(np.float64))
    dots = np.abs(np.sum(rotations * q_true, axis=1))
    # Bulk of quats: sub-degree error. The w≈0 tail degrades to ~10° because
    # u8 xyz noise can push |xyz|²>1 and clamp w to 0 — v2's known weakness
    # (v3's smallest-three packing exists to fix it). Order/sign bugs give
    # dot ≪ 0.9 across the board, which is what this guards against.
    assert np.percentile(dots, 90) >= 0.99995
    assert dots.min() >= 0.98
    # SH: deg-1 coeffs at 5 bits, rest at 4 bits (bucket size / 128 half-error + rounding)
    np.testing.assert_allclose(sh[:, :9], cloud.sh[:, :9], atol=(1 << 3) / 128.0)
    np.testing.assert_allclose(sh[:, 9:], cloud.sh[:, 9:], atol=(1 << 4) / 128.0)


def test_sh_degrees_0_and_1():
    for deg in (0, 1):
        cloud = make_cloud(n=16, sh_degree=deg)
        raw = pack_spz(cloud)
        _, _, _, _, _, sh, unpacked_deg = unpack_spz(raw)
        assert unpacked_deg == deg
        assert sh.shape == (16, dim_for_degree(deg) * 3)


def test_ply_roundtrip(tmp_path):
    cloud = make_cloud(n=128, sh_degree=3, seed=11)
    ply = tmp_path / "point_cloud.ply"
    write_synthetic_ply(ply, cloud)

    parsed = read_gaussian_ply(ply)
    assert parsed.num_points == 128
    assert parsed.sh_degree == 3
    np.testing.assert_allclose(parsed.positions, cloud.positions, atol=1e-6)
    np.testing.assert_allclose(parsed.rotations, cloud.rotations, atol=1e-6)
    np.testing.assert_allclose(parsed.sh, cloud.sh, atol=1e-6)

    out = tmp_path / "output.spz"
    n_bytes = write_spz(parsed, out)
    assert out.stat().st_size == n_bytes > 0
    # gzip container must decode back to the packed layout
    positions, *_ = unpack_spz(gzip.decompress(out.read_bytes()))
    np.testing.assert_allclose(positions, cloud.positions, atol=1.5 / (1 << 13))


def test_compression_reduces_size(tmp_path):
    cloud = make_cloud(n=4096, sh_degree=3, seed=3)
    ply = tmp_path / "point_cloud.ply"
    write_synthetic_ply(ply, cloud)
    out = tmp_path / "output.spz"
    write_spz(read_gaussian_ply(ply), out)
    # Random (incompressible-ish) data still lands far under the PLY size;
    # real captures compress much further (~90%).
    assert out.stat().st_size < ply.stat().st_size * 0.35


def test_rejects_non_ply(tmp_path):
    bad = tmp_path / "bad.ply"
    bad.write_bytes(b"not a ply at all")
    with pytest.raises(ValueError, match="not a PLY"):
        read_gaussian_ply(bad)
