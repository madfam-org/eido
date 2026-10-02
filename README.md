# Product Requirements Document (PRD): Eido
**Domains:** `eido.cam` (Product/Gallery) | `eidocam.com` (Redirect)
**Entity:** Innovaciones MADFAM SAS de CV

## Current Status (2026-10-01)

Eido is a **working shell with a real georeference path and no reconstruction**.
Read Sections 1–4 below as vision; this section is the truth.

**What exists and runs:**

- `eido.cam` is deployed — web, API and orchestration, via GitOps (CI signs and
  digest-pins; ArgoCD reconciles). Live health checks were recorded in the
  2026-07-10 go-live runbook (internal-devops); they are **not** re-verified in
  this README, so treat deployment state as "last known good", not as proof.
- A test suite exists and gates CI: 20 API tests, 30 pipeline-stage tests and
  a 3-test guard on `apps/web/next.config.js` (see
  [Web stack](#web-stack-and-security-invariants)).
- **Georeference works end to end in code.** `services/media-prep` (stage 0,
  CPU-only) extracts zip archives, decodes video to frames with ffmpeg, and
  reads the position the media already carries — GPS EXIF from stills, DJI SRT
  telemetry alongside video. `colmap-sfm` then runs `model_aligner` against
  those priors, and a georeferenced capture emits a located observation to
  Factlas carrying its coverage envelope and provenance.

**What does not work yet:**

- **The reconstruction pipeline does not run.** No GPU node exists in the
  cluster; the orchestration worker shells `docker run --gpus all` from a
  CPU-only pod, and the 3DGS trainer invokes a module that does not exist. No
  capture has produced a real `.spz` in production. media-prep is deliberately
  CPU-only so a capture is still normalized and georeferenced without it.
- **R2 storage and the Janua M2M service client are unprovisioned** as of the
  last recorded check (2026-07-10). Until they are, uploads and the Factlas
  handoff cannot complete in production regardless of the code.
- **Not built**, despite the repo structure in Section 5: the iOS/Android
  capture apps (`apps/mobile-ios`, `apps/mobile-android`), the `packages/`
  libraries (`r3f-splat-viewer`, `eido-sdk`), and `ops/terraform`.
- **The 3D viewer does not finish loading** (known bug, not yet fixed): see
  [Known issues](#known-issues).

> The status block this replaces was written 2026-07-04 and said "there is no
> working product… a single-commit code skeleton… no tests, releases, or
> deployments." That had been false for a month. Understating is a truthfulness
> failure in the same way overstating is: it teaches readers the status block is
> not worth reading.

## Web stack and security invariants

`apps/web` runs **Next.js 15.5.27 on React 19** (19.3.0 in the lockfile), with
`@react-three/fiber` 9 and `@react-three/drei` 10 (#25, 2026-10-01). It moved
from Next 14.2.35 (#22), whose remaining advisories have no 14.x fix.
`@react-three/postprocessing` was removed because nothing imported it. All
pages are client components, so the Next 15 async request APIs did not touch
`src/`. Next 15's App Router renders with its own bundled React; the installed
`react` mostly drives typings and peer resolution.

**Image optimizer (GHSA-2xp9-vwfh-vxw4), a security invariant:**

- `images.unoptimized: true` in `apps/web/next.config.js`; nothing uses
  `next/image`.
- `images.remotePatterns` is the exact allow-list
  `[{ protocol: "https", hostname: "cdn.eido.cam", port: "" }]`, with no
  `domains` list.
- `/_next/image` answers **404** for any `url`, so the web pod is never an
  image proxy.

`apps/web/tests/next-config.test.mjs` enforces the first two and fails if any
file under `src/` imports `next/image`. It runs with Node's built-in runner
(`cd apps/web && pnpm test`) in the CI job "Web — Lint & Build Check". The 404
itself was verified against the standalone build in #25; there is no live
smoke for it.

## Known issues

- **3D viewer stuck on its loading fallback.** `/capture/[id]` renders drei's
  `<Environment preset="studio" />`. drei fetches that HDR from
  `https://raw.githack.com/pmndrs/drei-assets/…`, but the CSP `connect-src` in
  `next.config.js` does not allow that origin. The HDR never loads, and the
  `<Suspense>` around the scene stays on `<Loader />`. Possible fixes: self-host
  the HDR under `public/` and pass `files=`, serve it from `cdn.eido.cam`, or
  drop `<Environment>`. Widening the CSP to a third-party CDN is the weakest
  option. This bug is documented only; it has not been fixed.
- `next lint` prints a deprecation notice on Next 15; it still works on 15.x.
- `postcss@8.4.31`, pinned exactly by `next@15.5.27`, still shows up in
  `pnpm audit`. It is build-time only and not part of the standalone server.
- The API still verifies Janua tokens with `python-jose` (`apps/api/src/eido_api/auth.py`).
  The fleet is moving to PyJWT; eido has not been ported yet.
- Python dependency note: `sqlalchemy` is pinned `<2.1`, because 2.1 drops
  greenlet from the default install and `sqlalchemy.ext.asyncio` then fails to
  import.

## Related repositories and contracts

- **Janua (identity):** the API verifies RS256 tokens against Janua's JWKS.
  Issuer, audience and `kid` rules:
  [janua `docs/guides/ECOSYSTEM_INTEGRATION.md`](https://github.com/madfam-org/janua/blob/main/docs/guides/ECOSYSTEM_INTEGRATION.md).
  Service-to-service tokens for hand-offs:
  [janua `docs/service-tokens.md`](https://github.com/madfam-org/janua/blob/main/docs/service-tokens.md).
- **Enclii (deploy platform):** onboarding and the GitOps/ArgoCD model:
  [`docs/cli/commands/onboard.md`](https://github.com/madfam-org/enclii/blob/main/docs/cli/commands/onboard.md),
  [`docs/infrastructure/GITOPS.md`](https://github.com/madfam-org/enclii/blob/main/docs/infrastructure/GITOPS.md),
  [`docs/runbooks/SIGNED_GITOPS_DIGESTS.md`](https://github.com/madfam-org/enclii/blob/main/docs/runbooks/SIGNED_GITOPS_DIGESTS.md)
  and the signature/digest admission policies in
  [`docs/infrastructure/KYVERNO_POLICIES.md`](https://github.com/madfam-org/enclii/blob/main/docs/infrastructure/KYVERNO_POLICIES.md).
- **Factlas (located observations):** eido's side of the contract is
  [`apps/api/contracts/observation.v1.json`](apps/api/contracts/observation.v1.json),
  pinned by `apps/api/tests/test_observation_contract.py`.

## 1. Vision & Philosophy
Eido is the sovereign optical sensor and spatial gallery of the MADFAM ecosystem. Derived from *Eidos* (the classical concept of pure, ideal form), the platform operates on a single truth: to extract the exact, metric geometry of a physical object from the noise of reality. 

It democratizes high-fidelity reality capture via edge devices (smartphones, drones) and cloud-accelerated Neural Rendering (3D Gaussian Splatting, SfM). By handling the messy reality of the physical world, Eido ensures that downstream nodes receive only pristine, workable digital primitives.

## 2. Bilingual Brand Identity & Market Positioning
Operating as a global platform engineered in a bilingual hub, Eido’s brand must translate seamlessly across English and Spanish technical registers, maintaining its "noir luxury" and "solarpunk-adjacent" aesthetic in both.

### English Branding: The Essence Extractor
*   **Narrative:** Eido is the Gateway of Form. It isn't just a 3D scanner; it is a deterministic engine that distills physical noise into parametric perfection.
*   **Taglines:**
    *   *Eido: Capture Reality. Command Form.*
    *   *From Messy Reality to Parametric Perfection.*
    *   *The Lens of the Hyperobject.*
*   **Tone:** Authoritative, metric-driven, and structurally focused. 

### Spanish Branding: El Umbral de la Forma
*   **Narrative:** In Spanish, the Greek root *Eidos* taps into a highly elevated, "culto" register. It shifts the platform from a mere utility (*aplicación de escaneo*) to an engineering instrument (*generador de formas exactas*). The open vowels of E-i-d-o make it phonetically frictionless for LATAM and European markets.
*   **Taglines:**
    *   *Eido: Captura la Realidad. Domina la Forma.*
    *   *De la Realidad Tangible a la Perfección Paramétrica.*
    *   *La Lente del Ecosistema.*
*   **Tone:** Sofisticado, enfocado en la ingeniería, e implacable en su precisión. 

## 3. Separation of Concerns (Ecosystem Fit)
Eido maintains strict architectural isolation to prevent monolithic bloat, passing curated data across the suite:

*   **Eido → Janua:** Zero reliance on third-party identity providers. Janua operates as the sovereign Identity Master, managing access controls, API tokens for edge capture, and user authentication across `eido.cam`.
*   **Eido → Blueprint Harvester (`.tube`):** Upon publishing a capture, Eido pushes the canonical mesh, SPZ files, tags, and spatial metadata to the data lake for global indexing and archival.
*   **Eido → Yantra4D (`.io`):** Through the advanced Splat-to-Mesh pipeline, Eido converts volumetric fields into rigid, metric-scaled Boundary Representations. These are streamed directly into the Hyperobjects Commons for parametric enclosure engineering.
*   **Eido → Factlas:** Drone photogrammetry processes into georeferenced 3D Tiles, pushing spatial coordinates directly to the global spatial map.
*   **Eido → CEQ:** Feeds clean, 360-degree turntable renders into ComfyUI workflows for automated marketing generation.

## 4. Core Features
*   **Edge Ingestion Layer:** Native iOS (ARKit/LiDAR) and Android (ARCore/Depth API) apps for capturing localized depth maps, point clouds, and high-res imagery directly at the source.
*   **Cloud-Accelerated 3DGS Pipeline:** Asynchronous, serverless GPU clusters training 3D Gaussian Splats up to 30,000 iterations in minutes.
*   **Material-Aware Splat-to-Mesh Conversion:** Extracts explicit polygon geometry (`.OBJ`, `.GLB`) from Gaussian fields. This is calibrated to handle highly reflective or complex surfaces, ensuring the resulting mesh is watertight and dimensionally accurate enough to be sliced for direct FDM manufacturing (e.g., sending a captured gear straight to a Snapmaker 2 A350 for extrusion in PEEK or TPU).
*   **Declarative WebXR Viewer:** A Next.js and React Three Fiber (R3F) 3D canvas on `eido.cam`. Features a glassmorphic UI, GPU-accelerated radix sorting, progressive loading of compressed SPZ files, and neutral HDRI lighting to showcase the raw ontic form.
*   **Social Portfolio Graph:** Single-table NoSQL database tracking engineer profiles, follower networks, and interactive dimensional annotations pinned to 3D coordinates.

## 5. Architecture & Repo Structure

```text
.
├── apps/
│   ├── web/                 # Next.js portfolio gallery (R3F, Tailwind, Glassmorphism)
│   ├── mobile-ios/          # Swift/Metal ARKit capture app
│   └── mobile-android/      # Kotlin/ARCore capture app
├── services/
│   ├── orchestration/       # Job queue for ephemeral GPU allocation
│   ├── media-prep/          # Stage 0 (CPU): zip/video → frames, GPS EXIF + DJI SRT → geo priors
│   ├── colmap-sfm/          # Structure-from-Motion alignment + GPS georegistration
│   ├── gaussian-splatting/  # CUDA kernels for 3DGS training 
│   ├── splat-to-mesh/       # Poisson surface reconstruction & material extraction
│   └── spz-compress/        # Pure-numpy SPZ v2 encoder (pipeline stage 4)
├── packages/
│   ├── r3f-splat-viewer/    # Internal R3F splat rendering library
│   └── eido-sdk/            # API clients for Yantra4D, Janua, and Blueprint Harvester
├── ops/
│   ├── terraform/           # IaC for S3, CloudFront, and Ephemeral GPU spot instances
│   └── docker/              # Container definitions
├── docs/                    # Architecture Decision Records (ADRs)
├── .env.example
├── Makefile
└── README.md
```

## 6. Quickstart (local development)
**Prereqs:** Node.js 20 and pnpm 9 (`packageManager: pnpm@9.0.0`), Python 3.11,
and Docker for the compose stack.

```bash
git clone https://github.com/madfam-org/eido.git && cd eido
cp .env.example .env

# Web (Next.js 15) on :3000
pnpm install && make dev.web

# API (FastAPI) on :8000
cd apps/api && pip install -e ".[dev]" && cd ../.. && make dev.api

# Or the whole stack in containers
make up
```

Gates, the same as CI:

```bash
cd apps/api && ruff check src/ && mypy src/eido_api/ --ignore-missing-imports && pytest tests/
pytest services/spz-compress/tests/ services/media-prep/tests/
cd apps/web && pnpm lint && pnpm tsc --noEmit && pnpm test
```

Capture mutations need a Janua bearer token, so `make ingest` only works
against an API that is configured with Janua. No sample dataset ships with
this repo.

## 7. APIs & Ecosystem Handoff
Internal syndication webhook fired from Eido to Blueprint Harvester upon successful processing:

```bash
curl -X POST https://api.blueprint.tube/v1/ingest/eido \
  -H 'Authorization: Bearer <JANUA_SERVICE_TOKEN>' \
  -H 'Content-Type: application/json' \
  -d '{
        "eido_id": "edo_9876",
        "author": "usr_123",
        "mesh_url": "https://cdn.eido.cam/assets/edo_9876_clean.glb",
        "splat_url": "https://cdn.eido.cam/assets/edo_9876.spz",
        "license": "CC-BY-4.0",
        "scale_metric": "millimeters"
      }'
```

## 8. Data, Privacy & Cloud Economics
*   **Ephemeral GPU Spot Fleets:** 3DGS training requires heavy VRAM. Eido avoids constant burn-rates by spinning up ephemeral cloud GPU instances strictly on-demand. They process the payload, save the compressed `.spz` and `.glb` files to S3, and terminate immediately.
*   **Compression Engine:** Automatically compresses raw PLY files using spatial clustering and entropy coding, reducing web payloads by up to 90%.
*   **Edge Compute Offloading:** Mobile apps handle localized image cropping, point-cloud sizing, and zip compression locally before hitting the cloud, protecting ingest bandwidth costs.
*   **Privacy & Redaction:** AI-driven blurring microservice runs during the SfM phase to redact PII (faces, license plates) before public publishing.

## 9. Roadmap
*   **Phase 1 (MVP):** iOS LiDAR/Android app, Cloud SfM + 3DGS pipeline, Next.js WebGL gallery on `eido.cam`, Janua Auth integration.
*   **Phase 2 (The Engineering Bridge):** Deploy the automated Splat-to-Mesh pipeline. Establish the API bridge allowing Yantra4D to pull metric-scaled meshes directly from Eido for parametric modeling.
*   **Phase 3 (Temporal & Spatial):** Implement 4D Gaussian Splatting for dynamic motion capture. Enable georeferenced RTK drone integrations to feed city-scale tiles directly to Factlas.
