"use client";

import { Suspense, useRef, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { Canvas } from "@react-three/fiber";
import { OrbitControls, Environment, Html, useProgress, useGLTF, Center } from "@react-three/drei";
import { motion, AnimatePresence } from "framer-motion";
import useSWR from "swr";

import SpzPoints, { useSpz } from "@/components/SpzPoints";

const fetcher = (url: string) => fetch(url).then((r) => r.json());

function Loader() {
  const { progress } = useProgress();
  return (
    <Html center>
      <div className="flex flex-col items-center gap-3">
        <div className="w-48 h-1 bg-white/10 rounded-full overflow-hidden">
          <motion.div
            className="h-full bg-sky-400 rounded-full"
            initial={{ width: 0 }}
            animate={{ width: `${progress}%` }}
          />
        </div>
        <p className="text-slate-400 text-xs">{Math.round(progress)}% loaded</p>
      </div>
    </Html>
  );
}

function MeshModel({ url }: { url: string }) {
  const { scene } = useGLTF(url);
  return (
    <Center>
      <primitive object={scene} />
    </Center>
  );
}

function SplatCloud({ url }: { url: string }) {
  const { cloud, error } = useSpz(url);
  if (error) {
    return (
      <Html center>
        <p className="text-xs text-rose-400 whitespace-nowrap">Splat load failed: {error}</p>
      </Html>
    );
  }
  if (!cloud) return <Loader />;
  return <SpzPoints cloud={cloud} />;
}

function Scene({ meshUrl, splatUrl }: { meshUrl: string; splatUrl: string }) {
  return (
    <>
      <ambientLight intensity={0.5} />
      <directionalLight position={[5, 10, 5]} intensity={1.5} castShadow />
      <Environment preset="studio" />
      {/* Real artifacts: prefer the .spz gaussian cloud, fall back to the
          reconstructed mesh. No placeholder geometry — an empty state is
          more honest than a torus knot. */}
      {splatUrl ? (
        <SplatCloud url={splatUrl} />
      ) : meshUrl ? (
        <MeshModel url={meshUrl} />
      ) : (
        <Html center>
          <p className="text-xs text-slate-500 whitespace-nowrap">
            No 3D artifacts yet — capture is still processing.
          </p>
        </Html>
      )}
      <OrbitControls
        enableDamping
        dampingFactor={0.05}
        minDistance={1.5}
        maxDistance={8}
        autoRotate
        autoRotateSpeed={0.5}
      />
    </>
  );
}

const FORMATS = ["glb", "spz", "obj", "usdz", "ply"];

export default function CapturePage() {
  const params = useParams<{ id: string }>();
  const { data: capture, isLoading } = useSWR(
    params.id ? `/api/v1/captures/${params.id}` : null,
    fetcher
  );
  const { data: annotations } = useSWR(
    params.id ? `/api/v1/social/captures/${params.id}/annotations` : null,
    fetcher
  );
  const { data: embedData } = useSWR(
    capture?.is_public ? `/api/v1/export/${params.id}/embed` : null,
    fetcher
  );

  const [activeTab, setActiveTab] = useState<"viewer" | "info" | "embed" | "dispatch">("viewer");
  const [embedCopied, setEmbedCopied] = useState(false);
  const [likeLoading, setLikeLoading] = useState(false);

  if (isLoading) return <CapturePageSkeleton />;
  if (!capture) return <div className="p-8 text-slate-400">Capture not found.</div>;

  const copyEmbed = () => {
    if (embedData?.iframe_snippet) {
      navigator.clipboard.writeText(embedData.iframe_snippet);
      setEmbedCopied(true);
      setTimeout(() => setEmbedCopied(false), 2000);
    }
  };

  const ECOSYSTEM_TARGETS = [
    { id: "blueprint-harvester", label: "📦 Blueprint Harvester", desc: "Archive to global 3D data lake" },
    { id: "yantra4d", label: "⚙️ Yantra4D", desc: "Open in parametric CAD engine" },
    { id: "factlas", label: "🌍 Factlas", desc: "Pin to geospatial atlas" },
    { id: "ceq", label: "🧠 CEQ", desc: "Generate marketing renders" },
  ];

  return (
    <div className="min-h-screen flex flex-col">
      {/* Nav */}
      <nav className="flex items-center justify-between px-6 py-4 border-b border-white/5">
        <Link href="/" className="flex items-center gap-2 text-sm text-slate-400 hover:text-white transition-colors">
          <span>←</span> Gallery
        </Link>
        <span
          className="text-xl font-bold"
          style={{
            background: "linear-gradient(135deg,#e2e8f0 0%,#38bdf8 100%)",
            WebkitBackgroundClip: "text",
            WebkitTextFillColor: "transparent",
          }}
        >
          👁 Eido
        </span>
        <div className="flex gap-2">
          {capture.is_public && (
            <a
              href={`/api/v1/export/${params.id}?format=glb`}
              className="px-3 py-1.5 text-xs rounded-lg bg-white/5 border border-white/10 text-slate-300 hover:bg-white/10 transition-colors"
            >
              ↓ Download
            </a>
          )}
        </div>
      </nav>

      <div className="flex flex-1 overflow-hidden">
        {/* 3D Viewer — Left column */}
        <div className="flex-1 bg-[#060610] relative">
          <Canvas
            camera={{ position: [0, 0, 3], fov: 50 }}
            className="w-full h-full"
            shadows
          >
            <Suspense fallback={<Loader />}>
              <Scene meshUrl={capture.mesh_url || ""} splatUrl={capture.splat_url || ""} />
            </Suspense>
          </Canvas>

          {/* Floating stat pills */}
          <div className="absolute bottom-4 left-4 flex gap-2 flex-wrap pointer-events-none">
            {capture.gaussian_count && (
              <span className="glass text-[11px] text-sky-300 px-3 py-1 rounded-full">
                {(capture.gaussian_count / 1_000_000).toFixed(2)}M gaussians
              </span>
            )}
            {capture.vertex_count && (
              <span className="glass text-[11px] text-emerald-300 px-3 py-1 rounded-full">
                {(capture.vertex_count / 1_000).toFixed(0)}K vertices
              </span>
            )}
            <span className="glass text-[11px] text-slate-400 px-3 py-1 rounded-full uppercase">
              {capture.mode}
            </span>
          </div>
        </div>

        {/* Sidebar — Right column */}
        <aside className="w-80 flex flex-col border-l border-white/5 bg-[#0d0d18] overflow-y-auto">
          {/* Title block */}
          <div className="p-6 border-b border-white/5">
            <h1 className="text-lg font-semibold text-white leading-tight">{capture.title}</h1>
            {capture.description && (
              <p className="text-sm text-slate-400 mt-2">{capture.description}</p>
            )}
            {capture.tags?.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mt-3">
                {capture.tags.map((tag: string) => (
                  <Link key={tag} href={`/?tag=${tag}`}>
                    <span className="text-[11px] px-2 py-0.5 rounded-full bg-sky-950/60 text-sky-400 border border-sky-800/50 hover:bg-sky-900/60 transition-colors">
                      {tag}
                    </span>
                  </Link>
                ))}
              </div>
            )}
            {capture.license && (
              <p className="text-[11px] text-slate-600 mt-3">🔖 {capture.license}</p>
            )}
          </div>

          {/* Tabs */}
          <div className="flex border-b border-white/5 text-xs">
            {(["viewer", "info", "embed", "dispatch"] as const).map((tab) => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={`flex-1 py-3 capitalize transition-colors ${
                  activeTab === tab
                    ? "text-sky-400 border-b-2 border-sky-400"
                    : "text-slate-500 hover:text-slate-300"
                }`}
              >
                {tab}
              </button>
            ))}
          </div>

          {/* Tab content */}
          <div className="flex-1 p-5">
            {activeTab === "viewer" && (
              <div className="space-y-4">
                <div>
                  <p className="text-[11px] text-slate-500 uppercase tracking-wider mb-2">
                    Spatial Annotations ({annotations?.length ?? 0})
                  </p>
                  {annotations?.length === 0 && (
                    <p className="text-xs text-slate-600">No annotations yet. Click to pin a note.</p>
                  )}
                  {annotations?.map((a: any) => (
                    <div key={a.id} className="text-xs text-slate-300 glass px-3 py-2 rounded-lg mb-2">
                      <span className="text-sky-400 font-mono text-[10px]">
                        ({a.x.toFixed(2)}, {a.y.toFixed(2)}, {a.z.toFixed(2)})
                      </span>
                      <p className="mt-0.5">{a.text}</p>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {activeTab === "info" && (
              <div className="space-y-3 text-sm">
                {[
                  ["Mode", capture.mode],
                  ["Status", capture.status],
                  ["Scale", capture.scale_metric],
                  ["Georeferenced", capture.is_georeferenced ? "Yes ✓" : "No"],
                  ["Gaussian Count", capture.gaussian_count?.toLocaleString()],
                  ["Vertex Count", capture.vertex_count?.toLocaleString()],
                ].map(([label, val]) =>
                  val ? (
                    <div key={label} className="flex justify-between">
                      <span className="text-slate-500">{label}</span>
                      <span className="text-slate-200">{val}</span>
                    </div>
                  ) : null
                )}
                <hr className="border-white/5 my-2" />
                <p className="text-slate-500 text-xs">Download formats</p>
                <div className="grid grid-cols-3 gap-1.5">
                  {FORMATS.map((fmt) => (
                    <a
                      key={fmt}
                      href={`/api/v1/export/${params.id}?format=${fmt}`}
                      className="text-center text-[11px] py-1.5 rounded-lg bg-white/5 border border-white/10 text-slate-300 hover:bg-white/10 transition-colors uppercase"
                    >
                      .{fmt}
                    </a>
                  ))}
                </div>
              </div>
            )}

            {activeTab === "embed" && (
              <div className="space-y-3">
                <p className="text-xs text-slate-400">
                  Embed this 3D viewer anywhere with an iframe.
                </p>
                {embedData ? (
                  <>
                    <pre className="text-[10px] text-slate-400 glass p-3 rounded-lg overflow-auto whitespace-pre-wrap break-all">
                      {embedData.iframe_snippet}
                    </pre>
                    <button
                      onClick={copyEmbed}
                      className="w-full py-2 text-xs rounded-lg bg-sky-500 hover:bg-sky-400 text-white transition-colors"
                    >
                      {embedCopied ? "✓ Copied!" : "Copy Embed Code"}
                    </button>
                  </>
                ) : (
                  <p className="text-xs text-slate-600">Capture must be public to embed.</p>
                )}
              </div>
            )}

            {activeTab === "dispatch" && (
              <div className="space-y-3">
                <p className="text-xs text-slate-400">
                  Send this capture to an Eido ecosystem platform.
                </p>
                {ECOSYSTEM_TARGETS.map((t) => (
                  <button
                    key={t.id}
                    className="w-full text-left px-4 py-3 glass rounded-xl hover:border-sky-800/60 transition-all group"
                    onClick={() =>
                      fetch(`/api/v1/handoffs/${params.id}/retry`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ target: t.id }),
                      })
                    }
                  >
                    <p className="text-sm font-medium text-white group-hover:text-sky-400 transition-colors">
                      {t.label}
                    </p>
                    <p className="text-[11px] text-slate-500 mt-0.5">{t.desc}</p>
                  </button>
                ))}
              </div>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}

function CapturePageSkeleton() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-[#0a0a0f]">
      <div className="w-16 h-16 border-2 border-sky-400/30 border-t-sky-400 rounded-full animate-spin" />
    </div>
  );
}
