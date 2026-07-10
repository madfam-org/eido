"use client";

import { Suspense } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { motion } from "framer-motion";
import useSWR from "swr";
import CaptureCard from "@/components/CaptureCard";

const fetcher = async (url: string) => {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`Request failed: ${r.status}`);
  return r.json();
};

export default function GalleryPage() {
  return (
    <Suspense fallback={<main className="min-h-screen" />}>
      <Gallery />
    </Suspense>
  );
}

function Gallery() {
  const params = useSearchParams();
  const tag = params.get("tag");

  const query = `/api/v1/captures/?limit=24${tag ? `&tag=${encodeURIComponent(tag)}` : ""}`;
  const { data, error, isLoading, mutate } = useSWR(query, fetcher);
  const captures: any[] = Array.isArray(data) ? data : [];

  return (
    <main className="min-h-screen">
      {/* ── Hero Header ─────────────────────────────────────────────────── */}
      <header className="relative px-8 pt-16 pb-12 border-b border-white/5">
        <div className="max-w-7xl mx-auto flex items-end justify-between">
          <div>
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              className="flex items-center gap-3 mb-4"
            >
              <span className="text-3xl">👁</span>
              <span
                className="text-4xl font-bold tracking-tight"
                style={{
                  background: "linear-gradient(135deg, #e2e8f0 0%, #94a3b8 50%, #38bdf8 100%)",
                  WebkitBackgroundClip: "text",
                  WebkitTextFillColor: "transparent",
                }}
              >
                Eido
              </span>
            </motion.div>
            <motion.p
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: 0.15 }}
              className="text-slate-400 text-lg tracking-wide"
            >
              Capture Reality.{" "}
              <span className="text-sky-400 font-semibold">Command Form.</span>
            </motion.p>
          </div>

          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.3 }}
            className="flex gap-3"
          >
            <Link
              href="/capture/new"
              className="px-5 py-2.5 rounded-lg bg-sky-500 hover:bg-sky-400 text-white text-sm font-semibold transition-colors"
            >
              + New Capture
            </Link>
          </motion.div>
        </div>

        {/* Active tag filter */}
        {tag && (
          <div className="max-w-7xl mx-auto mt-6 flex items-center gap-2">
            <span className="text-sm text-slate-500">Filtered by</span>
            <span className="text-xs px-2.5 py-1 rounded-full bg-sky-950/60 text-sky-300 border border-sky-800/60">
              #{tag}
            </span>
            <Link href="/" className="text-xs text-slate-500 hover:text-slate-300 underline underline-offset-2">
              clear
            </Link>
          </div>
        )}
      </header>

      {/* ── Gallery Grid ────────────────────────────────────────────────── */}
      <section className="max-w-7xl mx-auto px-8 py-12">
        {isLoading ? (
          <GallerySkeleton />
        ) : error ? (
          <ErrorState onRetry={() => mutate()} />
        ) : captures.length === 0 ? (
          <EmptyState tag={tag} />
        ) : (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.2 }}
            className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-6"
          >
            {captures.map((capture: any, i: number) => (
              <motion.div
                key={capture.id}
                initial={{ opacity: 0, y: 24 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: Math.min(i, 12) * 0.04 }}
              >
                <CaptureCard capture={capture} />
              </motion.div>
            ))}
          </motion.div>
        )}
      </section>
    </main>
  );
}

function GallerySkeleton() {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-6">
      {Array.from({ length: 12 }).map((_, i) => (
        <div key={i} className="aspect-square rounded-xl bg-white/5 animate-pulse" />
      ))}
    </div>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center py-24 text-center">
      <span className="text-4xl mb-4">🌫️</span>
      <h2 className="text-lg font-semibold text-slate-200">Couldn’t load the gallery</h2>
      <p className="text-slate-500 mt-1 max-w-sm">
        The API didn’t respond. This is usually momentary.
      </p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-5 px-4 py-2 rounded-lg bg-white/8 hover:bg-white/12 text-sm text-slate-200 transition-colors"
      >
        Try again
      </button>
    </div>
  );
}

function EmptyState({ tag }: { tag: string | null }) {
  return (
    <div className="flex flex-col items-center justify-center py-24 text-center">
      <span className="text-4xl mb-4">👁</span>
      <h2 className="text-lg font-semibold text-slate-200">
        {tag ? `No captures tagged #${tag}` : "No captures yet"}
      </h2>
      <p className="text-slate-500 mt-1 max-w-sm">
        {tag
          ? "Try clearing the filter to see everything."
          : "Reality capture starts here — turn a set of photos into an explorable 3D scene."}
      </p>
      {tag ? (
        <Link
          href="/"
          className="mt-5 px-4 py-2 rounded-lg bg-white/8 hover:bg-white/12 text-sm text-slate-200 transition-colors"
        >
          Clear filter
        </Link>
      ) : (
        <Link
          href="/capture/new"
          className="mt-5 px-5 py-2.5 rounded-lg bg-sky-500 hover:bg-sky-400 text-white text-sm font-semibold transition-colors"
        >
          + Create your first capture
        </Link>
      )}
    </div>
  );
}
