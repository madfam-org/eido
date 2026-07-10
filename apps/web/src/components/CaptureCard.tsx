"use client";

import { motion } from "framer-motion";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo } from "react";

interface Capture {
  id: string;
  title: string;
  thumbnail_url: string | null;
  splat_url: string | null;
  mesh_url: string | null;
  vertex_count: number | null;
  gaussian_count: number | null;
  mode: string;
  license: string | null;
  tags: string[];
  is_georeferenced: boolean;
}

// Deterministic hue from the capture id — every card gets a distinct, stable
// poster so the grid looks intentional rather than 24 identical placeholders.
function hueFromId(id: string): number {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 360;
  return h;
}

/**
 * Static poster for the gallery grid.
 *
 * IMPORTANT: no WebGL here. The previous card mounted a live R3F <Canvas> per
 * tile; 24 tiles blew past the browser's ~16 WebGL-context cap and the grid
 * blanked out. The real splat viewer lives on the capture detail page. Until
 * the pipeline generates thumbnails, an unprocessed capture shows a deterministic
 * point-cloud-style gradient — not a spinning torus knot.
 */
function PosterFallback({ id, mode }: { id: string; mode: string }) {
  const hue = useMemo(() => hueFromId(id), [id]);
  return (
    <div
      className="relative w-full aspect-square overflow-hidden"
      style={{
        background: `radial-gradient(120% 120% at 30% 20%, hsl(${hue} 70% 22%) 0%, #0b0b14 70%)`,
      }}
    >
      {/* faint point-cloud dot grid — evokes a splat without a GL context */}
      <div
        aria-hidden
        className="absolute inset-0 opacity-40"
        style={{
          backgroundImage: `radial-gradient(hsl(${hue} 80% 70% / 0.5) 1px, transparent 1.4px)`,
          backgroundSize: "14px 14px",
          maskImage: "radial-gradient(70% 70% at 50% 45%, black 0%, transparent 75%)",
          WebkitMaskImage: "radial-gradient(70% 70% at 50% 45%, black 0%, transparent 75%)",
        }}
      />
      <span className="absolute bottom-2.5 left-3 text-[10px] uppercase tracking-widest text-white/45">
        {mode === "3dgs" ? "Gaussian Splat" : mode}
      </span>
    </div>
  );
}

/** Capture card for the gallery grid. */
export default function CaptureCard({ capture }: { capture: Capture }) {
  const router = useRouter();

  return (
    <motion.div
      whileHover={{ scale: 1.02, y: -4 }}
      transition={{ type: "spring", stiffness: 300 }}
      className="group rounded-xl overflow-hidden border border-white/8 bg-white/3 backdrop-blur"
    >
      <Link href={`/capture/${capture.id}`} className="block cursor-pointer">
        {capture.thumbnail_url ? (
          <div
            className="aspect-square bg-cover bg-center"
            style={{ backgroundImage: `url(${capture.thumbnail_url})` }}
          />
        ) : (
          <PosterFallback id={capture.id} mode={capture.mode} />
        )}
        <div className="px-4 pt-4">
          <h2 className="font-semibold text-sm text-white truncate group-hover:text-sky-400 transition-colors">
            {capture.title}
          </h2>
          <div className="flex items-center gap-2 mt-1.5">
            <span className="text-xs text-slate-500 uppercase tracking-wider">
              {capture.mode}
            </span>
            {capture.gaussian_count ? (
              <span className="text-xs text-slate-600">
                · {(capture.gaussian_count / 1_000).toFixed(0)}K gaussians
              </span>
            ) : null}
            {capture.is_georeferenced && (
              <span className="ml-auto text-xs text-emerald-400">📍</span>
            )}
          </div>
        </div>
      </Link>

      {/* Tags are navigable (button, not a nested <a> inside the card Link). */}
      {capture.tags.length > 0 && (
        <div className="flex flex-wrap gap-1 px-4 pb-4 pt-2">
          {capture.tags.slice(0, 3).map((tag) => (
            <button
              key={tag}
              type="button"
              onClick={() => router.push(`/?tag=${encodeURIComponent(tag)}`)}
              className="text-[10px] px-2 py-0.5 rounded-full bg-sky-950/50 text-sky-400 border border-sky-800/50 hover:bg-sky-900/60 hover:border-sky-600 transition-colors"
            >
              {tag}
            </button>
          ))}
        </div>
      )}
    </motion.div>
  );
}
