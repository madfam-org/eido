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

Reality-capture platform: photo/video uploads → COLMAP SfM → 3D Gaussian
Splatting → mesh/`.spz` artifacts → WebGL gallery at eido.cam. Three deployed
services (`eido-api`, `eido-web`, `eido-orchestration`) plus ephemeral GPU
pipeline containers (`services/colmap-sfm`, `services/gaussian-splatting`,
`services/splat-to-mesh`) dispatched via Vast.ai. Honest status lives in
`README.md` and `docs/DEPLOYMENT.md` — keep it truthful; this product is
pre-alpha and docs must not claim otherwise.

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
  and pins digests into `infra/k8s/production/kustomization.yaml` in one
  commit. ArgoCD (registered via `enclii onboard`) reconciles that directory.
  CI holds no cluster credentials.
- `enclii.yaml` registers the services with Enclii (`services-sync`);
  `infra/k8s/production/` is the deployed truth. Keep both in sync when
  changing ports, probes, or resources.
- One-time provisioning: `ops/provision.sh` (as-built 2026-07-10). Domains:
  eido.cam + api.eido.cam via Enclii tunnel routes; cdn.eido.cam is an R2
  custom domain, never an Enclii route.

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
- Web: `cd apps/web && pnpm lint && pnpm tsc --noEmit`
- Prod: `curl https://api.eido.cam/health` and `https://eido.cam/api/health`
