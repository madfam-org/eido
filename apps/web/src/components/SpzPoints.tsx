"use client";

/**
 * SpzPoints — renders the gaussian centers of a .spz capture as a colored
 * point cloud.
 *
 * Decodes Niantic SPZ v2 (gzip container, 16-byte header, SoA layout) in the
 * browser via DecompressionStream — the same profile services/spz-compress
 * writes. This is the honest MVP viewer: real capture data, point-splat
 * rendering (full gaussian rasterization comes later).
 */
import { useEffect, useMemo, useState } from "react";
import * as THREE from "three";

const SPZ_MAGIC = 0x5053474e; // "NGSP" LE
const SH_C0 = 0.28209479177387814; // SH basis DC term
const COLOR_SCALE = 0.15;

export interface SpzCloud {
  positions: Float32Array; // centered + normalized for display
  colors: Float32Array;
  numPoints: number;
}

async function gunzip(buf: ArrayBuffer): Promise<ArrayBuffer> {
  const ds = new DecompressionStream("gzip");
  const stream = new Blob([buf]).stream().pipeThrough(ds);
  return await new Response(stream).arrayBuffer();
}

export async function decodeSpz(buf: ArrayBuffer): Promise<SpzCloud> {
  const raw = await gunzip(buf);
  const view = new DataView(raw);
  const magic = view.getUint32(0, true);
  const version = view.getUint32(4, true);
  if (magic !== SPZ_MAGIC) throw new Error("not an SPZ file");
  if (version !== 2 && version !== 3) throw new Error(`unsupported SPZ version ${version}`);
  const n = view.getUint32(8, true);
  const fracBits = view.getUint8(13);
  const bytes = new Uint8Array(raw);

  let off = 16;
  const posBytes = bytes.subarray(off, off + n * 9);
  off += n * 9;
  const alphas = bytes.subarray(off, off + n);
  off += n;
  const colorBytes = bytes.subarray(off, off + n * 3);

  const scale = 1 / (1 << fracBits);
  const positions = new Float32Array(n * 3);
  for (let i = 0; i < n * 3; i++) {
    let fixed = posBytes[i * 3] | (posBytes[i * 3 + 1] << 8) | (posBytes[i * 3 + 2] << 16);
    if (fixed & 0x800000) fixed -= 1 << 24;
    positions[i] = fixed * scale;
  }

  // Display color from the stored SH DC term: rgb = 0.5 + C0 * f_dc
  const colors = new Float32Array(n * 3);
  for (let i = 0; i < n * 3; i++) {
    const fdc = (colorBytes[i] / 255 - 0.5) / COLOR_SCALE;
    colors[i] = Math.min(1, Math.max(0, 0.5 + SH_C0 * fdc));
  }

  // Drop near-transparent gaussians (noise floaters) by compacting in place.
  let kept = 0;
  for (let i = 0; i < n; i++) {
    if (alphas[i] < 26) continue; // sigmoid(alpha) < ~0.1
    positions.copyWithin(kept * 3, i * 3, i * 3 + 3);
    colors.copyWithin(kept * 3, i * 3, i * 3 + 3);
    kept++;
  }

  // Center + normalize to ~2 world units so the default camera frames it.
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < kept; i++) {
    for (let a = 0; a < 3; a++) {
      const v = positions[i * 3 + a];
      if (v < min[a]) min[a] = v;
      if (v > max[a]) max[a] = v;
    }
  }
  const center = [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2];
  const extent = Math.max(max[0] - min[0], max[1] - min[1], max[2] - min[2]) || 1;
  const s = 2 / extent;
  for (let i = 0; i < kept; i++) {
    for (let a = 0; a < 3; a++) {
      positions[i * 3 + a] = (positions[i * 3 + a] - center[a]) * s;
    }
  }

  return {
    positions: positions.subarray(0, kept * 3),
    colors: colors.subarray(0, kept * 3),
    numPoints: kept,
  };
}

export function useSpz(url: string | null | undefined) {
  const [cloud, setCloud] = useState<SpzCloud | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!url) return;
    let cancelled = false;
    setCloud(null);
    setError(null);
    fetch(url)
      .then((r) => {
        if (!r.ok) throw new Error(`fetch ${r.status}`);
        return r.arrayBuffer();
      })
      .then(decodeSpz)
      .then((c) => !cancelled && setCloud(c))
      .catch((e) => !cancelled && setError(String(e?.message ?? e)));
    return () => {
      cancelled = true;
    };
  }, [url]);

  return { cloud, error };
}

export default function SpzPoints({ cloud }: { cloud: SpzCloud }) {
  const geometry = useMemo(() => {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(cloud.positions, 3));
    g.setAttribute("color", new THREE.BufferAttribute(cloud.colors, 3));
    return g;
  }, [cloud]);

  useEffect(() => () => geometry.dispose(), [geometry]);

  return (
    <points geometry={geometry}>
      <pointsMaterial vertexColors size={0.012} sizeAttenuation depthWrite={false} />
    </points>
  );
}
