# Eido Agent Operating Guide

> [!IMPORTANT] MADFAM-ENCLII-FIRST v1: Routine production operations must use
> Enclii web, API, or CLI. Treat raw `kubectl`, `helm`, SSH, provider CLI/API,
> `docker exec`, and direct container access as platform bootstrap or
> documented break-glass only, and record any missing Enclii adapter gap.

<!-- MADFAM-AGENTS-CANONICAL v1 -->

This is the canonical instruction file for Claude, Codex, and any other LLM
agent working in this repository. `CLAUDE.md` is a compatibility redirect and
should not become the source of truth again.

## What Eido is

Reality-capture platform: photo/video uploads → media-prep → COLMAP SfM →
3D Gaussian Splatting → mesh/`.spz` artifacts → WebGL gallery at eido.cam.
Three deployed services (`eido-api`, `eido-web`, `eido-orchestration`), the
CPU-only `services/media-prep` stage, plus ephemeral GPU pipeline containers
(`services/colmap-sfm`, `services/gaussian-splatting`,
`services/splat-to-mesh`) dispatched via Vast.ai. Honest status lives in
`README.md` and `docs/DEPLOYMENT.md` — keep it truthful; this product is
pre-alpha and docs must not claim otherwise.

**Eido owns georegistration.** It is the only product holding both the camera
poses and the per-image GPS priors, so it — not Factlas — turns a capture into
an earth-framed artifact: `services/media-prep` reads GPS EXIF and DJI SRT
telemetry, and `colmap-sfm` runs `model_aligner` against those priors. Factlas
receives *located facts* and never derives location from pixels. Ratified in
internal-devops ADR-005; do not move this concern across that boundary.

Two distinctions the code depends on:

- **georeferenced** (the capture has coordinates) is not **georegistered** (its
  model was actually aligned to them). Conflating them lets an un-anchored model
  pass as anchored.
- The footprint is a **`capture_envelope`** — the ground area the capture
  observed, from the hull of the camera positions. It is not a cadastral parcel
  or a building outline, and must not be described as one.

## Required operating doctrine

- Treat capture uploads, pipeline job dispatch, GPU instance provisioning
  (Vast.ai spends real money), R2 object writes, database migrations,
  production smoke checks, and deploys as side-effectful operations. Do not
  run them against production unless the user explicitly requests the action
  and names the target environment.
- Never commit secret VALUES. `infra/k8s/production/secrets-template.yaml`
  documents secret NAMES only; runtime values are provisioned by the operator
  through Enclii (`eido-secrets`). `.env` files stay local and gitignored.
- GPU budget guardrails (`GPU_MAX_HOURLY_SPEND`, `GPU_MIN_VRAM_GB`) exist to
  bound spend — never raise or bypass them to "make a job pass".
- `INTERNAL_API_TOKEN` must remain the identical value on `eido-api` and
  `eido-orchestration` (worker→API status callback). Changing one side breaks
  the pipeline silently.
- Auth is Janua RS256 via JWKS (`https://auth.madfam.io`). HS256 is
  fail-closed by design; do not re-enable self-issued tokens.
- Prefer existing repo conventions, scripts, and docs over new patterns.
  Preserve user work; never revert unrelated changes.

## Deployment model (GitOps)

- CI (`.github/workflows/ci.yml`) builds the three images, cosign-signs them
  (keyless, GH OIDC — the `eido` namespace enforces signature verification),
  and pins the digests into the raw `infra/k8s/production/*-deployment.yaml`
  files (not a kustomization `images:` block) in one `[skip ci]` commit.
  ArgoCD (registered via `enclii onboard`) reconciles that directory. CI holds
  no cluster credentials. Every merge to `main` rebuilds and redeploys all
  three services, docs-only merges included (there are no path filters).
- GitHub-hosted jobs are pinned to `ubuntu-24.04` (#24), ahead of
  `ubuntu-latest` moving to Ubuntu 26 on 2026-10-19. Do not switch them back to
  `ubuntu-latest`.
- `enclii.yaml` registers the services with Enclii (`services-sync`);
  `infra/k8s/production/` is the deployed truth. Keep both in sync when
  changing ports, probes, or resources.
- One-time provisioning: `ops/provision.sh` (as-built 2026-07-10). Domains:
  eido.cam + api.eido.cam via Enclii tunnel routes; cdn.eido.cam is an R2
  custom domain, never an Enclii route.

## Web stack and the image-optimizer invariant

- `apps/web` is Next.js 15.5.27 + React 19 (19.3.0 in the lockfile),
  `@react-three/fiber` 9, `@react-three/drei` 10 (#25). Any R3F/drei bump must
  stay inside fiber 9.8's peer range (`react >=19 <19.4`).
- **Security invariant (GHSA-2xp9-vwfh-vxw4):** `images.unoptimized: true`, an
  exact `remotePatterns` allow-list (`https://cdn.eido.cam`, default port), no
  `domains`, and `/_next/image` answers 404. Nothing may import `next/image`
  without revisiting this. Enforced by `apps/web/tests/next-config.test.mjs`
  (`pnpm test`, CI step "Config guard").
- **Known bug, not yet fixed:** the CSP `connect-src` blocks drei's
  `<Environment preset="studio">` HDR (fetched from `raw.githack.com`), so
  `/capture/[id]` stays on its `<Loader />` fallback. Fix it by self-hosting
  the HDR, not by allowing the third-party origin in the CSP. Details are in
  `README.md` → Known issues.
- API dependency notes: `sqlalchemy` is pinned `<2.1`. Token verification is
  still `python-jose`, not PyJWT; porting it is open.

## Repo entrypoints

- `README.md` — product overview and honest status
- `docs/DEPLOYMENT.md` — deployment/provisioning runbook
- `apps/api` — FastAPI (auth, captures, health) · `apps/web` — Next.js gallery
- `services/orchestration/worker.py` — Redis-queue pipeline dispatcher
- `infra/k8s/production/` — deployed manifests (ArgoCD-reconciled)
- Private ops/audit history: `madfam-org/internal-devops` (secret NAMES only
  there too; product repo carries only sanitized code)

## Verification

- API: `cd apps/api && ruff check src/ && mypy src/eido_api/ --ignore-missing-imports && pytest tests/`
- Pipeline stages: `pytest services/spz-compress/tests/ services/media-prep/tests/`
- Web: `cd apps/web && pnpm lint && pnpm tsc --noEmit && pnpm test`
- No test is skipped or marked flaky. `apps/web` has no component tests; its
  only test is the config guard.
- Prod: `curl https://api.eido.cam/health` and `https://eido.cam/api/health`

## Related repositories and contracts

- Janua JWKS, issuer and audience rules:
  https://github.com/madfam-org/janua/blob/main/docs/guides/ECOSYSTEM_INTEGRATION.md
  · M2M service tokens:
  https://github.com/madfam-org/janua/blob/main/docs/service-tokens.md
- Enclii onboarding, GitOps, signed digests and Kyverno policies:
  https://github.com/madfam-org/enclii/blob/main/docs/cli/commands/onboard.md
  · https://github.com/madfam-org/enclii/blob/main/docs/infrastructure/GITOPS.md
  · https://github.com/madfam-org/enclii/blob/main/docs/runbooks/SIGNED_GITOPS_DIGESTS.md
  · https://github.com/madfam-org/enclii/blob/main/docs/infrastructure/KYVERNO_POLICIES.md
- Factlas observation hand-off: eido's side is
  `apps/api/contracts/observation.v1.json`, pinned by
  `apps/api/tests/test_observation_contract.py`.
