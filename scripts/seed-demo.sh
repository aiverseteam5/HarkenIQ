#!/usr/bin/env bash
# Seed the full-stack demo (QA-014): tenant -> site registration, using
# REAL authentication (no insecure bypass). Run after:
#   cd deploy/full-stack && docker compose up -d      # wait until healthy
#
# Idempotent: safe to re-run (409/existing responses are tolerated).
set -euo pipefail

KEYCLOAK=${KEYCLOAK:-http://localhost:8180}
CONSOLE=${CONSOLE:-http://localhost:8100}
CC=${CC:-http://localhost:8090}
ADMIN_USER=${ADMIN_USER:-admin@harkeniq.com}
ADMIN_PASS=${ADMIN_PASS:-admin}

echo "==> Minting a platform-admin token from Keycloak"
TOKEN=$(curl -sf -X POST \
  "$KEYCLOAK/realms/harkeniq-platform/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=harkeniq-console&username=$ADMIN_USER&password=$ADMIN_PASS" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
AUTH="Authorization: Bearer $TOKEN"

echo "==> Creating demo tenant (slug tenant-demo)"
curl -s -X POST "$CONSOLE/api/admin/tenants/" -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"name": "Demo Datacenter Co", "slug": "tenant-demo",
       "billing_country": "US", "currency": "USD", "plan": "approve",
       "node_commit": 10, "admin_email": "owner@demo.example"}' \
  | python3 -m json.tool | head -6 || true

echo "==> Registering site-1's Site Manager with Central Command"
curl -s -X POST "$CC/api/sites/register" -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"site_name": "site-1", "sm_endpoint": "site-manager:50051",
       "license_fingerprint": "demo"}' \
  | python3 -m json.tool | head -6 || true

echo
echo "Seeded. Where to look:"
echo "  Console (L4):      $CONSOLE      login $ADMIN_USER / $ADMIN_PASS"
echo "  Central Command:   $CC/api/fleet/ (Bearer token)"
echo "  Site Manager (L2): http://localhost:8080  token: dev-token-sm"
echo "  Keycloak:          $KEYCLOAK (admin/admin)"
echo
echo "Inject a fault to light up the pipeline:"
echo "  curl -sk -X POST https://localhost:9000/test/inject-fault \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"fault_type\":\"fan\",\"target\":\"Fan1A\",\"params\":{\"health\":\"Critical\",\"speed_rpm\":0}}'"
