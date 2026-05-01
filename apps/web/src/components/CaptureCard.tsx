"use client";

import { Canvas } from "@react-three/fiber";
import { OrbitControls, Environment, ContactShadows } from "@react-three/drei";
import { Suspense, useRef } from "react";
import { motion } from "framer-motion";
import Link from "next/link";

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

interface SplatViewerProps {
  capture: Capture;
}

/** Inline 3D splat viewer — loads .glb via R3F for portfolio previews. */
export function SplatViewer({ capture }: SplatViewerProps) {
  return (
    <div className="w-full aspect-square rounded-xl overflow-hidden bg-[#0d0d18]">
      <Canvas camera={{ position: [0, 0, 3], fov: 45 }} shadows>
        <ambientLight intensity={0.4} />
        <directionalLight position={[5, 5, 5]} intensity={1} castShadow />
        <Suspense fallback={<FallbackMesh />}>
          <Environment preset="studio" />
          <ContactShadows position={[0, -1, 0]} opacity={0.5} blur={2} />
          {/* In production: replace with <Splat url={capture.splat_url} /> from gsplat */}
          <PlaceholderGeometry />
        </Suspense>
        <OrbitControls
          enablePan={false}
          minDistance={1}
          maxDistance={6}
          autoRotate
          autoRotateSpeed={1.5}
        />
      </Canvas>
    </div>
  );
}

function FallbackMesh() {
  return (
    <mesh>
      <boxGeometry args={[1, 1, 1]} />
      <meshStandardMaterial color="#1e293b" wireframe />
    </mesh>
  );
}

function PlaceholderGeometry() {
  const ref = useRef<any>(null);
  return (
    <mesh ref={ref} castShadow>
      <torusKnotGeometry args={[0.6, 0.2, 128, 32]} />
      <meshStandardMaterial
        color="#38bdf8"
        roughness={0.2}
        metalness={0.8}
      />
    </mesh>
  );
}

/** Capture card for the gallery grid. */
export default function CaptureCard({ capture }: { capture: Capture }) {
  return (
    <Link href={`/capture/${capture.id}`}>
      <motion.div
        whileHover={{ scale: 1.02, y: -4 }}
        transition={{ type: "spring", stiffness: 300 }}
        className="group rounded-xl overflow-hidden border border-white/8 bg-white/3 backdrop-blur cursor-pointer"
      >
        {/* Thumbnail or inline viewer */}
        {capture.thumbnail_url ? (
          <div
            className="aspect-square bg-cover bg-center"
            style={{ backgroundImage: `url(${capture.thumbnail_url})` }}
          />
        ) : (
          <SplatViewer capture={capture} />
        )}

        {/* Metadata */}
        <div className="p-4">
          <h2 className="font-semibold text-sm text-white truncate group-hover:text-sky-400 transition-colors">
            {capture.title}
          </h2>
          <div className="flex items-center gap-2 mt-1.5">
            <span className="text-xs text-slate-500 uppercase tracking-wider">
              {capture.mode}
            </span>
            {capture.gaussian_count && (
              <span className="text-xs text-slate-600">
                · {(capture.gaussian_count / 1_000).toFixed(0)}K gaussians
              </span>
            )}
            {capture.is_georeferenced && (
              <span className="ml-auto text-xs text-emerald-400">📍</span>
            )}
          </div>
          {capture.tags.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-2">
              {capture.tags.slice(0, 3).map((tag) => (
                <span
                  key={tag}
                  className="text-[10px] px-2 py-0.5 rounded-full bg-sky-950/50 text-sky-400 border border-sky-800/50"
                >
                  {tag}
                </span>
              ))}
            </div>
          )}
        </div>
      </motion.div>
    </Link>
  );
}
