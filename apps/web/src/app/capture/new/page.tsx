"use client";

/**
 * /capture/new — upload a capture and enqueue the 3DGS pipeline.
 *
 * Flow: POST /api/v1/captures/ingest → PUT file to the pre-signed URL →
 * POST /api/v1/captures/{id}/process → redirect to /capture/{id}.
 *
 * Auth is a pasted Janua bearer token for now (kept in sessionStorage only) —
 * the web OIDC login flow is deferred until the eido-web Janua client is
 * registered. Honest pre-alpha, not a fake login.
 */
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";

type Phase = "idle" | "registering" | "uploading" | "queueing" | "done" | "error";

const TOKEN_KEY = "eido.janua_token";

export default function NewCapturePage() {
  const router = useRouter();
  const [token, setToken] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState("");
  const [license, setLicense] = useState("CC-BY-4.0");
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  useEffect(() => {
    const saved = sessionStorage.getItem(TOKEN_KEY);
    if (saved) setToken(saved);
  }, []);

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f) setFile(f);
  }, []);

  const submit = async () => {
    if (!file || !title || !token) return;
    setError(null);
    sessionStorage.setItem(TOKEN_KEY, token);
    const auth = { Authorization: `Bearer ${token}` };

    try {
      setPhase("registering");
      const ingest = await fetch("/api/v1/captures/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...auth },
        body: JSON.stringify({
          title,
          description: description || null,
          mode: "3dgs",
          file_name: file.name,
          file_size_bytes: file.size,
          tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
          license,
        }),
      });
      if (!ingest.ok) throw new Error(`ingest failed (${ingest.status}): ${await ingest.text()}`);
      const { capture_id, upload_url } = await ingest.json();

      setPhase("uploading");
      const put = await fetch(upload_url, { method: "PUT", body: file });
      if (!put.ok) throw new Error(`upload failed (${put.status})`);

      setPhase("queueing");
      const proc = await fetch(`/api/v1/captures/${capture_id}/process`, {
        method: "POST",
        headers: auth,
      });
      if (!proc.ok) throw new Error(`process enqueue failed (${proc.status})`);

      setPhase("done");
      router.push(`/capture/${capture_id}`);
    } catch (e: unknown) {
      setPhase("error");
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const busy = phase === "registering" || phase === "uploading" || phase === "queueing";
  const inputCls =
    "w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white " +
    "placeholder:text-slate-600 focus:outline-none focus:border-sky-500/60";

  return (
    <div className="min-h-screen">
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
        <span className="w-16" />
      </nav>

      <main className="max-w-xl mx-auto px-6 py-10">
        <h1 className="text-2xl font-semibold text-white">New capture</h1>
        <p className="text-sm text-slate-400 mt-1">
          Upload a capture archive (zip of photos, or a video). The 3DGS
          pipeline runs after upload.
        </p>

        <div className="space-y-4 mt-8">
          <div>
            <label className="text-xs text-slate-500 uppercase tracking-wider">Janua token</label>
            <input
              className={inputCls}
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="Paste a Janua bearer token (web SSO coming)"
            />
          </div>

          <div>
            <label className="text-xs text-slate-500 uppercase tracking-wider">Title</label>
            <input className={inputCls} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Fountain, Cuernavaca courtyard" />
          </div>

          <div>
            <label className="text-xs text-slate-500 uppercase tracking-wider">Description</label>
            <textarea className={inputCls} rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-slate-500 uppercase tracking-wider">Tags (comma-sep)</label>
              <input className={inputCls} value={tags} onChange={(e) => setTags(e.target.value)} placeholder="architecture, mexico" />
            </div>
            <div>
              <label className="text-xs text-slate-500 uppercase tracking-wider">License</label>
              <select className={inputCls} value={license} onChange={(e) => setLicense(e.target.value)}>
                {["CC-BY-4.0", "CC-BY-SA-4.0", "CC0-1.0", "All rights reserved"].map((l) => (
                  <option key={l} value={l} className="bg-[#0d0d18]">{l}</option>
                ))}
              </select>
            </div>
          </div>

          <div
            onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onDrop={onDrop}
            className={`border-2 border-dashed rounded-2xl p-8 text-center transition-colors ${
              dragOver ? "border-sky-400 bg-sky-950/20" : "border-white/10"
            }`}
          >
            {file ? (
              <p className="text-sm text-slate-200">
                {file.name} <span className="text-slate-500">({(file.size / 1e6).toFixed(1)} MB)</span>
              </p>
            ) : (
              <p className="text-sm text-slate-500">Drag a .zip / video here, or</p>
            )}
            <label className="inline-block mt-3 px-4 py-2 text-xs rounded-lg bg-white/5 border border-white/10 text-slate-300 hover:bg-white/10 cursor-pointer transition-colors">
              Browse files
              <input
                type="file"
                className="hidden"
                accept=".zip,video/*"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
            </label>
          </div>

          {error && (
            <p className="text-xs text-rose-400 glass rounded-lg px-3 py-2 break-all">{error}</p>
          )}

          <button
            onClick={submit}
            disabled={busy || !file || !title || !token}
            className="w-full py-3 rounded-xl bg-sky-500 hover:bg-sky-400 disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm font-medium transition-colors"
          >
            {phase === "registering" && "Registering capture…"}
            {phase === "uploading" && "Uploading…"}
            {phase === "queueing" && "Enqueueing pipeline…"}
            {(phase === "idle" || phase === "error" || phase === "done") && "Upload & process"}
          </button>
        </div>
      </main>
    </div>
  );
}
