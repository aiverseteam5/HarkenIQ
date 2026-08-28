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
source "$(dirname "$0")/lib/tenant-lookup.sh"
TENANT_ID=$(lookup_tenant_id "http://localhost:8100" "Authorization: Bearer $TOKEN")
[ -n "$TENANT_ID" ] || { echo "demo tenant not found" >&2; exit 1; }
curl -sfL -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8100/api/t/$TENANT_ID/fleet/summary" | grep -q total_nodes

step "Placement is fail-closed: an unregistered tenant is refused, not defaulted"
UNREG=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8100/api/t/does-not-exist/fleet/summary")
# An unknown tenant id 404s in tenant_scope BEFORE placement resolution, so
# this step alone never reaches the 503 branch (red-team finding). It stays
# as the unknown-id refusal check; the real fail-closed path is next.
[ "$UNREG" = "404" ] || { echo "unknown tenant returned $UNREG, want 404" >&2; exit 1; }

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

step "Action proposed -> approve as a named operator"
wait_for "pending action" 120 bash -c \
  "curl -s -H 'Authorization: Bearer dev-token-sm' http://localhost:8080/api/actions | grep -q COLLECT_DIAGNOSTICS"
ACTION=$(curl -s -H "Authorization: Bearer dev-token-sm" http://localhost:8080/api/actions \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")
RESULT=$(curl -s -X POST -H "Authorization: Bearer dev-token-sm" \
  -H "Content-Type: application/json" -d '{"actor": "ci-gate"}' \
  "http://localhost:8080/api/actions/$ACTION/approve")
echo "$RESULT" | grep -q '"sm-local:ci-gate"'

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
