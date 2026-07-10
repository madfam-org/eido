# Eido Deployment & Provisioning

> Last Updated: 2026-07-10
>
> Enclii-first. Every production mutation goes through the Enclii web/API/CLI;
> the raw Cloudflare/Porkbun calls in `ops/provision.sh` are one-time platform
> bootstrap. Secret **values** never live in git — only names.

## Current readiness (honest)

Two layers, very different states:

- **Deploy/provisioning wiring — executed 2026-07-10.** `enclii.yaml` registers
  the three services; `infra/k8s/production/` carries the deployed manifests
  (ArgoCD-reconciled); `ops/provision.sh` is the as-built provisioning record.
  The API and web **shells** go live with green CI on `main`.
- **Product — pre-alpha (~15–20%).** The web 3D viewer is a placeholder, and the
  capture pipeline does not yet produce output (the 3DGS stage is a stub and the
  `.spz` compression step is missing), so captures never reach `READY`. Bringing
  a *working* product live is engineering beyond provisioning — see
  [Gaps](#gaps-blocking-a-working-product).

## Topology

| Surface | Serves | Backed by |
|---------|--------|-----------|
| `eido.cam` | Next.js gallery (`eido-web`, :3000) | Enclii → Cloudflare Tunnel |
| `api.eido.cam` | FastAPI (`eido-api`, :8000) | Enclii → Cloudflare Tunnel |
| `cdn.eido.cam` | Splat/mesh/thumbnail assets | Cloudflare **R2 custom domain** (`eido-cdn-production`) — *not* the API |
| — | GPU pipeline dispatcher (`eido-orchestration`) | Redis-queue worker → Vast.ai GPUs |

DNS: `eido.cam` is registered at **Porkbun** and delegated to **Cloudflare**
nameservers (full edge + Tunnel), matching the rest of the ecosystem.

Dependencies: PostgreSQL (Enclii **addon** `eido-db` — own instance, eido is
*not* on the shared CNPG set), Redis (in-namespace `eido-redis`, transient
queue — see `infra/k8s/production/redis.yaml`), Cloudflare R2 (two buckets),
Janua (OIDC/JWKS), Selva (inference), Dhanam (entitlements), Vast.ai (GPU).

## Deployment model (GitOps)

CI builds the three images, **cosign-signs** them (keyless, GH Actions OIDC —
the `eido` namespace enforces `verify-image-signatures`), then pins all three
digests into `infra/k8s/production/kustomization.yaml` in a single commit.
ArgoCD (registered by `enclii onboard`) reconciles that directory onto the
cluster. CI holds **no cluster credentials** — there is no `ENCLII_TOKEN`.

The digest-pin commit is excluded from CI triggers (`paths-ignore`), which
breaks the build→pin→build loop.

## Provisioning (one-shot, as-built 2026-07-10)

`ops/provision.sh` documents the exact sequence against the real enclii CLI:

1. Cloudflare zone for `eido.cam` + Porkbun NS delegation (bootstrap, raw API).
2. `enclii projects create` + `enclii services-sync` (reads `enclii.yaml`).
3. Postgres **addon** (`enclii addon create eido-db --plan standard-1
   --project eido`; as-built namespace `project-0be6ce5e`) — credentials go
   into the secrets env file; the addon namespace is in
   `network-policies.yaml` egress. Redis is in-namespace (`redis.yaml`) — no
   Redis addon plans exist and the shared data-namespace Redis would couple
   eido to another product's password.
4. R2 buckets via the switchyard admin endpoint (`POST /v1/admin/provision/r2`);
   the `cdn.eido.cam` custom-domain attach and the R2 S3 token mint are
   dashboard steps (Enclii adapter gap, recorded).
5. `enclii onboard --repo madfam-org/eido --project eido
   --manifest-path infra/k8s/production --skip-postgres
   --secrets-file <secrets.env> --secret-name eido-secrets` — namespace,
   ArgoCD app, GHCR pull creds, `eido-secrets`.
6. `enclii domains add eido.cam --service eido-web --env production` and
   `api.eido.cam --service eido-api`.

Then merge to `main` with green CI (`test-api` + `test-web` gate the builds).

### Secret names (`eido-secrets`)

Keys are UPPERCASE env names consumed via `envFrom` (see
`infra/k8s/production/secrets-template.yaml`):

| Key | Service(s) | Notes |
|-----|-----------|-------|
| `DATABASE_URL` | api | Postgres DSN (`postgresql+asyncpg://…`, addon) |
| `REDIS_URL` | api, orchestration | Job queue (addon) |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | api, orchestration | R2 token (Object R/W, both buckets) |
| `S3_ENDPOINT` | api, orchestration | `https://<cf-account-id>.r2.cloudflarestorage.com` (account-specific) |
| `INTERNAL_API_TOKEN` | api, orchestration | Worker → API status callback; **identical on both** |
| `API_SECRET_KEY` | api | Reserved (not yet read by app code) |
| `VAST_API_KEY` | orchestration | GPU fleet |
| `JANUA_CLIENT_SECRET` | web | Deferred — register the `eido-web` OIDC client via Janua `POST /api/v1/oauth/clients/register` (X-Internal-API-Key) when the web login flow ships |

## Verify

```bash
curl https://api.eido.cam/health   # {"status":"healthy"}
curl https://eido.cam/api/health    # {"status":"healthy"}
# https://eido.cam                   → gallery
enclii logs eido-api -f
```

## Gaps blocking a working product

Provisioning brings up *shells*. A full-fledged product additionally needs:

1. **Real 3DGS training** — `services/gaussian-splatting` is a stub; wire a real
   trainer and produce a point cloud.
2. **`.spz` compression** — the worker uploads `output.spz` that no stage creates,
   so captures never reach `READY`. This is the single blocker to an end-to-end run.
3. **Real splat viewer** — the web viewer renders placeholder geometry; wire a
   `.spz`/gsplat viewer and an upload flow (`/capture/new`).
4. **Auth on capture mutations + a Janua service token for handoffs** — tracked in
   the auth PR; the eido→sibling hand-offs need an M2M token.
