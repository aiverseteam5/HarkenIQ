#!/usr/bin/env bash
# The compose exit gate (QA-030): boots the REAL stack and drives the
# amendment-§8 scenario end to end. This is the gate whose absence let
# R4-0 ship green while CC/Console had no schema and the mock simulator
# couldn't start. Run locally or in CI — identical either way.
#
#   register -> poll -> inject fault -> incident -> propose -> approve
set -euo pipefail
cd "$(dirname "$0")/../deploy/full-stack"

step() { echo; echo "=== $*"; }

wait_for() {  # wait_for <description> <timeout_s> <command...>
  local desc=$1 timeout=$2; shift 2
  local start=$SECONDS
  until "$@" > /dev/null 2>&1; do
    if (( SECONDS - start > timeout )); then
      echo "TIMEOUT waiting for: $desc" >&2
      docker compose ps >&2
      exit 1
    fi
    sleep 5
  done
}

step "Build + boot the full stack"
docker compose up -d --build

step "Every service healthy (real healthchecks: DB probes, not smoke)"
services=(postgres keycloak site-manager central-command console mock-simulator)
for svc in "${services[@]}"; do
  wait_for "$svc healthy" 300 bash -c \
    "docker compose ps --format '{{.Name}} {{.Status}}' | grep $svc | grep -q healthy"
done
wait_for "agent running" 120 bash -c \
  "docker compose ps --format '{{.Name}} {{.State}}' | grep agent | grep -q running"

step "Seed: tenant + site registration (real Keycloak token)"
bash ../../scripts/seed-demo.sh

step "Agent registered and observed at SM"
wait_for "SM device observed" 120 bash -c \
  "curl -s -H 'Authorization: Bearer dev-token-sm' http://localhost:8080/api/devices | grep -q '\"observation\": *\"observed\"\\|observed'"

step "CC fleet poll picked up the agent (token bootstrap worked)"
TOKEN=$(curl -sf -X POST \
  "http://localhost:8180/realms/harkeniq-platform/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=harkeniq-console&username=admin@harkeniq.com&password=admin" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
wait_for "CC fleet has the device" 120 bash -c \
  "curl -s -H 'Authorization: Bearer $TOKEN' http://localhost:8090/api/fleet/ | grep -q agent_id"

step "Console proxy serves CC data for the tenant (SPA path)"
# The proxy is tenant-scoped now: /api/t/{tenant}/fleet/... resolved through
# the tenant_services placement registry. A tenant with no placement is
# refused with 503 rather than handed a shared Central Command, so this
# step also proves seed-demo.sh registered one.
# Resolve from the REPO ROOT, not $0: this script cds into
# deploy/full-stack before this line, so a $0-relative path breaks
# (gate-caught: "No such file or directory" under set -e).
_REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$(cd "$(dirname "$0")/.." && pwd)")"
source "$_REPO_ROOT/scripts/lib/tenant-lookup.sh"
TENANT_ID=$(lookup_tenant_id "http://localhost:8100" "Authorization: Bearer $TOKEN")
[ -n "$TENANT_ID" ] || { echo "demo tenant not found" >&2; exit 1; }
curl -sfL -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8100/api/t/$TENANT_ID/fleet/summary" | grep -q total_nodes

step "Placement is fail-closed: an unregistered tenant is refused, not defaulted"
UNREG=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8100/api/t/does-not-exist/fleet/summary")
# Refusal semantics differ across the PR stack this gate rides on: with
# the tenant-existence check in tenant_scope (navigation slice) an unknown
# id is 404; without it, placement resolution fail-closes as 503. Both are
# refusals; 200 is the only failure. The REAL 503-branch proof is the
# placement-less tenant step below, which is exact on every branch.
case "$UNREG" in 404|503) : ;; *) echo "unknown tenant returned $UNREG, want 404/503" >&2; exit 1;; esac

step "Fail-closed for a REAL tenant with no placement (the 503 branch itself)"
DARK_ID=$(curl -sf -X POST "http://localhost:8100/api/admin/tenants/" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "Gate Dark Tenant", "slug": "gate-dark", "billing_country": "US",
       "currency": "USD", "plan": "observe", "node_commit": 1,
       "admin_email": "dark@gate.example"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" ) || \
  DARK_ID=$(lookup_tenant_id "http://localhost:8100" "Authorization: Bearer $TOKEN" gate-dark)
DARK=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8100/api/t/$DARK_ID/fleet/summary")
[ "$DARK" = "503" ] || { echo "placement-less tenant returned $DARK, want 503" >&2; exit 1; }
# Known gate limitation (documented, not hidden): the scenario runs on a
# platform_super_admin token whose break-glass bypasses membership and the
# support-access gate; those paths are pinned by the unit suite, not here.

step "Auth is real: no token / garbage token are rejected"
[ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8090/api/fleet/)" = "401" ]
[ "$(curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer junk' http://localhost:8090/api/fleet/)" = "401" ]

step "Inject a critical fan fault at the simulated BMC"
curl -skf -X POST https://localhost:9000/test/inject-fault \
  -H 'Content-Type: application/json' \
  -d '{"fault_type":"fan","target":"Fan1A","params":{"health":"Critical","speed_rpm":0}}' \
  > /dev/null

step "Agent detects -> SM opens the incident"
wait_for "fan incident open" 120 bash -c \
  "curl -s -H 'Authorization: Bearer dev-token-sm' http://localhost:8080/api/incidents | grep -q '\"subsystem\": *\"fan\"\\|fan CRITICAL'"

step "Action proposed at the SM"
wait_for "pending action" 120 bash -c \
  "curl -s -H 'Authorization: Bearer dev-token-sm' http://localhost:8080/api/actions | grep -q COLLECT_DIAGNOSTICS"
ACTION=$(curl -s -H "Authorization: Bearer dev-token-sm" http://localhost:8080/api/actions \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

step "Seed an OPERATOR (P0 2026-08-29): the role that was locked out of CC"
# C1's proof at runtime: before the RBAC repair, CC granted non-admins
# only the literal "view" — an operator 403ed on every route including
# approvals, so this persona could not function at all. Create the role
# and a user via the Keycloak admin API (idempotent: 409s tolerated).
KC_ADMIN_TOKEN=$(curl -sf -X POST \
  "http://localhost:8180/realms/master/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=admin-cli&username=admin&password=${HARKENIQ_KC_ADMIN_PASSWORD:-admin}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
curl -s -o /dev/null -X POST \
  "http://localhost:8180/admin/realms/harkeniq-platform/roles" \
  -H "Authorization: Bearer $KC_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "operator"}'
curl -s -o /dev/null -X POST \
  "http://localhost:8180/admin/realms/harkeniq-platform/users" \
  -H "Authorization: Bearer $KC_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"username": "operator1@harkeniq.com", "email": "operator1@harkeniq.com",
       "firstName": "Gate", "lastName": "Operator",
       "enabled": true, "emailVerified": true,
       "credentials": [{"type": "password", "value": "operator", "temporary": false}]}'
OP_UID=$(curl -sf "http://localhost:8180/admin/realms/harkeniq-platform/users?username=operator1@harkeniq.com" \
  -H "Authorization: Bearer $KC_ADMIN_TOKEN" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")
# Keycloak's declarative user profile queues VERIFY_PROFILE on users
# created without a full profile, and "Account is not fully set up" then
# refuses the password grant. Clear required actions idempotently so a
# rerun (or a partial earlier run) always converges to a usable operator.
curl -s -o /dev/null -X PUT \
  "http://localhost:8180/admin/realms/harkeniq-platform/users/$OP_UID" \
  -H "Authorization: Bearer $KC_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"firstName": "Gate", "lastName": "Operator",
       "email": "operator1@harkeniq.com", "emailVerified": true,
       "enabled": true, "requiredActions": []}'
OP_ROLE=$(curl -sf "http://localhost:8180/admin/realms/harkeniq-platform/roles/operator" \
  -H "Authorization: Bearer $KC_ADMIN_TOKEN")
curl -s -o /dev/null -X POST \
  "http://localhost:8180/admin/realms/harkeniq-platform/users/$OP_UID/role-mappings/realm" \
  -H "Authorization: Bearer $KC_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d "[$OP_ROLE]"
OP_TOKEN=$(curl -sf -X POST \
  "http://localhost:8180/realms/harkeniq-platform/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=harkeniq-console&username=operator1@harkeniq.com&password=operator" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

step "CC RBAC is real: operator reads fleet (200), cannot read audit (403)"
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/fleet/)" = "200" ]
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/audit/)" = "403" ]

step "CC ingested the pending action (C2: the approvals hop is wired)"
# Fleet poll interval is 30s in this stack; the route must appear at CC.
wait_for "approval route at CC" 120 bash -c \
  "curl -s -H 'Authorization: Bearer $OP_TOKEN' http://localhost:8090/api/approvals/ | grep -q '$ACTION'"

step "OPERATOR approves through CC -> RouteApproval -> SM records the decision"
RESULT=$(curl -s -X POST -H "Authorization: Bearer $OP_TOKEN" \
  "http://localhost:8090/api/approvals/$ACTION/approve")
echo "$RESULT" | grep -q '"decision": *"approved"'
echo "$RESULT" | grep -q 'operator1@harkeniq.com'
wait_for "SM action approved" 60 bash -c \
  "curl -s -H 'Authorization: Bearer dev-token-sm' http://localhost:8080/api/actions | grep -q '\"status\": *\"approved\"\\|approved'"

step "Audit chain verifies"
curl -sf -H "Authorization: Bearer dev-token-sm" http://localhost:8080/api/audit/verify | grep -q true

step "Autonomy identity chain live (QA-040: agent identity + certificate persisted)"
# The observe->approve path works even when RegisterAgent crashes server-side,
# so assert the persisted row directly — a green gate must mean leases can flow.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
  "SELECT count(*) FROM agent_identities WHERE certificate IS NOT NULL" \
  | grep -qv '^0$'

step "No ERROR-level logs in any service (crashed handlers must not pass silently)"
for svc in site-manager central-command console; do
  if docker compose logs --no-log-prefix "$svc" 2>&1 \
      | grep -E '"level": *"(ERROR|CRITICAL)"' ; then
    echo "ERROR-level log lines in $svc (above)" >&2
    exit 1
  fi
done

step "GATE GREEN"
