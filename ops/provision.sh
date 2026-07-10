#!/usr/bin/env bash
# ── Eido Production Provisioning ──────────────────────────────────────────────
# Run ONCE to onboard eido to Enclii and wire eido.cam end-to-end.
# Executed for real on 2026-07-10; every command below matches the actual
# enclii CLI surface (verified against enclii v2026-07) — no aspirational flags.
#
# Topology: eido.cam registered at Porkbun, delegated to Cloudflare NS
# (full Cloudflare edge + Tunnel). Enclii owns tunnel routing for
# eido.cam + api.eido.cam; cdn.eido.cam is a Cloudflare R2 custom domain
# on the eido-cdn-production bucket (NOT an Enclii service route).
#
# Deployment model is GitOps: `enclii onboard` registers an ArgoCD app on
# infra/k8s/production; CI builds+cosign-signs images and pins digests into
# the kustomization; ArgoCD reconciles. CI needs NO ENCLII_TOKEN.
#
# Prerequisites:
#   - enclii CLI installed and logged in (`enclii login`)
#   - gh CLI authenticated for madfam-org/eido
#   - Operator env: CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN (Zone+DNS),
#     PORKBUN_API_KEY, PORKBUN_SECRET_KEY
#   - SECRETS_ENV_FILE: path to a file holding ONLY the runtime secret keys
#     from infra/k8s/production/secrets-template.yaml (never committed).
#     Do NOT point this at an operator .env that carries provider keys.
#
# Enclii-first notes (adapter gaps recorded 2026-07-10):
#   - R2 bucket creation: `enclii providers cloudflare r2` is inspect/plan
#     only; the audited mutate path is the switchyard admin endpoint
#     POST /v1/admin/provision/r2 (used below via authenticated curl).
#   - R2 custom domain attach + R2 S3 token mint: no Enclii adapter and the
#     standard operator CF token lacks R2 scope — dashboard step (documented).
#   - Janua OIDC client registration: no Enclii adapter. Deferred until the
#     web auth-code flow ships; the real endpoint is Janua
#     POST /api/v1/oauth/clients/register with X-Internal-API-Key.

set -euo pipefail

: "${CLOUDFLARE_ACCOUNT_ID:?set CLOUDFLARE_ACCOUNT_ID}"
: "${CLOUDFLARE_API_TOKEN:?set CLOUDFLARE_API_TOKEN (Zone + DNS edit)}"
: "${PORKBUN_API_KEY:?set PORKBUN_API_KEY}"
: "${PORKBUN_SECRET_KEY:?set PORKBUN_SECRET_KEY}"
: "${SECRETS_ENV_FILE:?set SECRETS_ENV_FILE (runtime secrets only — see secrets-template.yaml)}"

ENCLII_API="${ENCLII_API:-https://api.enclii.dev}"

echo "🔭 Eido Production Provisioning"
echo "================================"

# ── Step 1: Cloudflare zone + Porkbun nameserver delegation ───────────────────
# Platform bootstrap (raw provider APIs). Idempotent.
echo ""
echo "Step 1: Cloudflare zone for eido.cam + Porkbun NS delegation"

CF_ZONE_RESP=$(curl -s -X POST "https://api.cloudflare.com/client/v4/zones" \
  -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"eido.cam\",\"account\":{\"id\":\"${CLOUDFLARE_ACCOUNT_ID}\"},\"type\":\"full\"}")
CF_NS=$(echo "$CF_ZONE_RESP" | jq -r '.result.name_servers[]? // empty')
if [ -z "$CF_NS" ]; then
  CF_NS=$(curl -s "https://api.cloudflare.com/client/v4/zones?name=eido.cam" \
    -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" | jq -r '.result[0].name_servers[]')
fi
echo "  Cloudflare nameservers:"; echo "$CF_NS" | sed 's/^/    - /'

NS_JSON=$(echo "$CF_NS" | jq -R . | jq -s '{ns: .}')
curl -s -X POST "https://api.porkbun.com/api/json/v3/domain/updateNs/eido.cam" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg k "$PORKBUN_API_KEY" --arg s "$PORKBUN_SECRET_KEY" --argjson n "$NS_JSON" \
        '{apikey:$k, secretapikey:$s} + $n')" | jq -r '.status'
echo "  ✅ Porkbun delegated to Cloudflare. Propagation can take up to ~1h."

# ── Step 2: Enclii project + service registration ─────────────────────────────
echo ""
echo "Step 2: Enclii project + services (from enclii.yaml)"
enclii projects create --name "Eido" --slug eido 2>/dev/null || echo "  (project eido already exists)"
enclii services-sync --dir . --project eido
echo "  ✅ eido-api / eido-web / eido-orchestration registered."

# ── Step 3: Managed data stores (own instances — NOT the shared CNPG set) ─────
echo ""
echo "Step 3: Postgres addon (Redis runs in-namespace via the manifests)"
echo "  As-built 2026-07-10: eido-db, plan standard-1, namespace project-0be6ce5e."
echo "    enclii addon plans      # postgres plans only — no redis plans exist"
echo "    enclii addon create eido-db --plan standard-1 --project eido"
echo "    enclii addon ls --project eido"
echo "    GET /v1/addons/<id>/credentials   # DATABASE_URL → ${SECRETS_ENV_FILE}"
echo "  REDIS_URL is redis://eido-redis.eido.svc.cluster.local:6379/0"
echo "  (infra/k8s/production/redis.yaml — transient queue, no shared-Redis coupling)."
echo "  ⚠️  The addon namespace must appear in network-policies.yaml egress (it does)."

# ── Step 4: R2 buckets (via switchyard admin endpoint) ────────────────────────
echo ""
echo "Step 4: R2 buckets (audited server-side path)"
ENCLII_TOKEN_HDR="Authorization: Bearer ${ENCLII_API_TOKEN:?run: export ENCLII_API_TOKEN=<personal token> (enclii tokens create)}"
for BUCKET in eido-raw-production eido-cdn-production; do
  curl -s -X POST "${ENCLII_API}/v1/admin/provision/r2" \
    -H "${ENCLII_TOKEN_HDR}" -H "Content-Type: application/json" \
    -d "{\"namespace\":\"eido\",\"bucket_name\":\"${BUCKET}\"}" | jq -c .
done
echo "  Dashboard steps (no API path with standard operator token):"
echo "    1. R2 → eido-cdn-production → Settings → Custom Domains → add cdn.eido.cam"
echo "    2. R2 → Manage API tokens → mint Object R/W token for both buckets;"
echo "       put S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY / S3_ENDPOINT"
echo "       (https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com) into ${SECRETS_ENV_FILE}."

# ── Step 5: Onboard (namespace, ArgoCD app, GHCR creds, eido-secrets) ─────────
# Namespace defaults to the project slug (eido) — matches the manifests.
echo ""
echo "Step 5: Enclii onboarding (GitOps registration)"
enclii onboard \
  --repo madfam-org/eido \
  --project eido \
  --manifest-path infra/k8s/production \
  --skip-postgres \
  --secrets-file "${SECRETS_ENV_FILE}" \
  --secret-name eido-secrets
echo "  ✅ ArgoCD app registered on infra/k8s/production; eido-secrets created."

# ── Step 6: Tunnel domains ────────────────────────────────────────────────────
echo ""
echo "Step 6: Enclii tunnel domains (proxied CNAME → tunnel)"
enclii domains add eido.cam     --service eido-web --env production
enclii domains add api.eido.cam --service eido-api --env production
# cdn.eido.cam intentionally NOT here — it is the R2 custom domain (Step 4).
echo "  ✅ eido.cam + api.eido.cam routed through the Cloudflare Tunnel."

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "🚀 Provisioning complete."
echo ""
echo "Next:"
echo "  1. Merge to main with green CI → images build + cosign-sign → digests pinned."
echo "  2. ArgoCD reconciles infra/k8s/production onto the cluster."
echo "  3. Verify:  curl https://api.eido.cam/health   → {\"status\":\"healthy\"}"
echo "             curl https://eido.cam/api/health    → {\"status\":\"healthy\"}"
echo "  4. Logs:    enclii logs eido-api -f"
echo ""
echo "Deferred (post-launch): Janua OIDC client for eido-web — register via"
echo "Janua POST /api/v1/oauth/clients/register (X-Internal-API-Key) when the"
echo "web login flow ships; put janua-client-secret into eido-secrets."
