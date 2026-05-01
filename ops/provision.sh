#!/usr/bin/env bash
# ── Eido Production Provisioning Runbook ──────────────────────────────────────
# Run this ONCE to onboard eido to the Enclii platform and configure DNS.
# Prerequisites: enclii CLI installed and logged in (`enclii login`)
#
# Porkbun domain API: eido.cam already enabled for API access.
# Cloudflare Tunnel routes are provisioned by `enclii domains add` automatically.

set -euo pipefail

echo "🔭 Eido Production Provisioning"
echo "================================"

# ── Step 1: Enclii Onboard ────────────────────────────────────────────────────
# Creates namespace, ArgoCD app, Cloudflare tunnel routes, Janua OIDC client,
# and NetworkPolicies in one shot.
echo ""
echo "Step 1: Enclii onboarding (one-shot)..."

enclii onboard \
  --repo madfam-org/eido \
  --service eido-api \
  --db-name eido \
  --secrets-file .env

echo "✅ Onboarding complete."

# ── Step 2: Provision Secrets ─────────────────────────────────────────────────
echo ""
echo "Step 2: Provisioning secrets into Lockbox..."
echo "  ⚠️  Fill the values below from .env / 1Password before running."

enclii secrets set database-url="${DATABASE_URL}"              --service eido-api --secret
enclii secrets set redis-url="${REDIS_URL}"                    --service eido-api --secret
enclii secrets set r2-access-key-id="${S3_ACCESS_KEY_ID}"      --service eido-api --secret
enclii secrets set r2-secret-access-key="${S3_SECRET_ACCESS_KEY}" --service eido-api --secret
enclii secrets set api-secret-key="${API_SECRET_KEY}"          --service eido-api --secret
enclii secrets set vast-api-key="${VAST_API_KEY}"              --service eido-orchestration --secret
enclii secrets set janua-client-secret="${JANUA_CLIENT_SECRET}" --service eido-web --secret

echo "✅ Secrets provisioned."

# ── Step 3: DNS — eido.cam via Porkbun API ───────────────────────────────────
echo ""
echo "Step 3: Configuring DNS via Porkbun API..."
echo "  Enclii handles Cloudflare Tunnel routing automatically after domain add."

# Get the Cloudflare Tunnel ingress hostname from Enclii
TUNNEL_HOSTNAME=$(enclii junctions list eido-api --output json | jq -r '.[0].tunnel_hostname')

echo "  Tunnel hostname: ${TUNNEL_HOSTNAME}"

# Porkbun API: create CNAME records for eido.cam and api.eido.cam
PORKBUN_API_KEY="${PORKBUN_API_KEY:-$(enclii secrets get porkbun-api-key 2>/dev/null || echo "MISSING")}"
PORKBUN_SECRET="${PORKBUN_SECRET_KEY:-$(enclii secrets get porkbun-secret-key 2>/dev/null || echo "MISSING")}"

for SUBDOMAIN in "" "api" "cdn"; do
  RECORD_NAME="${SUBDOMAIN}"
  if [ -z "$RECORD_NAME" ]; then
    RECORD_NAME="@"
    CONTENT="${TUNNEL_HOSTNAME}"
  else
    CONTENT="${TUNNEL_HOSTNAME}"
  fi

  echo "  Creating CNAME: ${RECORD_NAME}.eido.cam → ${CONTENT}"
  curl -s -X POST "https://porkbun.com/api/json/v3/dns/create/eido.cam" \
    -H "Content-Type: application/json" \
    -d "{
      \"apikey\": \"${PORKBUN_API_KEY}\",
      \"secretapikey\": \"${PORKBUN_SECRET}\",
      \"name\": \"${RECORD_NAME}\",
      \"type\": \"CNAME\",
      \"content\": \"${CONTENT}\",
      \"ttl\": \"300\"
    }" | jq .
done

echo "✅ DNS records created. Propagation typically completes within 2 minutes."

# ── Step 4: Add domains to Enclii tunnel routing ─────────────────────────────
echo ""
echo "Step 4: Registering domains in Enclii tunnel..."

enclii domains add eido-web    eido.cam
enclii domains add eido-api    api.eido.cam
enclii domains add eido-api    cdn.eido.cam

echo "✅ Domains registered."

# ── Step 5: Add ENCLII_TOKEN to GitHub Actions secrets ───────────────────────
echo ""
echo "Step 5: Setting GitHub Actions deployment secret..."
ENCLII_TOKEN=$(enclii token create --name "github-actions-eido" --output raw)

gh secret set ENCLII_TOKEN \
  --repo madfam-org/eido \
  --body "${ENCLII_TOKEN}"

echo "✅ ENCLII_TOKEN set in GitHub Actions."

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "🚀 Provisioning complete!"
echo ""
echo "Next steps:"
echo "  1. Push to main → CI builds images → Enclii deploys automatically"
echo "  2. Monitor: enclii logs eido-api -f"
echo "  3. Verify: curl https://api.eido.cam/health"
echo "  4. Gallery: https://eido.cam"
