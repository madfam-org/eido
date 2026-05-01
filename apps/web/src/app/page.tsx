"use client";

import { Suspense } from "react";
import Link from "next/link";
import { motion } from "framer-motion";
import useSWR from "swr";
import CaptureCard from "@/components/CaptureCard";

const fetcher = (url: string) => fetch(url).then((r) => r.json());

export default function GalleryPage() {
  const { data: captures, isLoading } = useSWR("/api/v1/captures/?limit=24", fetcher);

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
      </header>

      {/* ── Gallery Grid ────────────────────────────────────────────────── */}
      <section className="max-w-7xl mx-auto px-8 py-12">
        {isLoading ? (
          <GallerySkeleton />
        ) : (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.2 }}
            className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-6"
          >
            {(captures ?? []).map((capture: any, i: number) => (
              <motion.div
                key={capture.id}
                initial={{ opacity: 0, y: 24 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: i * 0.04 }}
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
        <div
          key={i}
          className="aspect-square rounded-xl bg-white/5 animate-pulse"
        />
      ))}
    </div>
  );
}
