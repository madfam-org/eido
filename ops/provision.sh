#!/usr/bin/env bash
# ── Eido Production Provisioning ──────────────────────────────────────────────
# Run ONCE to onboard eido to Enclii and wire eido.cam end-to-end.
#
# Topology: eido.cam is registered at Porkbun, delegated to Cloudflare
# nameservers (full Cloudflare edge + Tunnel), matching the rest of the
# ecosystem. Enclii drives Cloudflare Tunnel routing; cdn.eido.cam is served by
# a Cloudflare R2 custom domain (NOT the API).
#
# Prerequisites:
#   - enclii CLI installed and logged in (`enclii login`)
#   - gh CLI authenticated for madfam-org/eido
#   - A populated .env (see .env.example) — secret VALUES never live in git
#   - Cloudflare + Porkbun API credentials available to the operator
#
# This script is Enclii-first: every production mutation goes through the enclii
# CLI. Raw Cloudflare/Porkbun/R2 API calls here are one-time platform bootstrap.

set -euo pipefail

: "${CLOUDFLARE_ACCOUNT_ID:?set CLOUDFLARE_ACCOUNT_ID}"
: "${CLOUDFLARE_API_TOKEN:?set CLOUDFLARE_API_TOKEN (Zone + R2 + DNS edit)}"
: "${PORKBUN_API_KEY:?set PORKBUN_API_KEY}"
: "${PORKBUN_SECRET_KEY:?set PORKBUN_SECRET_KEY}"

echo "🔭 Eido Production Provisioning"
echo "================================"

# ── Step 1: Cloudflare zone + Porkbun nameserver delegation ───────────────────
echo ""
echo "Step 1: Cloudflare zone for eido.cam + Porkbun NS delegation"

# Create the zone in Cloudflare (idempotent: ignore 'already exists').
CF_ZONE_RESP=$(curl -s -X POST "https://api.cloudflare.com/client/v4/zones" \
  -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"eido.cam\",\"account\":{\"id\":\"${CLOUDFLARE_ACCOUNT_ID}\"},\"type\":\"full\"}")
CF_NS=$(echo "$CF_ZONE_RESP" | jq -r '.result.name_servers[]? // empty')
if [ -z "$CF_NS" ]; then
  # Zone likely already exists — read its assigned nameservers.
  CF_NS=$(curl -s "https://api.cloudflare.com/client/v4/zones?name=eido.cam" \
    -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" | jq -r '.result[0].name_servers[]')
fi
echo "  Cloudflare nameservers:"; echo "$CF_NS" | sed 's/^/    - /'

# Point Porkbun at Cloudflare's nameservers (this is the "connect Porkbun" step).
NS_JSON=$(echo "$CF_NS" | jq -R . | jq -s '{ns: .}')
curl -s -X POST "https://porkbun.com/api/json/v3/domain/updateNs/eido.cam" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg k "$PORKBUN_API_KEY" --arg s "$PORKBUN_SECRET_KEY" --argjson n "$NS_JSON" \
        '{apikey:$k, secretapikey:$s} + $n')" | jq .
echo "  ✅ Porkbun delegated to Cloudflare. Propagation can take up to ~1h."

# ── Step 2: Object storage — Cloudflare R2 ───────────────────────────────────
echo ""
echo "Step 2: Cloudflare R2 buckets + cdn.eido.cam custom domain"
for BUCKET in eido-raw-production eido-cdn-production; do
  curl -s -X POST "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/r2/buckets" \
    -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"${BUCKET}\"}" | jq -r '.success as $ok | "  \($ok) — \"'"${BUCKET}"'\""'
done
# Attach cdn.eido.cam as a public custom domain on the CDN bucket (public read).
curl -s -X POST "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/r2/buckets/eido-cdn-production/domains/custom" \
  -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"domain":"cdn.eido.cam","enabled":true}' | jq '.success'
echo "  ✅ R2 ready. Generate an R2 API token (Object R/W) for eido-raw/eido-cdn;"
echo "     its endpoint is https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"

# ── Step 3: Enclii onboard (namespace, ArgoCD, Tunnel, NetworkPolicies) ───────
echo ""
echo "Step 3: Enclii onboarding (one-shot)"
enclii onboard \
  --repo madfam-org/eido \
  --project madfam-platform \
  --service eido-api \
  --service eido-web \
  --service eido-orchestration \
  --db-name eido \
  --with-redis \
  --secrets-file .env
echo "  ✅ Onboarding complete (Postgres 'eido' + Redis provisioned by Enclii)."

# ── Step 4: Janua OIDC clients ───────────────────────────────────────────────
# The API validates Janua RS256 tokens (no client secret needed to verify).
# The web app is a confidential OIDC client and needs its own registration.
echo ""
echo "Step 4: Janua OIDC clients"
enclii janua client create \
  --name eido-web \
  --redirect-uri "https://eido.cam/api/auth/callback/janua" \
  --grant authorization_code --grant refresh_token
echo "  ✅ eido-web OIDC client registered (secret → eido-secrets/janua-client-secret)."

# ── Step 5: Secrets (names only; values come from .env / 1Password) ───────────
echo ""
echo "Step 5: Provisioning secrets into Enclii (Vault-backed)"
enclii secrets set database-url="${DATABASE_URL}"                --service eido-api --secret
enclii secrets set redis-url="${REDIS_URL}"                      --service eido-api --secret
enclii secrets set r2-access-key-id="${S3_ACCESS_KEY_ID}"        --service eido-api --secret
enclii secrets set r2-secret-access-key="${S3_SECRET_ACCESS_KEY}" --service eido-api --secret
enclii secrets set r2-endpoint="https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com" --service eido-api
enclii secrets set api-secret-key="${API_SECRET_KEY}"            --service eido-api --secret
enclii secrets set internal-api-token="${INTERNAL_API_TOKEN}"    --service eido-api --secret
enclii secrets set janua-client-secret="${JANUA_CLIENT_SECRET}"  --service eido-web --secret
# The worker shares the same eido-secrets; ensure the callback token + R2 keys resolve there too.
enclii secrets set internal-api-token="${INTERNAL_API_TOKEN}"    --service eido-orchestration --secret
enclii secrets set vast-api-key="${VAST_API_KEY}"               --service eido-orchestration --secret
echo "  ✅ Secrets provisioned."

# ── Step 6: Register tunnel domains in Cloudflare (via Enclii) ────────────────
echo ""
echo "Step 6: Enclii tunnel domains (proxied CNAME → tunnel, created in Cloudflare)"
enclii domains add eido-web  eido.cam
enclii domains add eido-api  api.eido.cam
# NOTE: cdn.eido.cam is intentionally NOT added here — it is the R2 custom
# domain from Step 2, not an Enclii service route.
echo "  ✅ eido.cam + api.eido.cam routed through the Cloudflare Tunnel."

# ── Step 7: GitHub Actions deploy token ──────────────────────────────────────
echo ""
echo "Step 7: GitHub Actions deploy secret"
ENCLII_TOKEN=$(enclii token create --name "github-actions-eido" --output raw)
gh secret set ENCLII_TOKEN --repo madfam-org/eido --body "${ENCLII_TOKEN}"
echo "  ✅ ENCLII_TOKEN set (CI deploy job can now roll out on push to main)."

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "🚀 Provisioning complete."
echo ""
echo "Next:"
echo "  1. Ensure CI is green on main (deploy is gated on test-api + test-web)."
echo "  2. Push to main → CI builds images → Enclii deploys all three services."
echo "  3. Verify:  curl https://api.eido.cam/health   → {\"status\":\"healthy\"}"
echo "             curl https://eido.cam/api/health    → {\"status\":\"healthy\"}"
echo "             https://eido.cam                    → gallery"
echo "  4. Logs:    enclii logs eido-api -f"
