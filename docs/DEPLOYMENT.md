# Eido Deployment & Provisioning

> Last Updated: 2026-07-09
>
> Enclii-first. Every production mutation goes through the Enclii web/API/CLI;
> the raw Cloudflare/Porkbun/R2 calls in `ops/provision.sh` are one-time
> platform bootstrap. Secret **values** never live in git — only names.

## Current readiness (honest)

Two layers, very different states:

- **Deploy/provisioning wiring — ready to run.** `.enclii.yml` defines all three
  services; `ops/provision.sh` onboards them and wires DNS/R2/secrets. Once an
  operator runs provisioning and CI is green, the API and web **shells** go live.
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
| — | GPU pipeline dispatcher (`eido-orchestration`) | Enclii worker → Vast.ai GPUs |

DNS: `eido.cam` is registered at **Porkbun** and delegated to **Cloudflare**
nameservers (full edge + Tunnel), matching the rest of the ecosystem.

Dependencies: PostgreSQL (`eido`, Enclii-provisioned — eido is *not* on the
shared CNPG cluster), Redis (job queue), Cloudflare R2 (two buckets), Janua
(OIDC/JWKS), Selva (inference), Dhanam (entitlements), Vast.ai (GPU).

## Provisioning (one-shot)

Run `ops/provision.sh` with operator credentials in the environment. It:

1. Creates the Cloudflare zone and points Porkbun's nameservers at it.
2. Creates the R2 buckets and attaches `cdn.eido.cam` as a public custom domain.
3. `enclii onboard` — namespace, ArgoCD app, Tunnel, NetworkPolicies, Postgres, Redis.
4. Registers the `eido-web` Janua OIDC client.
5. Provisions secrets (below) into Enclii's Vault-backed store.
6. `enclii domains add` for `eido.cam` and `api.eido.cam`.
7. Sets `ENCLII_TOKEN` in GitHub Actions so the deploy job can roll out.

Then push to `main`: CI builds images and Enclii deploys all three services.
**Deploy is gated on green CI** (`test-api` + `test-web`).

### Secret names (`eido-secrets`)

| Key | Service(s) | Notes |
|-----|-----------|-------|
| `database-url` | api | Postgres DSN (`postgresql+asyncpg://…`) |
| `redis-url` | api, orchestration | Job queue |
| `r2-access-key-id` / `r2-secret-access-key` | api, orchestration | R2 API token |
| `r2-endpoint` | api, orchestration | `https://<cf-account-id>.r2.cloudflarestorage.com` |
| `internal-api-token` | api, orchestration | Worker → API status callback; identical on both |
| `janua-client-secret` | web | eido-web OIDC confidential client |
| `vast-api-key` | orchestration | GPU fleet |
| `api-secret-key` | api | Currently unused by app code (vestigial) |

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
