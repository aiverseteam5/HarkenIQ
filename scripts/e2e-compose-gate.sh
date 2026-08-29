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
# Select the PENDING action, never actions[0]. The stack's volumes survive
# between gate runs, so index 0 is whatever the LAST run left behind — an
# already-approved action that never appears in CC's pending queue, which
# then times out the C2 step below for a reason unrelated to the code
# under test (observed 2026-08-29). Wait for and pick a genuinely pending one.
pending_action_id() {
  curl -s -H "Authorization: Bearer dev-token-sm" http://localhost:8080/api/actions \
    | python3 -c "import sys,json; print(next((a['id'] for a in json.load(sys.stdin) if a.get('status')=='pending'), ''))"
}
have_pending_action() { [ -n "$(pending_action_id)" ]; }

wait_for "pending action" 120 have_pending_action
ACTION=$(pending_action_id)
[ -n "$ACTION" ] || { echo "no pending action at SM" >&2; exit 1; }

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

step "S1: the trust ladder is VISIBLE to an operator, and still immutable (D2)"
# Posture reads opened to fleet.view so the people living under the ladder
# can see it; mutation stayed at site.manage. Both halves asserted.
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/policies/autonomy)" = "200" ]
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/policies/stop-switch)" = "200" ]
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/policies/stop-switch)" = "403" ]

step "S1: the surfaces the Tenant Console now renders are reachable THROUGH the proxy"
# Each of these had a live endpoint and no consumer before S1. The proxy
# path is the one the browser actually uses, so assert it, not CC direct.
for _p in learning/candidates learning/cycles learning/signals predictive/risk \
          firmware/exposure audit/verify attention incidents; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8100/api/t/$TENANT_ID/$_p")
  [ "$code" = "200" ] || { echo "proxy path $_p returned $code, want 200" >&2; exit 1; }
done
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8100/api/t/$TENANT_ID/audit/verify" | grep -q '"valid": *true'

step "S2: the attention capability answers with site attribution and evidence"
# The contract a future agent consumes, proven on the real stack: every
# ranked item must carry the site it belongs to, or a site-scoped caller
# cannot tell which rows are its own.
ATT=$(curl -sf -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/attention/)
echo "$ATT" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert "items" in d and "sites" in d and "summary" in d, "attention contract shape"
for i in d["items"]:
    assert "site_id" in i and i["site_id"], "every item must carry site attribution"
    assert "rank" in i and "band" in i and "reasons" in i, "ranking + explanation"
    assert "recommended_next" in i and "capability" in i["recommended_next"]
    assert "confidence" in i and "basis" in i["confidence"], "data sufficiency"
print("attention OK:", len(d["items"]), "ranked,", len(d["sites"]), "sites")
'
# Read-only: the capability names next steps, it never performs them.
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/attention/)" = "405" ]

step "S3: the learning substrate is DURABLE, not process memory"
# The learning ledger and the knowledge it produced must live in the
# database, so a restart cannot erase what the fleet learned. Assert the
# tables exist and are the ones attention reads.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT to_regclass('cc_learning_cycles'), to_regclass('cc_learned_signals')" \
  | grep -q "cc_learning_cycles|cc_learned_signals"
# Learned signals are knowledge, never authority: no write verb exists.
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/learning/signals)" = "405" ]
curl -sf -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/learning/signals \
  | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert "signals" in d, "learned-signal contract shape"
for s in d["signals"]:
    assert s["scope_type"] in ("cohort", "site"), "scope must be evidence-bound"
    assert s["statement"] and s["source_pattern_id"], "knowledge traces to a pattern"
print("learned signals OK:", len(d["signals"]))
'
# Attention must expose the learned-signal slot, so yesterdays learning has
# a path into tomorrows answer even before any pattern has been detected.
curl -sf -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/attention/ \
  | python3 -c '
import sys, json
d = json.load(sys.stdin)
for i in d["items"]:
    assert "learned_signals" in i["evidence"], "attention consumes learned knowledge"
print("attention<-learning wired OK")
'

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

step "S4: the diagnosis reaches the tenant surface, with its provenance"
# The whole point of S4: before it, the LLM explanation stopped at the Site
# Manager and the tenant could see WHAT was wrong but never WHY.
SM_INC=$(curl -sf -H "Authorization: Bearer dev-token-sm" http://localhost:8080/api/incidents)
echo "$SM_INC" | python3 -c "import sys,json; d=json.load(sys.stdin); print('SM incidents:', len(d))"
wait_for "incident at CC" 120 bash -c \
  "curl -s -H 'Authorization: Bearer $OP_TOKEN' http://localhost:8090/api/incidents/ | grep -q incident_id"
curl -sf -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/incidents/ | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert "incidents" in d, "incident contract shape"
for i in d["incidents"]:
    assert i["incident_id"] and i["site_id"], "tenant/site attribution"
    assert "is_parent" in i and "children" in i, "correlation hierarchy preserved"
    diag = i.get("diagnosis")
    if diag:
        # Provenance is a security property: a future agent reading this is
        # itself a language model, and this text came from device telemetry.
        assert diag["origin"], "diagnosis must name its origin"
        assert diag["trust"] in ("untrusted_generated", "deterministic")
        assert "generated" in diag, "model-authored fields must be grouped"
print("incidents OK:", len(d["incidents"]), "| explained:", d["diagnosed"])
'
# Read-only: incidents are a record, not a control surface.
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/incidents/)" = "405" ]
# The pseudo-incident placeholder is gone, not left to disagree with truth.
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/fleet/incidents)" = "404" ]

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
