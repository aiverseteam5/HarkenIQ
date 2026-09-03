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
# A23-5: the tenant's owner is now its founding administrator -- Central
# Command seeds the first `role.manage` grant on the subject the Console
# recorded (A23.14 D4). The demo used to create the Console owner as
# `owner@demo.example` and then mint a SEPARATE Keycloak user for every
# CC call, which after strict birth would leave the identity the demo
# actually uses holding no grant at all. They are one identity now.
TENANT_REALM=${TENANT_REALM:-tenant-demo}
DEMO_OWNER=${DEMO_OWNER:-demo-admin@harkeniq.com}
DEMO_OWNER_PASS=${DEMO_OWNER_PASS:-demo-admin}

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
       "node_commit": 10, "admin_email": "'"$DEMO_OWNER"'"}' \
  | python3 -m json.tool | head -6 || true

echo "==> Registering the tenant's Central Command placement"
# The Console resolves each tenant's L1-L3 stack through tenant_services,
# fail-closed: without a placement its infrastructure pages return 503
# rather than falling back to a shared endpoint. The Console's startup seed
# cannot do this for the demo because the container boots BEFORE this
# script creates the tenant, so register it explicitly here — which is the
# same thing a real multi-tenant install does.
# Resolve from the REPO ROOT, not $0: this script cds into
# deploy/full-stack before this line, so a $0-relative path breaks
# (gate-caught: "No such file or directory" under set -e).
_REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$(cd "$(dirname "$0")/.." && pwd)")"
source "$_REPO_ROOT/scripts/lib/tenant-lookup.sh"
TENANT_ID=$(lookup_tenant_id "$CONSOLE" "$AUTH")
if [ -n "$TENANT_ID" ]; then
  curl -s -X POST "$CONSOLE/api/admin/tenant-services/$TENANT_ID" -H "$AUTH" \
    -H "Content-Type: application/json" \
    -d "{\"service_kind\": \"central_command\",
         \"endpoint_url\": \"${CC_INTERNAL:-http://central-command:8090}\"}" \
    | python3 -m json.tool | head -5 || true
else
  echo "  !! demo tenant not found; skipping placement" >&2
fi

# E1.4: Central Command validates against the TENANT'S realm, so the
# platform-admin token above is refused there -- correctly: a platform
# identity is not a tenant operator. The Console admin calls keep using
# it; anything touching Central Command needs a tenant-realm identity.
#
# Tenant creation mints an owner in the tenant realm with no credential,
# which is right for a real install (the owner is invited). The demo has
# nobody to invite, so it sets one here.
echo "==> Giving the tenant owner a usable credential"
KC_ADMIN=$(curl -sf -X POST \
  "$KEYCLOAK/realms/master/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=admin-cli&username=admin&password=admin" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
curl -s -X POST "$KEYCLOAK/admin/realms/$TENANT_REALM/users" \
  -H "Authorization: Bearer $KC_ADMIN" -H "Content-Type: application/json" \
  -d "{\"username\":\"$DEMO_OWNER\",\"email\":\"$DEMO_OWNER\",\"enabled\":true,
       \"emailVerified\":true,\"firstName\":\"Demo\",\"lastName\":\"Admin\",
       \"credentials\":[{\"type\":\"password\",\"value\":\"$DEMO_OWNER_PASS\",
                          \"temporary\":false}]}" -o /dev/null
_UID=$(curl -s "$KEYCLOAK/admin/realms/$TENANT_REALM/users?username=$DEMO_OWNER&exact=true" \
  -H "Authorization: Bearer $KC_ADMIN" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['id'] if d else '')")
if [ -n "$_UID" ]; then
  # Tenant creation already assigned tenant_owner in the realm; what the
  # demo adds is a password it can actually log in with. The owner is
  # minted with a temporary credential, which is right for a real
  # install (the owner is invited) and unusable for a script.
  curl -s -X PUT "$KEYCLOAK/admin/realms/$TENANT_REALM/users/$_UID/reset-password" \
    -H "Authorization: Bearer $KC_ADMIN" -H "Content-Type: application/json" \
    -d "{\"type\":\"password\",\"value\":\"$DEMO_OWNER_PASS\",
         \"temporary\":false}" -o /dev/null
  curl -s -X PUT "$KEYCLOAK/admin/realms/$TENANT_REALM/users/$_UID" \
    -H "Authorization: Bearer $KC_ADMIN" -H "Content-Type: application/json" \
    -d '{"requiredActions":[],"emailVerified":true,"enabled":true}' -o /dev/null
  _ROLE=$(curl -s "$KEYCLOAK/admin/realms/$TENANT_REALM/roles/tenant_owner" \
    -H "Authorization: Bearer $KC_ADMIN")
  curl -s -X POST \
    "$KEYCLOAK/admin/realms/$TENANT_REALM/users/$_UID/role-mappings/realm" \
    -H "Authorization: Bearer $KC_ADMIN" -H "Content-Type: application/json" \
    -d "[$_ROLE]" -o /dev/null
  echo "  $DEMO_OWNER (tenant_owner) in realm $TENANT_REALM"
else
  echo "  !! could not create the tenant-realm owner" >&2
fi

TENANT_TOKEN=$(curl -sf -X POST \
  "$KEYCLOAK/realms/$TENANT_REALM/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=harkeniq-console&username=$DEMO_OWNER&password=$DEMO_OWNER_PASS" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null || echo "")
TENANT_AUTH="Authorization: Bearer $TENANT_TOKEN"

echo "==> Registering site-1's Site Manager with Central Command"
curl -s -X POST "$CC/api/sites/register" -H "$TENANT_AUTH" \
  -H "Content-Type: application/json" \
  -d '{"site_name": "site-1", "sm_endpoint": "site-manager:50051",
       "license_fingerprint": "demo"}' \
  | python3 -m json.tool | head -6 || true

echo
echo "Seeded. Where to look:"
echo "  Console (L4):      $CONSOLE      login $ADMIN_USER / $ADMIN_PASS (platform)"
echo "  Tenant identity:   $DEMO_OWNER / $DEMO_OWNER_PASS (realm $TENANT_REALM)"
echo "  Central Command:   $CC/api/fleet/ (Bearer token)"
echo "  Site Manager (L2): http://localhost:8080  token: dev-token-sm"
echo "  Keycloak:          $KEYCLOAK (admin/admin)"
echo
echo "Inject a fault to light up the pipeline:"
echo "  curl -sk -X POST https://localhost:9000/test/inject-fault \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"fault_type\":\"fan\",\"target\":\"Fan1A\",\"params\":{\"health\":\"Critical\",\"speed_rpm\":0}}'"
