# Product Requirements Document (PRD): Eido
**Domains:** `eido.cam` (Product/Gallery) | `eidocam.com` (Redirect)
**Entity:** Innovaciones MADFAM SAS de CV

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
│   ├── colmap-sfm/          # Structure-from-Motion alignment containers
│   ├── gaussian-splatting/  # CUDA kernels for 3DGS training 
│   └── splat-to-mesh/       # Poisson surface reconstruction & material extraction
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

## 6. Quickstart (Local Ingestion & Testing)
**Prereqs:** Node.js 18+, Docker, AWS CLI configured.

```bash
# 1) Clone the repository
git clone https://gitlab.com/madfam/eido-cloud.git
cd eido-cloud

# 2) Configure environment (link to Janua auth and Blueprint Harvester)
cp .env.example .env

# 3) Boot the local web viewer and mock processing queue
make dev.up

# 4) Submit a test dataset to the local ingestion pipeline
make test.ingest dataset=./sample-data/mechanical-bracket/

# 5) Open the portfolio gallery
open http://localhost:3000/portfolio/local-dev
```

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
