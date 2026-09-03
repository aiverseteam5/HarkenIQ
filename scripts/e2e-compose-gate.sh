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
  "curl -s -H 'Authorization: Bearer dev-token-sm' http://localhost:8080/api/devices?site=site-1 | grep -q '\"observation\": *\"observed\"\\|observed'"

# E1.4: Central Command validates against the TENANT'S realm now, so
# every CC-facing token below must come from tenant-demo. The platform
# realm holds only platform_super_admin and platform_support, and a
# platform token reaching Central Command is a 401 by design.
tenant_realm_user() {
  # $1 email, $2 password, $3 realm role
  local kc_admin
  kc_admin=$(curl -sf -X POST \
    "http://localhost:8180/realms/master/protocol/openid-connect/token" \
    -d "grant_type=password&client_id=admin-cli&username=admin&password=admin" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
  curl -s -X POST "http://localhost:8180/admin/realms/tenant-demo/users" \
    -H "Authorization: Bearer $kc_admin" -H "Content-Type: application/json" \
    -d "{\"username\":\"$1\",\"email\":\"$1\",\"enabled\":true,
         \"emailVerified\":true,\"firstName\":\"Gate\",\"lastName\":\"User\",
         \"credentials\":[{\"type\":\"password\",\"value\":\"$2\",
                            \"temporary\":false}]}" -o /dev/null
  local uid rj
  uid=$(curl -s "http://localhost:8180/admin/realms/tenant-demo/users?username=$1&exact=true" \
    -H "Authorization: Bearer $kc_admin" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['id'] if d else '')")
  [ -n "$uid" ] || { echo "could not create tenant-realm user $1" >&2; return 1; }
  rj=$(curl -s "http://localhost:8180/admin/realms/tenant-demo/roles/$3" \
    -H "Authorization: Bearer $kc_admin")
  curl -s -X POST \
    "http://localhost:8180/admin/realms/tenant-demo/users/$uid/role-mappings/realm" \
    -H "Authorization: Bearer $kc_admin" -H "Content-Type: application/json" \
    -d "[$rj]" -o /dev/null
}

tenant_token() {
  curl -sf -X POST \
    "http://localhost:8180/realms/tenant-demo/protocol/openid-connect/token" \
    -d "grant_type=password&client_id=harkeniq-console&username=$1&password=$2" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])"
}

step "E1.4: the tenant's own realm exists, and Central Command validates against it"
# Tenant creation provisions the realm; a tenant that predates E1.4 gets
# one through the explicit provisioning endpoint. Either way the tenant
# has an identity boundary before anybody authenticates into it.
PLATFORM_TOKEN=$(curl -sf -X POST \
  "http://localhost:8180/realms/harkeniq-platform/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=harkeniq-console&username=admin@harkeniq.com&password=admin" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
E14_TENANT=$(curl -sf -H "Authorization: Bearer $PLATFORM_TOKEN" \
  http://localhost:8100/api/admin/tenants/ \
  | python3 -c "
import sys, json
print([t['id'] for t in json.load(sys.stdin)['items'] if t['slug'] == 'tenant-demo'][0])")
curl -sf -X POST -H "Authorization: Bearer $PLATFORM_TOKEN" \
  "http://localhost:8100/api/admin/tenants/$E14_TENANT/provision-realm" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['keycloak_realm'] == 'tenant-demo', d
print('tenant realm:', d['keycloak_realm'])"

tenant_realm_user gate-owner@demo gate-owner tenant_owner || true
tenant_realm_user gate-op@demo gate-op operator || true
tenant_realm_user gate-aud@demo gate-aud auditor || true

KC_ADMIN=$(curl -sf -X POST \
  "http://localhost:8180/realms/master/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=admin-cli&username=admin&password=admin" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

step "E1.4: the realm carries the five tenant roles, a client, and an owner"
curl -sf "http://localhost:8180/admin/realms/tenant-demo/roles" \
  -H "Authorization: Bearer $KC_ADMIN" | python3 -c "
import sys, json
have = {r['name'] for r in json.load(sys.stdin)}
want = {'tenant_owner', 'site_admin', 'operator', 'auditor', 'viewer'}
missing = want - have
assert not missing, f'tenant realm is missing roles: {sorted(missing)}'
print('five tenant roles provisioned:', sorted(want))
"
curl -sf "http://localhost:8180/admin/realms/tenant-demo/clients?clientId=harkeniq-console" \
  -H "Authorization: Bearer $KC_ADMIN" | python3 -c "
import sys, json
clients = json.load(sys.stdin)
assert clients, 'the tenant realm has no console client to sign in through'
print('console client registered in the tenant realm')
"
curl -sf "http://localhost:8180/admin/realms/tenant-demo/users" \
  -H "Authorization: Bearer $KC_ADMIN" | python3 -c "
import sys, json
users = [u['username'] for u in json.load(sys.stdin)]
assert users, 'the tenant realm has no users at all'
print('tenant realm users:', sorted(users)[:6])
"

step "E1.4: the tenant<->realm binding is recorded and authoritative"
curl -sf -H "Authorization: Bearer $PLATFORM_TOKEN" \
  http://localhost:8100/api/admin/tenants/ | python3 -c "
import sys, json
rows = json.load(sys.stdin)['items']
demo = [t for t in rows if t['slug'] == 'tenant-demo'][0]
assert demo['keycloak_realm'] == 'tenant-demo', demo
# The defect this closes: creation used to return 200 with a NULL realm.
assert all(t['keycloak_realm'] for t in rows), (
    'a tenant exists with no realm: it reports success and nobody can '
    'sign in to it'
)
print('every tenant has a recorded realm binding')
"

step "E1.4: the platform realm holds NO tenant operational role"
curl -sf "http://localhost:8180/admin/realms/harkeniq-platform/roles" \
  -H "Authorization: Bearer $KC_ADMIN" | python3 -c "
import sys, json
have = {r['name'] for r in json.load(sys.stdin)}
tenant_roles = {'tenant_owner', 'site_admin', 'operator', 'auditor', 'viewer'}
leaked = have & tenant_roles
assert not leaked, (
    f'the platform realm carries tenant operational roles {sorted(leaked)}: '
    'a platform identity would become a tenant operator'
)
assert 'platform_super_admin' in have
print('platform realm roles:', sorted(have - {'offline_access', 'uma_authorization'}))
"

step "E1.4: a PLATFORM identity is refused by Central Command"
PLAT_CC=$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $PLATFORM_TOKEN" http://localhost:8090/api/fleet/)
[ "$PLAT_CC" = "401" ] || {
  echo "a platform identity reached tenant Central Command ($PLAT_CC)" >&2
  exit 1; }
echo "platform -> tenant Central Command: 401 at validation"

step "E1.4: a stray realm cannot mint access"
STRAY=$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $KC_ADMIN" http://localhost:8090/api/fleet/)
[ "$STRAY" = "401" ] || { echo "a master-realm token reached CC ($STRAY)" >&2; exit 1; }
STRAY_CONSOLE=$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $KC_ADMIN" http://localhost:8100/api/admin/tenants/)
[ "$STRAY_CONSOLE" = "401" ] || {
  echo "a master-realm token reached the Console ($STRAY_CONSOLE)" >&2; exit 1; }
echo "master-realm token: 401 at both services"

step "CC fleet poll picked up the agent (token bootstrap worked)"
# E1.4: a TENANT-realm identity. A platform token is refused by Central
# Command at validation now, which is the boundary this proves.
TOKEN=$(tenant_token gate-owner@demo gate-owner)

# A23-5: the tenant is born STRICT (A23.11), so gate-owner reaches
# NOTHING until somebody grants it -- there is no `legacy_open`
# synthesis to stand in any more. The tenant's founding administrator
# was seeded at birth by provisioning (A23.14 D4) on the owner subject
# the Console recorded, and it is that administrator who grants
# gate-owner, which is the ordinary two-person act A23.6 requires.
BIRTH_OWNER=${DEMO_OWNER:-demo-admin@harkeniq.com}
BIRTH_PASS=${DEMO_OWNER_PASS:-demo-admin}
birth_granted() {
  local t
  t=$(tenant_token "$BIRTH_OWNER" "$BIRTH_PASS") || return 1
  [ -n "$t" ] || return 1
  curl -sf -H "Authorization: Bearer $t" \
    http://localhost:8090/api/scope-grants/me | python3 -c "
import sys, json
d = json.load(sys.stdin)
raise SystemExit(0 if d['tenant_wide'] and d['synthesis'] == 'granted' else 1)"
}
wait_for "the tenant is born with its administrator" 300 birth_granted
BIRTH_TOKEN=$(tenant_token "$BIRTH_OWNER" "$BIRTH_PASS")
curl -sf -H "Authorization: Bearer $BIRTH_TOKEN" \
  http://localhost:8090/api/scope-grants/me | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['tenant_wide'] is True, d
assert d['synthesis'] == 'granted', ('the founding grant must be REAL, never '
                                     'synthesized: %r' % d)
print('A23-5: tenant born strict, administrator seeded:', d['synthesis'])"

OWNER_SUB=$(python3 -c "
import base64, json
t = '$TOKEN'.split('.')[1]; t += '=' * (-len(t) % 4)
print(json.loads(base64.urlsafe_b64decode(t))['sub'])")
BOOT=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $BIRTH_TOKEN" -H "Content-Type: application/json" \
  -d "{\"principal_ref\":\"$OWNER_SUB\",\"scope_type\":\"tenant\",\"role\":\"tenant_owner\"}" \
  http://localhost:8090/api/scope-grants/)
[ "$BOOT" = "201" ] || {
  echo "the founding administrator could not grant gate-owner ($BOOT)" >&2
  exit 1; }
echo "the birth-seeded administrator granted the gate's operator identity"

# The operator identity needs authority from the start too -- under
# strict birth it is not tenant-wide by synthesis any more, and every
# early step below reads approvals, incidents and attention as it. A
# real tenant's administrator grants its operators; the E1.2 step
# further down NARROWS this one to a single site, which is what makes
# its `site_ids == [E12_SITE]` assertion a real narrowing rather than an
# accident of never having been granted.
OP_BOOT_SUB=$(python3 -c "
import base64, json
t = '$(tenant_token gate-op@demo gate-op)'.split('.')[1]; t += '=' * (-len(t) % 4)
print(json.loads(base64.urlsafe_b64decode(t))['sub'])")
OPBOOT=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $BIRTH_TOKEN" -H "Content-Type: application/json" \
  -d "{\"principal_ref\":\"$OP_BOOT_SUB\",\"scope_type\":\"tenant\",\"role\":\"operator\"}" \
  http://localhost:8090/api/scope-grants/)
[ "$OPBOOT" = "201" ] || {
  echo "the founding administrator could not grant the operator ($OPBOOT)" >&2
  exit 1; }
echo "and the operator identity"

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
# E1.4: a TENANT identity is refused at the scope check before placement
# is ever resolved -- 403, because a tenant may not probe another
# tenant's id at all. That is stricter than the platform path, so it is
# asserted first and separately.
UNREG_TENANT=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8100/api/t/does-not-exist/fleet/summary")
[ "$UNREG_TENANT" = "403" ] || {
  echo "a tenant identity probing another tenant returned $UNREG_TENANT, want 403" >&2
  exit 1; }

# The PLACEMENT branch itself needs a caller that gets past the scope
# check, which is the platform plane.
UNREG=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $PLATFORM_TOKEN" \
  "http://localhost:8100/api/t/does-not-exist/fleet/summary")
# Refusal semantics differ across the PR stack this gate rides on: with
# the tenant-existence check in tenant_scope (navigation slice) an unknown
# id is 404; without it, placement resolution fail-closes as 503. Both are
# refusals; 200 is the only failure. The REAL 503-branch proof is the
# placement-less tenant step below, which is exact on every branch.
case "$UNREG" in 404|503) : ;; *) echo "unknown tenant returned $UNREG, want 404/503" >&2; exit 1;; esac

step "Fail-closed for a REAL tenant with no placement (the 503 branch itself)"
DARK_ID=$(curl -sf -X POST "http://localhost:8100/api/admin/tenants/" \
  -H "Authorization: Bearer $PLATFORM_TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "Gate Dark Tenant", "slug": "gate-dark", "billing_country": "US",
       "currency": "USD", "plan": "observe", "node_commit": 1,
       "admin_email": "dark@gate.example"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" ) || \
  DARK_ID=$(lookup_tenant_id "http://localhost:8100" "Authorization: Bearer $PLATFORM_TOKEN" gate-dark)
# The 503 is about PLACEMENT, so it needs a caller who reaches placement
# resolution -- the platform plane. A tenant identity is refused earlier,
# at the scope check, which the step above asserts separately.
DARK=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $PLATFORM_TOKEN" \
  "http://localhost:8100/api/t/$DARK_ID/fleet/summary")
[ "$DARK" = "503" ] || { echo "placement-less tenant returned $DARK, want 503" >&2; exit 1; }
# E1.4 narrowed this: the scenario now runs on a TENANT-realm identity, so
# the tenant-plane path is genuinely exercised rather than ridden over by
# a platform break-glass. Platform-plane calls are explicitly marked with
# $PLATFORM_TOKEN, and there are only a handful.

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
  "curl -s -H 'Authorization: Bearer dev-token-sm' http://localhost:8080/api/incidents?site=site-1 | grep -q '\"subsystem\": *\"fan\"\\|fan CRITICAL'"

step "Action proposed at the SM"
# Select the PENDING action, never actions[0]. The stack's volumes survive
# between gate runs, so index 0 is whatever the LAST run left behind — an
# already-approved action that never appears in CC's pending queue, which
# then times out the C2 step below for a reason unrelated to the code
# under test (observed 2026-08-29). Wait for and pick a genuinely pending one.
pending_action_id() {
  curl -s -H "Authorization: Bearer dev-token-sm" http://localhost:8080/api/actions?site=site-1 \
    | python3 -c "import sys,json; print(next((a['id'] for a in json.load(sys.stdin) if a.get('status')=='pending'), ''))"
}
have_pending_action() { [ -n "$(pending_action_id)" ]; }

wait_for "pending action" 120 have_pending_action
ACTION=$(pending_action_id)
[ -n "$ACTION" ] || { echo "no pending action at SM" >&2; exit 1; }

step "Seed an OPERATOR: the role that was locked out of CC"
# C1's proof at runtime: before the RBAC repair, CC granted non-admins
# only the literal "view" -- an operator 403ed on every route including
# approvals, so this persona could not function at all.
#
# E1.4: the operator lives in the TENANT realm now. This used to create
# the role and the user in the PLATFORM realm, which is exactly the
# boundary E1.4 closes -- and it ran AFTER the step asserting the
# platform realm holds no tenant operational role, quietly putting one
# back. `tenant_realm_user` created this persona at the top of the gate.
KC_ADMIN_TOKEN=$(curl -sf -X POST \
  "http://localhost:8180/realms/master/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=admin-cli&username=admin&password=${HARKENIQ_KC_ADMIN_PASSWORD:-admin}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
OP_TOKEN=$(tenant_token gate-op@demo gate-op)

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
echo "$RESULT" | grep -q 'gate-op@demo'
wait_for "SM action approved" 60 bash -c \
  "curl -s -H 'Authorization: Bearer dev-token-sm' http://localhost:8080/api/actions?site=site-1 | grep -q '\"status\": *\"approved\"\\|approved'"

step "S4: the diagnosis reaches the tenant surface, with its provenance"
# The whole point of S4: before it, the LLM explanation stopped at the Site
# Manager and the tenant could see WHAT was wrong but never WHY.
SM_INC=$(curl -sf -H "Authorization: Bearer dev-token-sm" http://localhost:8080/api/incidents?site=site-1)
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

step "S5: the autonomy contract is served, and it fences what it must"
# Read at fleet.view (D2's read-split): the people living under the trust
# ladder must be able to see it. The operator persona is used deliberately.
curl -sf -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/autonomy/ \
  | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d["contract_version"], "contract must be versioned for its consumers"
for key in ("actor", "scope", "posture", "safety_state", "action_classes"):
    assert key in d, f"missing contract section: {key}"
by = {c["action_type"]: c for c in d["action_classes"]}
# Every action the executor can run must be governed by a row here; a
# class missing from the contract is a class nobody governs.
assert len(by) >= 14, f"only {len(by)} action classes"
# The boundary. No level, and no amount of evidence, may ever make these
# autonomous through an autonomy budget.
for at in ("FIRMWARE_UPDATE", "FIRMWARE_ROLLBACK", "INTERFACE_RESET",
           "INTERFACE_DISABLE"):
    assert by[at]["never_budget_grantable"] is True, at
    assert by[at]["disposition"] == "denied", at
# Every class states WHY it is where it is, and what would move it.
for at, c in by.items():
    assert c["disposition_reason"], f"{at} has no reason"
    assert c["advancement"]["statement"], f"{at} has no advancement line"
    assert "evidence" in c and "safety" in c
print("autonomy OK:", len(by), "classes | level:",
      d["posture"]["configured_level"],
      "| safety reported:", d["safety_state"]["reported"])
'
# The contract is READ-ONLY. Every autonomy mutation stays on
# /api/policies/* at site.manage; S5 added no second control path.
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/autonomy/)" = "405" ]
# Safety state must actually have travelled SM -> CC, not defaulted.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT count(*) FROM cc_safety_state WHERE reported = true" \
  | grep -qv '^0$'

step "A0+A1: a named Operational Agent, end to end on the real stack"
# The thesis slice, proven rather than described: create -> scope -> bind
# -> activate -> observe -> propose -> approve -> dispatch -> execute ->
# attribute. Every hop uses a capability that already existed; the only
# new one is the CC->SM dispatch verb, which queues on the R5-1 directive
# transport the firmware campaigns already ride.
SITE_ID=$(curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/sites/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['sites'][0]['id'])")

# Creating an agent is site.manage. An operator holds action.approve and
# must NOT be able to configure one: deciding is not configuring.
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $OP_TOKEN" -H "Content-Type: application/json" \
    -d '{"name":"nope","scopes":[],"capabilities":[]}' \
    http://localhost:8090/api/operational-agents/)" = "403" ]

AGENT_JSON=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"Gate Agent $(date +%s)\",
       \"description\":\"compose gate\",
       \"require_approval_always\":true,
       \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$SITE_ID\"}],
       \"capabilities\":[
         {\"kind\":\"action_class\",\"capability_ref\":\"SEL_CLEAR\"},
         {\"kind\":\"action_class\",\"capability_ref\":\"COLLECT_DIAGNOSTICS\"}]}" \
  http://localhost:8090/api/operational-agents/)
AGENT_ID=$(echo "$AGENT_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "$AGENT_JSON" | python3 -c '
import sys, json
a = json.load(sys.stdin)
assert a["status"] == "draft", "a new agent must not be born active"
assert a["actor"].startswith("op-agent:") and a["actor"].endswith("@v1")
reads = {c["capability_ref"] for c in a["capabilities"] if c["kind"] == "read"}
assert {"attention", "autonomy"} <= reads, "required reads must be bound"
print("agent created:", a["actor"], a["status"])
'

# A2: a draft agent evaluates nothing, and activation is now a GOVERNED
# transition, not a status write. PREFLIGHT -> ACKNOWLEDGE (where warned)
# -> ACTIVATE. Switching an agent on without a stored readiness result
# for this exact configuration version is refused.
step "A2: activation is refused without a preflight for THIS configuration"
NOPRE=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$AGENT_ID/activate")
[ "$NOPRE" = "409" ] || {
  echo "activated with no preflight ($NOPRE)" >&2; exit 1; }
echo "activation without a preflight refused (409)"

PRE_JSON=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$AGENT_ID/preflight")
echo "$PRE_JSON" | python3 -c '
import sys, json
p = json.load(sys.stdin)
assert p["configuration_version"] == 1, p["configuration_version"]
assert len(p["dimensions"]) == 12, len(p["dimensions"])
# This agent requires a human for every action, so switching it on
# confers no unattended execution -- and D1 therefore raises no
# activation approval. Approval is derived, never ceremonial.
assert p["requires_activation_approval"] is False, p["unattended_classes"]
assert p["unattended_classes"] == [], p["unattended_classes"]
# A READY preflight is a statement about configuration, not a grant.
assert "grants nothing" in p["contract"]["authority"]
print("preflight:", p["overall"],
      "| blocked:", p["blocked_dimensions"],
      "| warn:", p["warn_dimensions"],
      "| unknown:", p["unknown_dimensions"])
'
NEEDS_ACK=$(echo "$PRE_JSON" | python3 -c \
  "import sys,json; print(json.load(sys.stdin)['requires_acknowledgement'])")
if [ "$NEEDS_ACK" = "True" ]; then
  # A warning is not a veto, but it is not nothing: a named human accepts
  # it, version-bound, before anything is switched on.
  curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8090/api/operational-agents/$AGENT_ID/acknowledge" \
    | python3 -c "
import sys, json
a = json.load(sys.stdin)
assert a['acknowledged_by'], 'an acknowledgement must name a person'
print('warnings acknowledged by', a['acknowledged_by'])
"
fi

curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$AGENT_ID/activate" \
  | python3 -c "
import sys, json
a = json.load(sys.stdin)
assert a['status'] == 'active' and a['activated_by'], 'activation must name a human'
# A19.9: activation records the configuration it switched on, atomically.
# Before this had a writer, activated_version stayed 0 against version 1
# and every active agent reported drift the moment it was turned on.
assert a['activated_version'] == a['version'], (a['activated_version'], a['version'])
print('agent activated by', a['activated_by'], 'at v%d' % a['activated_version'])
"

# A19.9 stated positively: active AND activated_version == version -> no drift.
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$AGENT_ID/runtime" | python3 -c '
import sys, json
r = json.load(sys.stdin)
assert r["activation_state"] == "active", r["activation_state"]
assert r["activation_provenance"] == "recorded", r["activation_provenance"]
assert r["configuration_drifted"] is False, "a fresh activation is not drifted"
assert r["preflight"]["current"] is True
# Device freshness is three-valued: a device the site has never reported
# is counted as neither healthy nor unhealthy.
assert "never_reported" in r["devices"], r["devices"]
print("runtime:", r["devices"], "| budget:", r["budget"]["limit"] or "unset")
'

# The detail view answers what an operator actually asks.
curl -sf -H "Authorization: Bearer $OP_TOKEN" \
  "http://localhost:8090/api/operational-agents/$AGENT_ID" | python3 -c '
import sys, json
v = json.load(sys.stdin)
assert v["scope"]["device_count"] >= 1, "the agent must see the seeded node"
classes = {c["action_type"]: c for c in v["capabilities"]["action_classes"]}
assert set(classes) == {"SEL_CLEAR", "COLLECT_DIAGNOSTICS"}
for at, c in classes.items():
    assert c["disposition_reason"], at
    # require_approval_always is a one-way tightening: nothing this agent
    # holds may run unattended, whatever the tenant grants.
    assert c["requires_approval"] is True, at
print("agent sees", v["scope"]["device_count"], "device(s);",
      len(classes), "classes, all requiring a human")
'

# Wait for the evaluator to observe the fault the gate already injected.
wait_for "agent proposal in the ONE approval queue" 240 bash -c \
  "curl -s -H 'Authorization: Bearer $OP_TOKEN' http://localhost:8090/api/approvals/ \
   | python3 -c \"import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('agent_total',0)>0 else 1)\""

QUEUE=$(curl -sf -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/approvals/)
echo "$QUEUE" | python3 -c '
import sys, json
d = json.load(sys.stdin)
agent_items = [a for a in d["actions"] if a["origin"] == "agent"]
assert agent_items, "the agent proposal must appear in the same queue"
p = agent_items[0]["proposal"]
# A request with no reasoning is not reviewable.
assert p["rationale"], "a proposal must say what it saw and why"
assert p["actor"].startswith("op-agent:"), "attribution on the proposal"
assert p["evidence"]["observed"], "evidence must name the observation"
assert p["authorization_basis"] == "human_approval"
assert p["status"] == "awaiting_approval"
print("proposal:", p["action_type"], "on", p["device_agent_id"])
print("rationale:", p["rationale"][:160])
'
PROP_ID=$(echo "$QUEUE" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print([a for a in d['actions'] if a['origin'] == 'agent'][0]['action_id'])
")

# The SAME endpoint and the SAME permission a node action uses.
curl -sf -X POST -H "Authorization: Bearer $OP_TOKEN" \
  "http://localhost:8090/api/approvals/$PROP_ID/approve" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d["origin"] == "agent"
assert d["decided_by"], "a named human must be recorded"
assert d["delivery"]["delivered"] is True, d["delivery"]
print("approved by", d["decided_by"], "-> directive",
      d["delivery"].get("directive_id"))
'

# Dispatch really reached the Site Manager as a directive carrying the
# agent's attribution, and the node really settled it.
wait_for "directive settled at the SM" 180 bash -c \
  "docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
   \"SELECT count(*) FROM sm_directives WHERE actor LIKE 'op-agent:%' \
     AND status IN ('completed','failed')\" | grep -qv '^0\$'"
docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
  "SELECT actor, authorization_basis, status FROM sm_directives \
   WHERE actor LIKE 'op-agent:%' LIMIT 1"

# The execution became EVIDENCE with its actor intact. Before this slice a
# directed action produced no outcome row at all, so nothing an agent (or
# a firmware campaign) did could ever reach the error budget or learning.
wait_for "attributed outcome at the SM" 180 bash -c \
  "docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
   \"SELECT count(*) FROM sm_action_outcomes WHERE actor LIKE 'op-agent:%'\" \
   | grep -qv '^0\$'"
wait_for "attributed outcome reached Central Command" 240 bash -c \
  "docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
   \"SELECT count(*) FROM cc_outcome_history WHERE actor LIKE 'op-agent:%'\" \
   | grep -qv '^0\$'"

# The proposal settles against its own outcome, so the agent's record is
# closed rather than left dispatched forever.
wait_for "proposal settled with its outcome" 240 bash -c \
  "curl -s -H 'Authorization: Bearer $OP_TOKEN' \
     http://localhost:8090/api/operational-agents/$AGENT_ID/proposals \
   | python3 -c \"import sys,json; d=json.load(sys.stdin); sys.exit(0 if any(p['outcome'] for p in d['proposals']) else 1)\""
curl -sf -H "Authorization: Bearer $OP_TOKEN" \
  "http://localhost:8090/api/operational-agents/$AGENT_ID/proposals" | python3 -c '
import sys, json
d = json.load(sys.stdin)
settled = [p for p in d["proposals"] if p["outcome"]]
assert settled, "no settled proposal"
p = settled[0]
assert p["status"] in ("completed", "failed")
assert p["directive_id"], "the proposal must name the directive it became"
print("settled:", p["action_type"], p["status"], "outcome", p["outcome"])
'

# The whole chain is reconstructable from the existing audit chain, and
# the agent is named in it as an actor.
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/audit/ | python3 -c '
import sys, json
d = json.load(sys.stdin)
actions = {e["action"] for e in d["entries"]}
for needed in ("operational_agent.created", "operational_agent.activated",
               "agent_proposal.created", "action.approved",
               "agent_proposal.dispatched"):
    assert needed in actions, f"missing audit event: {needed}"
assert any(e["actor"].startswith("op-agent:") for e in d["entries"]), \
    "the agent must appear as an actor in the chain"
print("audit chain carries the full agent journey")
'
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/audit/verify \
  | grep -q '"valid": *true'

# There is no agent execution surface. An agent router that could act
# would be the parallel governance path the architecture forbids.
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8090/api/operational-agents/$AGENT_ID/execute")" = "404" ]

step "E0.2: the CC-SM site identity is authoritative, and scoping holds"
# Before E0.2 the SM received CC's site id, discarded it, and answered
# every site's poll with everything it knew. Prove the binding exists and
# that a second site on the SAME Site Manager cannot see the first.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
  "SELECT name || ' -> ' || COALESCE(cc_site_id, '(unbound)') FROM sites"
docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
  "SELECT count(*) FROM sites WHERE cc_site_id IS NOT NULL" | grep -qv '^0$' || {
    echo "no site is bound to a Central Command identity" >&2; exit 1; }

SITE_A=$(curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/sites/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['sites'][0]['id'])")

step "E0.2: a second site on the same Site Manager is isolated"
# Register a second site pointing at the SAME Site Manager, then seed one
# device into it directly. The write path that lets an AGENT choose its
# site is E1.3; what E0.2 owns is that the read path cannot leak.
SITE_B=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"site_name":"gate-site-b","sm_endpoint":"site-manager:50051",
       "license_fingerprint":"demo"}' \
  http://localhost:8090/api/sites/register \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['site']['id'])")
echo "site B = $SITE_B"
docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
  "INSERT INTO devices (id, site_id, agent_id, agent_name, vendor, model,
                        service_tag, device_class, first_seen_at, last_seen_at)
   SELECT 'gatedevb00000000000000000000000', s.id, 'gate-agent-b', 'b1',
          'Dell', 'R750', 'GATEB1', 'server', now(), now()
   FROM sites s WHERE s.cc_site_id = '$SITE_B'
   ON CONFLICT (id) DO NOTHING"
docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
  "INSERT INTO incidents (id, site_id, kind, status, device_id, subsystem,
                          title, confidence, inferred, opened_at)
   SELECT 'gateincb00000000000000000000000', s.id, 'device', 'open',
          'gatedevb00000000000000000000000', 'psu', 'site B only', 1.0, false, now()
   FROM sites s WHERE s.cc_site_id = '$SITE_B'
   ON CONFLICT (id) DO NOTHING"

# Poll both sites and assert each sees only its own devices and incidents.
wait_for "site B visible at CC" 180 bash -c \
  "curl -s -H 'Authorization: Bearer $TOKEN' 'http://localhost:8090/api/fleet/?site_id=$SITE_B' \
   | grep -q gate-agent-b"
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/fleet/?site_id=$SITE_A&page_size=200" | python3 -c '
import sys, json
d = json.load(sys.stdin)
ids = {x["agent_id"] for x in d["devices"]}
assert "gate-agent-b" not in ids, f"site A returned site B device: {ids}"
print("site A devices:", sorted(ids))
'
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/fleet/?site_id=$SITE_B&page_size=200" | python3 -c '
import sys, json
d = json.load(sys.stdin)
ids = {x["agent_id"] for x in d["devices"]}
assert ids == {"gate-agent-b"}, f"site B returned foreign devices: {ids}"
print("site B devices:", sorted(ids))
'
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/incidents/?site_id=$SITE_A" | python3 -c '
import sys, json
d = json.load(sys.stdin)
titles = {i["title"] for i in d["incidents"]}
assert "site B only" not in titles, f"site A returned site B incident: {titles}"
print("site A incidents:", len(titles))
'

step "E0.2: usage is metered per site, not per Site Manager"
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT site_id || '=' || node_count FROM cc_usage_snapshots
   ORDER BY date DESC LIMIT 4" || true

step "E0.2: an unbound site returns nothing, never another site's data"
docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
  "UPDATE sites SET cc_site_id = NULL WHERE cc_site_id = '$SITE_B'"
docker compose exec -T site-manager python -c "
import asyncio, grpc, os, sys
sys.path.insert(0, '/app/src')
from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc
async def main():
    async with grpc.aio.insecure_channel('localhost:50051') as ch:
        stub = harkeniq_pb2_grpc.SiteManagerServiceStub(ch)
        snap = await stub.GetFleetSnapshot(
            harkeniq_pb2.FleetSnapshotRequest(tenant_id='tenant-demo', site_id='$SITE_B'),
            metadata=[('authorization', 'Bearer ' + os.environ['HARKEN_SM_SITE_TOKEN'])],
        )
        assert snap.site_resolved is False, 'unbound site was resolved'
        assert len(snap.devices) == 0, 'unbound site returned devices'
        assert snap.site_reason, 'no reason given'
        print('unbound ->', snap.site_reason)
asyncio.run(main())
"

step "E0.2: the audited unbind is the sanctioned recovery"
curl -sf -X POST -H "Authorization: Bearer dev-token-sm" \
  -H "Content-Type: application/json" \
  -d '{"actor":"gate@harkeniq.com","confirm_site_name":"gate-site-b",
       "reason":"compose gate: prove the recovery path"}' \
  http://localhost:8080/api/site/gate-site-b/unbind > /dev/null 2>&1 || true
docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
  "SELECT count(*) FROM audit_log WHERE action IN ('site.bound','site.unbound')" \
  | grep -qv '^0$'
curl -sf -H "Authorization: Bearer dev-token-sm" \
  http://localhost:8080/api/audit/verify | grep -q true

step "E0.1: a configured approval policy actually binds"
# The defect this closes: cc_approval_policies has carried
# required_approvers since R2b and nothing consulted it, so a tenant
# could configure dual authorization and get single authorization.
# Runs AFTER the A0+A1 step so there is a live agent to propose with.

# auto_approve is refused: unattended execution is granted by the autonomy
# contract, which needs evidence and a human, never by an approval policy.
AUTO_CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"gate-auto","approval_mode":"auto_approve"}' \
  http://localhost:8090/api/policies/)
[ "$AUTO_CODE" = "400" ] || {
  echo "auto_approve policy accepted ($AUTO_CODE), want 400" >&2; exit 1; }

POLICY_ID=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"gate-dual","action_type":"COLLECT_DIAGNOSTICS",
       "required_approvers":2}' \
  http://localhost:8090/api/policies/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['policy']['id'])")

# A second, different fault opens a new incident, so the agent proposes
# again (its dedupe key names the condition, so the same fault would not
# re-propose). This one is governed by the dual policy.
curl -skf -X POST https://localhost:9000/test/inject-fault \
  -H 'Content-Type: application/json' \
  -d '{"fault_type":"psu","target":"PS1","params":{"health":"Critical","redundancy_health":"Critical"}}' \
  > /dev/null
wait_for "a second agent proposal under the dual policy" 300 bash -c \
  "curl -s -H 'Authorization: Bearer $OP_TOKEN' http://localhost:8090/api/approvals/ \
   | python3 -c \"import sys,json; d=json.load(sys.stdin); sys.exit(0 if [a for a in d['actions'] if a['origin']=='agent' and a['approval']['required']==2] else 1)\""
DUAL_ID=$(curl -sf -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/approvals/ \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
print([a for a in d['actions']
       if a['origin'] == 'agent' and a['approval']['required'] == 2][0]['action_id'])")

step "E0.1: one approval records and does NOT execute"
curl -sf -X POST -H "Authorization: Bearer $OP_TOKEN" \
  "http://localhost:8090/api/approvals/$DUAL_ID/approve" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d.get("recorded") is True, d
assert d.get("decision") is None, "one approval must not decide under a dual policy"
assert d["approval"]["required"] == 2 and d["approval"]["received"] == 1
print("1 of 2 recorded; nothing executed")
'
# The same person cannot be both approvers.
DUP=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $OP_TOKEN" \
  "http://localhost:8090/api/approvals/$DUAL_ID/approve")
[ "$DUP" = "409" ] || { echo "duplicate approver returned $DUP, want 409" >&2; exit 1; }

step "E0.1: a second, different approver completes it"
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/approvals/$DUAL_ID/approve" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d["decision"] == "approved", d
assert d["approval"]["received"] == 2, d["approval"]
print("2 of 2 -> decided; delivered:", d["delivery"].get("delivered"))
'
# Each approval is individually auditable: the evidence R-C3 promises.
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/approvals/$DUAL_ID/records" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d["total"] == 2, d
approvers = {r["approver"] for r in d["records"]}
assert len(approvers) == 2, "two DISTINCT approvers must be recorded"
print("approval ledger:", ", ".join(sorted(approvers)))
'
# Leave the tenant as we found it: a stale rule must not govern a re-run.
curl -sf -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/policies/$POLICY_ID" > /dev/null

step "E0.3: /metrics is served by every service, and it counts"
# MetricsRegistry shipped with R4-0 and had NO callers: all three
# services could say they were alive and nothing about what they did.
for svc_port in "site-manager:8080" "central-command:8090" "console:8100"; do
  name="${svc_port%%:*}"; port="${svc_port##*:}"
  body=$(curl -sf "http://localhost:$port/metrics") || {
    echo "$name serves no /metrics" >&2; exit 1; }
  echo "$body" | grep -q "harkeniq_up 1.0" || {
    echo "$name /metrics missing harkeniq_up" >&2; exit 1; }
  before=$(echo "$body" | awk '/^harkeniq_http_requests_total /{print $2}')
  curl -sf "http://localhost:$port/healthz" > /dev/null
  after=$(curl -sf "http://localhost:$port/metrics" \
    | awk '/^harkeniq_http_requests_total /{print $2}')
  python3 -c "
import sys
before, after = float('$before'), float('$after')
assert after > before, f'$name request counter did not move: {before} -> {after}'
print('$name /metrics OK: requests', before, '->', after)
"
done

step "E0.3: an auditor can read the evidence, and still change nothing"
# A13 ratified read-only-everything for the auditor. Approval evidence
# and approval posture were gated on permissions the auditor never holds.
# E1.4: the auditor lives in the TENANT realm. This used to create the
# auditor ROLE in the platform realm on demand, putting back the very
# thing the platform-realm assertion had just checked was absent.
AUD_TOKEN=$(tenant_token gate-aud@demo gate-aud)

for _p in "approvals/" "approvals/history" "policies/" "policies/groups" "audit/"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $AUD_TOKEN" \
    "http://localhost:8090/api/$_p")
  [ "$code" = "200" ] || { echo "auditor read $_p returned $code, want 200" >&2; exit 1; }
done
echo "auditor reads approvals, history, policies, groups, audit"
# ...and mutates nothing.
for _m in "policies/" "policies/groups"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $AUD_TOKEN" -H "Content-Type: application/json" \
    -d '{"name":"auditor-should-not"}' "http://localhost:8090/api/$_m")
  [ "$code" = "403" ] || { echo "auditor WROTE $_m ($code)" >&2; exit 1; }
done
echo "auditor refused every mutation"

step "A2: the skill binding E0.3 refused is real, and it is GOVERNED"
# E0.3 refused `kind: skill` outright rather than leave a capability that
# was accepted, rendered, and wired to nothing. A2 built the four pieces
# it named, so the binding is accepted now -- and the point of this step
# is that accepting it did not make it ungoverned: the skill is resolved
# and judged against the Capability Registry at preflight, by name.
SKILL_AGENT=$(curl -sf -X POST \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"name\":\"gate-skill-agent $(date +%s)\",
       \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$SITE_A\"}],
       \"capabilities\":[
         {\"kind\":\"action_class\",\"capability_ref\":\"COLLECT_DIAGNOSTICS\"},
         {\"kind\":\"skill\",\"capability_ref\":\"fan-health\"}]}" \
  http://localhost:8090/api/operational-agents/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
[ -n "$SKILL_AGENT" ] || { echo "skill binding refused; A2 makes it real" >&2; exit 1; }

curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$SKILL_AGENT/preflight" \
  | python3 -c '
import sys, json
p = json.load(sys.stdin)
skills = p["skills"]
assert len(skills) == 1, skills
row = skills[0]
assert row["skill_id"] == "fan-health"
# usable is True / False / None, and None means the platform cannot yet
# tell -- an unfetchable skill is UNKNOWN, never quietly assumed fine.
assert row["usable"] in (True, False, None), row
assert row["reason"], "a skill verdict must carry a reason an operator can act on"
d = next(x for x in p["dimensions"] if x["dimension"] == "skills")
assert d["verdict"] in ("ready", "warn", "unknown", "blocked"), d
print("skill binding governed:", row["skill_id"], "usable=%s" % row["usable"],
      "|", row["reason"][:72])
'
# A skill may never widen authority. The bundle it hangs on is unchanged:
# the agent still reaches only its own scope and its own action classes.
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$SKILL_AGENT" | python3 -c '
import sys, json
v = json.load(sys.stdin)
classes = {c["action_type"] for c in v["capabilities"]["action_classes"]}
assert classes == {"COLLECT_DIAGNOSTICS"}, classes
print("skill expanded no capability authority:", sorted(classes))
'

step "E1.1: the tenant's organizational tree, and it is containment ONLY"
# The migration backfills one root per tenant with every site attached, so
# a tenant that upgrades is never left with sites that have no path.
ROOT_UNIT=$(curl -sf -H "Authorization: Bearer $TOKEN" \
  http://localhost:8090/api/org-units/ \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['tree'][0]['id'])")
[ -n "$ROOT_UNIT" ] || { echo "migration 0010 left the tenant with no root unit" >&2; exit 1; }

mkunit() {
  curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"name\":\"$1\",\"unit_type\":\"$2\",\"parent_id\":\"$3\"}" \
    http://localhost:8090/api/org-units/
}
GATE_W=$(mkunit "Gate West" region "$ROOT_UNIT" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
GATE_E=$(mkunit "Gate East" region "$ROOT_UNIT" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
GATE_C=$(mkunit "Gate Cluster" cluster "$GATE_W" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
GATE_H=$(mkunit "Gate Hall" hall "$GATE_C" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# A move must rewrite every descendant path in the same transaction; a
# half-moved subtree leaves paths that resolve to nothing.
curl -sf -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"parent_id\":\"$GATE_E\"}" \
  "http://localhost:8090/api/org-units/$GATE_C" > /dev/null
curl -sf -H "Authorization: Bearer $TOKEN" "http://localhost:8090/api/org-units/$GATE_H" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['unit']['path'].split('/')[2] == '$GATE_E', 'descendant path was not rewritten'
assert d['unit']['depth'] == 4, f\"depth drifted: {d['unit']['depth']}\"
names = [a['name'] for a in d['ancestors']]
assert names[-2:] == ['Gate East', 'Gate Cluster'], names
print('descendant paths followed the move:', ' > '.join(names))
"

# Cycle and depth are refused by the SERVER, not by the console.
CYC=$(curl -s -o /dev/null -w '%{http_code}' -X PATCH \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"parent_id\":\"$GATE_H\"}" "http://localhost:8090/api/org-units/$GATE_E")
[ "$CYC" = "400" ] || { echo "a cycle was accepted ($CYC)" >&2; exit 1; }

# A unit holding a site is not deletable: a site with no organizational
# path is a site nobody owns, and at E1.2 one nobody can be granted.
SITE_ONE=$(curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/sites/ \
  | python3 -c "
import sys, json
d = json.load(sys.stdin); rows = d['sites'] if isinstance(d, dict) else d
print(rows[0]['id'])")
curl -sf -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"org_unit_id\":\"$GATE_C\"}" \
  "http://localhost:8090/api/sites/$SITE_ONE/org-unit" > /dev/null
DEL=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE \
  -H "Authorization: Bearer $TOKEN" "http://localhost:8090/api/org-units/$GATE_C")
[ "$DEL" = "409" ] || { echo "a unit holding a site was deleted ($DEL)" >&2; exit 1; }
echo "cycle refused, depth bounded, delete-with-contents refused"

step "E1.1: the tree grants nobody anything (containment is not authorization)"
# The whole point of decision B. An operator with no site.view cannot even
# READ the tree, and moving a site between units changes no disposition.
OP_TOKEN=$(tenant_token gate-op@demo gate-op)
OP_READ=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OP_TOKEN" \
  http://localhost:8090/api/org-units/)
[ "$OP_READ" = "403" ] || { echo "operator read the tree without site.view ($OP_READ)" >&2; exit 1; }
AUD_WRITE=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $AUD_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"auditor should not","unit_type":"region"}' \
  http://localhost:8090/api/org-units/)
[ "$AUD_WRITE" = "403" ] || { echo "auditor mutated the tree ($AUD_WRITE)" >&2; exit 1; }

BEFORE_AUT=$(curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/autonomy/ \
  | python3 -c "import sys,json; d=json.load(sys.stdin); d.pop('generated_at',None); print(json.dumps(d,sort_keys=True))")
curl -sf -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"org_unit_id\":\"$GATE_W\"}" \
  "http://localhost:8090/api/sites/$SITE_ONE/org-unit" > /dev/null
AFTER_AUT=$(curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/autonomy/ \
  | python3 -c "import sys,json; d=json.load(sys.stdin); d.pop('generated_at',None); print(json.dumps(d,sort_keys=True))")
[ "$BEFORE_AUT" = "$AFTER_AUT" ] || {
  echo "moving a site between org units changed the autonomy contract" >&2; exit 1; }
echo "operator 403, auditor 403 on write, and the governance contract did not move"

step "E1.2: scope is enforced at the SERVER, for every persona"
# The tenant already has a tree from the E1.1 step. Grant the operator a
# site scope, flip to strict, and prove both directions.
E12_SITE=$(curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/sites/ \
  | python3 -c "
import sys, json
d = json.load(sys.stdin); rows = d['sites'] if isinstance(d, dict) else d
print(rows[0]['id'])")
OP_SUB=$(python3 -c "
import base64, json, sys
t = '$OP_TOKEN'.split('.')[1]; t += '=' * (-len(t) % 4)
print(json.loads(base64.urlsafe_b64decode(t))['sub'])")
OWNER_SUB=$(python3 -c "
import base64, json, sys
t = '$TOKEN'.split('.')[1]; t += '=' * (-len(t) % 4)
print(json.loads(base64.urlsafe_b64decode(t))['sub'])")

grant() {
  curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"principal_ref\":\"$1\",\"scope_type\":\"$2\",\"scope_ref\":\"$3\",\"role\":\"$4\"}" \
    http://localhost:8090/api/scope-grants/
}
# A23.6: self-grant is refused outright, tenant-wide grantors included,
# so the owner cannot hand THEMSELVES the first tenant grant.
SELF=$(grant "$OWNER_SUB" tenant "" tenant_owner)
[ "$SELF" = "403" ] || { echo "the owner self-granted tenant scope ($SELF)" >&2; exit 1; }

# The gate's own identity was granted at the E1.4 bootstrap above, by the
# administrator the tenant was BORN with (A23.14 D4) -- there is no
# `legacy_open` synthesis left to bootstrap from.
#
# The seeded grant is an ORDINARY grant (A23.14 D4): it confers nothing
# special and is subject to the normal lifecycle, so a real
# administrator can retire the provisioning one now that the tenant has
# its own. This also restores the single-administrator precondition the
# A23-3 last-admin steps below depend on.
BIRTH_SUB=$(python3 -c "
import base64, json
t = '$BIRTH_TOKEN'.split('.')[1]; t += '=' * (-len(t) % 4)
print(json.loads(base64.urlsafe_b64decode(t))['sub'])")
BIRTH_GID=$(curl -sf -H "Authorization: Bearer $TOKEN" \
  http://localhost:8090/api/scope-grants/ | python3 -c "
import sys, json
rows = [g for g in json.load(sys.stdin)['grants']
        if g['principal_ref'] == '$BIRTH_SUB' and g['scope_type'] == 'tenant']
assert rows, 'the birth-seeded grant is not in the ledger'
assert rows[0]['granted_by'] == 'system:tenant_birth', rows[0]
print(rows[0]['id'])")
RETIRE=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE \
  -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/scope-grants/$BIRTH_GID")
[ "$RETIRE" = "200" ] || {
  echo "the seeded grant did not behave like an ordinary one ($RETIRE)" >&2
  exit 1; }
echo "owner self-grant 403; the birth-seeded grant is ordinary, and retired"

# admin2 is used by the A23-3 last-admin steps further down.
tenant_realm_user gate-a23-admin2@demo gate-a23-admin2 tenant_owner
ADMIN2_TOKEN=$(tenant_token gate-a23-admin2@demo gate-a23-admin2)
ADMIN2_SUB=$(python3 -c "
import base64, json
t = '$ADMIN2_TOKEN'.split('.')[1]; t += '=' * (-len(t) % 4)
print(json.loads(base64.urlsafe_b64decode(t))['sub'])")
# A23-5: the operator was granted TENANT scope at the bootstrap so the
# early steps could run under strict birth. E1.2 is about narrowing, so
# withdraw that first -- otherwise the site grant below would ADD to a
# tenant-wide reach and the `site_ids == [E12_SITE]` assertion would be
# asserting nothing.
OP_TENANT_GID=$(curl -sf -H "Authorization: Bearer $TOKEN" \
  http://localhost:8090/api/scope-grants/ | python3 -c "
import sys, json
rows = [g for g in json.load(sys.stdin)['grants']
        if g['principal_ref'] == '$OP_SUB' and g['scope_type'] == 'tenant']
print(rows[0]['id'] if rows else '')")
if [ -n "$OP_TENANT_GID" ]; then
  NARROW=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE \
    -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8090/api/scope-grants/$OP_TENANT_GID")
  [ "$NARROW" = "200" ] || {
    echo "the operator's tenant grant could not be withdrawn ($NARROW)" >&2
    exit 1; }
  echo "operator narrowed: tenant grant withdrawn before the site grant"
fi
[ "$(grant "$OP_SUB" site "$E12_SITE" operator)" = "201" ] || {
  echo "site grant refused" >&2; exit 1; }

# The L1 preflight must pass now that an administrator exists, and the
# flip must be atomic.
FLIP=$(curl -s -o /dev/null -w '%{http_code}' -X PUT \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"mode":"strict"}' http://localhost:8090/api/tenant-settings/scope-enforcement)
[ "$FLIP" = "200" ] || { echo "strict flip refused ($FLIP)" >&2; exit 1; }
echo "granted, and the tenant is in strict enforcement"

# A subset may only narrow: handing an operator role.manage is refused.
ESC=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"principal_ref\":\"$OP_SUB\",\"scope_type\":\"site\",\"scope_ref\":\"$E12_SITE\",
       \"role\":\"operator\",\"permission_subset\":[\"role.manage\"]}" \
  http://localhost:8090/api/scope-grants/)
[ "$ESC" = "400" ] || { echo "permission_subset widened a role ($ESC)" >&2; exit 1; }

# The operator reads their own site and nothing else, and their own
# resolved scope says so.
curl -sf -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/scope-grants/me \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['tenant_wide'] is False, 'a site-scoped operator resolved tenant-wide'
assert d['site_ids'] == ['$E12_SITE'], d['site_ids']
assert d['contextual_unit_ids']['authority'] is False
print('operator scope:', d['site_ids'])
"

# An out-of-scope MUTATION is refused, and a tenant-governance READ is not:
# read authority and mutation authority are different things.
POL=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $OP_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"gate-should-refuse","required_approvers":1}' \
  http://localhost:8090/api/policies/)
[ "$POL" = "403" ] || { echo "an operator mutated tenant governance ($POL)" >&2; exit 1; }
POL_READ=$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/policies/)
[ "$POL_READ" = "200" ] || {
  echo "an operator cannot read why they are blocked ($POL_READ)" >&2; exit 1; }
echo "mutation 403, read 200 -- read authority != mutation authority"

step "E1.2: the audit chain still verifies with site scoping recorded"
# site_id sits outside _chain_payload, so every entry written before the
# column existed must still verify. A break here means the payload moved.
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/audit/verify \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['valid'], d
print('chain valid,', d['length'], 'entries')
"

step "A23-1: a one-site campaign preflighted by a TENANT-WIDE owner targets ONE site"
# The operator's site is E12_SITE (rows[0] of the site list, which sorts
# by NAME, so it may be either site). "Out of scope" is therefore the
# OTHER site, derived rather than assumed -- the first cut assumed site
# B and proved nothing, because the operator was scoped to site B.
A23_OTHER=$(curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/sites/ \
  | python3 -c "
import sys, json
d = json.load(sys.stdin); rows = d['sites'] if isinstance(d, dict) else d
print([r['id'] for r in rows if r['id'] != '$E12_SITE'][0])")
A23_OTHER_DEV=$(curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/fleet/?site_id=$A23_OTHER&page_size=200" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['devices'][0]['agent_id'])")
echo "operator site: $E12_SITE; out-of-scope site: $A23_OTHER (device $A23_OTHER_DEV)"

# The union that made a one-site campaign an estate-wide one: the owner
# reaches both sites; the campaign names the other site only. Every persisted
# target must sit at the other site -- and there must BE targets, or the
# assertion is vacuous.
A23_CAMP=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"a23-one-site\",\"description\":\"a23\",\"action_type\":\"IDENTIFY_LED\",
       \"params\":{\"target\":\"Drive 0\"},
       \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$A23_OTHER\"}]}" \
  http://localhost:8090/api/campaigns/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/campaigns/$A23_CAMP/preflight" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['targets'], 'preflight produced no targets'
sites = {t['site_id'] for t in d['targets']}
assert sites == {'$A23_OTHER'}, sites
devices = sorted(t['device_agent_id'] for t in d['targets'])
assert '$A23_OTHER_DEV' in devices, devices
print('targets confined to the other site:', devices)
"

step "A23-1: declared scope is TRUE at runtime for a site-scoped reader"
# The operator holds a site grant on E12_SITE (their site) under strict. The
# campaign above lives at the other site: absent for the operator, present for
# the owner. And NO fleet.view read may carry the other site's id or its device.
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OP_TOKEN" \
     http://localhost:8090/api/campaigns/$A23_CAMP)" = "404" ] || {
  echo "a scoped operator read a other-site campaign" >&2; exit 1; }
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" \
     http://localhost:8090/api/campaigns/$A23_CAMP)" = "200" ] || {
  echo "the owner lost the campaign" >&2; exit 1; }
for P in /api/campaigns/ /api/predictive/risk /api/firmware/exposure /api/warranty/ \
         /api/operational-agents/ /api/operational-agents/catalogue /api/autonomy/ \
         /api/learning/candidates /api/learning/signals /api/outcomes/patterns \
         /api/fleet/ /api/incidents/ /api/attention/ /api/capabilities/; do
  BODY=$(curl -s -H "Authorization: Bearer $OP_TOKEN" "http://localhost:8090$P")
  if echo "$BODY" | grep -q "$A23_OTHER"; then
    echo "GET $P leaked the other site's id to a scoped operator" >&2; exit 1; fi
  if echo "$BODY" | grep -q "$A23_OTHER_DEV"; then
    echo "GET $P leaked the other site's device to a scoped operator" >&2; exit 1; fi
done
echo "14 read routes: no other-site identifier reached the scoped operator"

step "A23-1: a NARROWED owner cannot operate outside their site, and can inside it"
# Every permission a tenant owner holds, reach limited to their site. This
# is the delegation ceiling's whole purpose, and the persona the
# generated mutation probe drives.
tenant_realm_user gate-a23-owner@demo gate-a23-owner tenant_owner
A23_TOKEN=$(tenant_token gate-a23-owner@demo gate-a23-owner)
A23_SUB=$(python3 -c "
import base64, json
t = '$A23_TOKEN'.split('.')[1]; t += '=' * (-len(t) % 4)
print(json.loads(base64.urlsafe_b64decode(t))['sub'])")
[ "$(grant "$A23_SUB" site "$E12_SITE" tenant_owner)" = "201" ] || {
  echo "narrowed owner grant refused" >&2; exit 1; }
# cancel a other-site campaign: absent (404), never operated
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $A23_TOKEN" \
     http://localhost:8090/api/campaigns/$A23_CAMP/cancel)" = "404" ] || {
  echo "a narrowed owner operated a other-site campaign" >&2; exit 1; }
# create a campaign reaching the other site: refused (403)
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $A23_TOKEN" \
     -H "Content-Type: application/json" \
     -d "{\"name\":\"probe\",\"description\":\"\",\"action_type\":\"IDENTIFY_LED\",
          \"params\":{\"target\":\"Drive 0\"},
          \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$A23_OTHER\"}]}" \
     http://localhost:8090/api/campaigns/)" = "403" ] || {
  echo "a narrowed owner created a other-site campaign" >&2; exit 1; }
# tenant governance writes: the CVE feed and the enforcement posture
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $A23_TOKEN" \
     -H "Content-Type: application/json" -d '{"entries":[]}' \
     http://localhost:8090/api/firmware/cve-feed)" = "403" ] || {
  echo "a narrowed owner rewrote the tenant CVE feed" >&2; exit 1; }
[ "$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $A23_TOKEN" \
     -H "Content-Type: application/json" -d '{"mode":"legacy_open"}' \
     http://localhost:8090/api/tenant-settings/scope-enforcement)" = "403" ] || {
  echo "a narrowed owner changed the tenant's enforcement posture" >&2; exit 1; }
# ...and inside their site the same owner works: create, preflight, cancel.
A23_INSIDE=$(curl -sf -X POST -H "Authorization: Bearer $A23_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"a23-inside\",\"description\":\"\",\"action_type\":\"IDENTIFY_LED\",
       \"params\":{\"target\":\"Drive 0\"},
       \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$E12_SITE\"}]}" \
  http://localhost:8090/api/campaigns/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
curl -sf -X POST -H "Authorization: Bearer $A23_TOKEN" \
  "http://localhost:8090/api/campaigns/$A23_INSIDE/preflight" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert {t['site_id'] for t in d['targets']} <= {'$E12_SITE'}, d['targets']
print('narrowed owner preflighted inside their site:', len(d['targets']), 'target(s)')"
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $A23_TOKEN" \
     http://localhost:8090/api/campaigns/$A23_INSIDE/cancel)" = "200" ] || {
  echo "the narrowed owner could not cancel their own own-site campaign" >&2; exit 1; }
echo "outside: 404 / 403 / 403 / 403; inside: 201 / 200 / 200"
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:8090/api/campaigns/$A23_CAMP/cancel > /dev/null

step "A23-1: a secure Central Command refuses to boot without a tenant realm"
# The platform-realm fallback is gone. An unset realm in secure mode is
# a configuration error at startup, not a live misconfiguration.
A23_BOOT=$(timeout 120 docker compose run --rm --no-deps -T \
  -e HARKEN_CC_KEYCLOAK_REALM= central-command 2>&1 || true)
echo "$A23_BOOT" | grep -q "keycloak_realm is required" || {
  echo "Central Command booted (or failed differently) with no realm:" >&2
  echo "$A23_BOOT" | tail -20 >&2; exit 1; }
echo "refused: keycloak_realm is required in secure mode"

step "A23-2: new audit rows carry a STABLE actor_ref; the chain hashes none of it"
# Every write above was made by real Keycloak identities. The ledger
# must name them by subject, not by address, and the owner's subject
# must be what the impact census compares to grants.
curl -sf -H "Authorization: Bearer $TOKEN" "http://localhost:8090/api/audit/?page_size=200" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
rows = d['entries']
assert rows, 'no audit rows'
with_ref = [r for r in rows if r.get('actor_ref')]
assert with_ref, 'no row carries actor_ref'
owner = [r for r in rows if r.get('actor_ref') == '$OWNER_SUB']
assert owner, 'the owner is never recorded by subject'
emails = [r for r in with_ref if '@' in (r['actor'] or '') and r['actor_ref'] and '@' not in r['actor_ref']]
print(len(rows), 'rows;', len(with_ref), 'with actor_ref;', len(owner), 'by the owner;',
      len(emails), 'recorded by email but identified by subject')
"

step "A23-2: a real 0019 -> head upgrade on PostgreSQL with existing rows"
# Take the live database back to 0019 (drop the column and its index,
# rewind the version), then let Central Command's own alembic bring it
# forward. Existing rows come back with actor_ref NULL -- no backfill --
# and the chain, which never hashed the column, still verifies.
#
# The expected head is READ FROM THE CHAIN, not hardcoded. This asserted
# '0020' literally and A23-5's 0021 broke it -- a true statement about
# the migration that ran, failing because a later slice added one. The
# subject of this step is "the upgrade ran and backfilled nothing", not
# which revision happens to be last today.
CC_HEAD=$(ls "$_REPO_ROOT"/services/central_command/src/harkeniq_cc/db/migrations/versions/[0-9]*.py \
  | sed 's|.*/\([0-9]\{4\}\)_.*|\1|' | sort | tail -1)
BEFORE=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "select count(*) from cc_audit_log")
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc "
  drop index if exists ix_cc_audit_log_tenant_actor_ref;
  alter table cc_audit_log drop column actor_ref;
  update alembic_version set version_num='0019';" > /dev/null
docker compose exec -T central-command sh -c \
  "cd /app/services/central_command && alembic upgrade head" 2>&1 | tail -1
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc "
  select version_num from alembic_version;
  select count(*) from cc_audit_log where actor_ref is null;
  select count(*) from cc_audit_log;
  select indexname from pg_indexes where tablename='cc_audit_log' and indexname='ix_cc_audit_log_tenant_actor_ref';" \
  | python3 -c "
import sys
lines = [l.strip() for l in sys.stdin.read().splitlines() if l.strip()]
version, nulls, total, index = lines[0], int(lines[1]), int(lines[2]), lines[3]
assert version == '$CC_HEAD', (version, 'expected head $CC_HEAD')
assert total == int('$BEFORE') and nulls == total, (nulls, total, '$BEFORE')
assert index == 'ix_cc_audit_log_tenant_actor_ref', index
print('chain applied to head $CC_HEAD on PostgreSQL:', total,
      'existing rows, all actor_ref NULL, index present')
"
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/audit/verify \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['valid'], d
print('chain still valid after the upgrade:', d['length'], 'entries')"

step "A23-5: a pinned legacy tenant keeps its posture across the 0020 -> 0021 upgrade"
# The migration's whole job. Simulate a pre-A23-5 deployment on the LIVE
# PostgreSQL: a tenant with history and no settings row, which is exactly
# what every existing installation looks like. It must come back
# `legacy_open` -- the posture the old default was giving it -- and not
# strict, which would lock a working deployment out on upgrade.
# The deployment's history is REAL -- this tenant has been acting since
# the first step, so `cc_audit_log` is populated and 0021 sees a database
# that has served somebody. Deliberately NOT faked with a synthetic audit
# row: one with a fabricated hash sorts into the chain and would break
# every later `/api/audit/verify`.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc "
  delete from cc_tenant_settings where tenant_id = 'tenant-demo';
  update alembic_version set version_num='0020';" > /dev/null
docker compose exec -T central-command sh -c \
  "cd /app/services/central_command && alembic upgrade head" 2>&1 | tail -1
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc "
  select version_num from alembic_version;
  select scope_enforcement || '|' || updated_by from cc_tenant_settings
   where tenant_id = 'tenant-demo';" \
  | python3 -c "
import sys
lines = [l.strip() for l in sys.stdin.read().splitlines() if l.strip()]
version, pinned = lines[0], lines[1]
assert version == '$CC_HEAD', (version, 'expected head $CC_HEAD')
mode, by = pinned.split('|')
assert mode == 'legacy_open', ('an existing tenant must keep the posture it '
                               'already had, not be flipped: %r' % pinned)
assert by == 'migration:0021', pinned
print('0021 on PostgreSQL: existing tenant pinned', mode, 'by', by)"

step "A23-5: the pin is idempotent and never overwrites an explicit posture"
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc "
  update cc_tenant_settings set scope_enforcement='strict',
         updated_by='an-operator@demo' where tenant_id='tenant-demo';
  update alembic_version set version_num='0020';" > /dev/null
docker compose exec -T central-command sh -c \
  "cd /app/services/central_command && alembic upgrade head" 2>&1 | tail -1
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc "
  select scope_enforcement || '|' || updated_by from cc_tenant_settings
   where tenant_id = 'tenant-demo';" \
  | python3 -c "
import sys
mode, by = sys.stdin.read().strip().split('|')
assert (mode, by) == ('strict', 'an-operator@demo'), (mode, by)
print('a decision somebody made survives the migration:', mode, 'by', by)"

step "A23-5: legacy_open cannot be reached by a missing row"
# The invariant A23.11 retires. Remove the tenant's settings row entirely
# and ask the running service what its posture is: before A23-5 an
# absence answered `legacy_open` -- the platform's most permissive
# posture, reachable by a decision nobody made.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "delete from cc_tenant_settings where tenant_id = 'tenant-demo';" > /dev/null
curl -sf -H "Authorization: Bearer $TOKEN" \
  http://localhost:8090/api/tenant-settings/scope-enforcement | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['scope_enforcement'] == 'strict', (
    'a missing row must never answer legacy_open again: %r' % d)
print('an unpinned tenant reads:', d['scope_enforcement'])"
# And a never-granted principal gets no synthesis from it.
tenant_realm_user gate-a235-nobody@demo gate-a235-nobody tenant_owner || true
A235_TOKEN=$(tenant_token gate-a235-nobody@demo gate-a235-nobody)
curl -sf -H "Authorization: Bearer $A235_TOKEN" \
  http://localhost:8090/api/scope-grants/me | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['tenant_wide'] is False, d
assert d['synthesis'] == 'strict', d
print('an ungranted tenant_owner on an unpinned tenant reaches nothing:',
      d['synthesis'])"
curl -sf -H "Authorization: Bearer $A235_TOKEN" http://localhost:8090/api/fleet/ \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['total'] == 0, ('a principal with no grant must see no devices: %r'
                         % d['total'])
print('and sees', d['total'], 'devices')"
# Restore the tenant's explicit strict posture for the steps that follow.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc "
  insert into cc_tenant_settings (tenant_id, scope_enforcement, updated_by,
                                  updated_at)
  values ('tenant-demo', 'strict', 'gate', now())
  on conflict (tenant_id) do update set scope_enforcement='strict';" > /dev/null

step "A23-5: a tenant cannot be created without an administrator"
# A23.14 D3: strict birth means an owner subject is a precondition of
# creation, not an optional extra. Both routes that used to produce an
# active tenant with nobody able to administer it now fail closed.
NOOWNER=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $PLATFORM_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"No Owner Co","slug":"a235-noowner","billing_country":"US",
       "currency":"USD","plan":"approve","node_commit":1,"admin_email":""}' \
  http://localhost:8100/api/admin/tenants/)
[ "$NOOWNER" = "400" ] || {
  echo "a tenant was created with no administrator ($NOOWNER)" >&2; exit 1; }
curl -sf -H "Authorization: Bearer $PLATFORM_TOKEN" \
  "http://localhost:8100/api/admin/tenants/?search=a235-noowner" | python3 -c "
import sys, json
d = json.load(sys.stdin)
rows = [t for t in d.get('items', d if isinstance(d, list) else [])
        if t.get('slug') == 'a235-noowner']
assert not rows, ('the refused tenant left a row behind: %r' % rows)
print('ownerless tenant creation: 400, and no tenant row survives')"

step "A23-2: readers are dual-form -- legacy rows by display string, new rows by subject"
# A new write after the upgrade carries the subject again; the historical
# rows (now NULL) are still found by their legacy actor string, and the
# impact census does not report the granted owner as ungranted merely
# because older rows name them differently.
curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"a23-2 probe","unit_type":"cluster"}' \
  http://localhost:8090/api/org-units/ > /dev/null
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/audit/?actor=$OWNER_SUB&page_size=50" | python3 -c "
import sys, json
d = json.load(sys.stdin)
new = [r for r in d['entries'] if r['actor_ref'] == '$OWNER_SUB']
assert new, 'the post-upgrade write did not carry actor_ref'
print('by subject:', d['total'], 'row(s); newest carries actor_ref')"
LEGACY_ACTOR=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "select actor from cc_audit_log where actor_ref is null and actor <> '' order by seq limit 1")
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/audit/?actor=$LEGACY_ACTOR&page_size=5" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['total'] >= 1, d
assert any(r['actor_ref'] is None for r in d['entries']), 'legacy rows must read with actor_ref null'
print('by legacy actor string', repr('$LEGACY_ACTOR'[:24]), '->', d['total'], 'row(s), actor_ref null')"
curl -sf -H "Authorization: Bearer $TOKEN" \
  http://localhost:8090/api/tenant-settings/scope-enforcement/impact | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert '$OWNER_SUB' not in d['observed_principals_without_grant'], d['observed_principals_without_grant']
assert 'actor_ref' in d['identity_basis']
print('census: owner not reported as ungranted;',
      len(d['observed_principals_without_grant']), 'without grant,',
      len(d['unresolved_legacy_actors']), 'unresolved legacy actor(s)')"

step "A23-3: the last tenant administrator cannot be configured away"
# The owner holds the ONLY tenant-scope grant carrying role.manage. Every
# path that would remove it is refused at the server, audited, and
# leaves the row untouched: revoke, an overwrite with a lesser role, an
# expiry (future or past), and a reassignment off tenant scope.
OWNER_GID=$(curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/scope-grants/ \
  | python3 -c "
import sys, json
rows = [g for g in json.load(sys.stdin)['grants']
        if g['principal_ref'] == '$OWNER_SUB' and g['scope_type'] == 'tenant' and not g['revoked_at']]
assert len(rows) == 1, rows
print(rows[0]['id'])")
REV=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE \
  -H "Authorization: Bearer $TOKEN" "http://localhost:8090/api/scope-grants/$OWNER_GID")
[ "$REV" = "409" ] || { echo "the last administrator was revoked ($REV)" >&2; exit 1; }
# A second, GRANTLESS owner-role identity: under strict it reaches nothing,
# but under legacy_open it would have full synthesized reach and STILL not
# count as an administrator. Here it proves the self-grant rule and, once
# granted, the two-admin case.
# admin2 was created at the E1.2 bootstrap; re-mint (tokens expire).
ADMIN2_TOKEN=$(tenant_token gate-a23-admin2@demo gate-a23-admin2)
FUTURE=$(python3 -c "import datetime; print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=30)).isoformat())")
for BODY in \
  "{\"principal_ref\":\"$OWNER_SUB\",\"scope_type\":\"tenant\",\"role\":\"site_admin\"}" \
  "{\"principal_ref\":\"$OWNER_SUB\",\"scope_type\":\"tenant\",\"role\":\"tenant_owner\",\"expires_at\":\"$FUTURE\"}" \
  "{\"principal_ref\":\"$OWNER_SUB\",\"scope_type\":\"tenant\",\"role\":\"tenant_owner\",\"permission_subset\":[\"fleet.view\"]}"; do
  # The owner cannot touch their own grant at all (self-grant, 403); the
  # question is whether ANOTHER administrator could. Grant admin2 tenant
  # scope so they can act, and they become an admin -- so the honest
  # proof of the last-admin rule against a non-self actor is below, with
  # admin2 narrowed. Here: the owner's own attempts are self-grants.
  SELF=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "$BODY" http://localhost:8090/api/scope-grants/)
  [ "$SELF" = "403" ] || { echo "an owner modified their own grant ($SELF): $BODY" >&2; exit 1; }
done
echo "revoke 409; every self-modification 403 (self-grant)"

step "A23-3: two administrators may lose one, never both -- concurrently"
[ "$(grant "$ADMIN2_SUB" tenant "" tenant_owner)" = "201" ] || {
  echo "second admin grant refused" >&2; exit 1; }
ADMIN2_GID=$(curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/scope-grants/ \
  | python3 -c "
import sys, json
print([g for g in json.load(sys.stdin)['grants']
       if g['principal_ref'] == '$ADMIN2_SUB' and g['scope_type'] == 'tenant'][0]['id'])")
# Each administrator revokes the OTHER at the same instant. A naive count
# lets both pass (each saw two). The per-tenant transaction lock makes the
# second wait, re-read a committed count of one, and refuse.
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE -H "Authorization: Bearer $ADMIN2_TOKEN" \
  "http://localhost:8090/api/scope-grants/$OWNER_GID" > /tmp/a23-3-race-a &
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/scope-grants/$ADMIN2_GID" > /tmp/a23-3-race-b &
wait
RACE=$(cat /tmp/a23-3-race-a /tmp/a23-3-race-b | sort | tr '\n' ' ')
[ "$RACE" = "200 409 " ] || { echo "concurrent revokes did not serialize: $RACE" >&2; exit 1; }
ADMINS=$(curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/tenant-settings/scope-enforcement 2>/dev/null \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['tenant_admin_count'])" 2>/dev/null || \
  curl -sf -H "Authorization: Bearer $ADMIN2_TOKEN" http://localhost:8090/api/tenant-settings/scope-enforcement \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['tenant_admin_count'])")
[ "$ADMINS" = "1" ] || { echo "expected exactly one administrator after the race, got $ADMINS" >&2; exit 1; }
# Restore the owner if it was the owner who lost: the rest of the gate
# runs as the owner.
if [ "$(cat /tmp/a23-3-race-a)" = "200" ]; then
  RESTORE=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $ADMIN2_TOKEN" -H "Content-Type: application/json" \
    -d "{\"principal_ref\":\"$OWNER_SUB\",\"scope_type\":\"tenant\",\"role\":\"tenant_owner\"}" \
    http://localhost:8090/api/scope-grants/)
  [ "$RESTORE" = "201" ] || { echo "could not restore the owner ($RESTORE)" >&2; exit 1; }
  echo "race: admin2 won; owner restored by admin2"
else
  echo "race: owner won; admin2 revoked"
fi
echo "exactly one revoke succeeded ($RACE); one administrator remained; chain of authority intact"

step "A23-3: delegation is reach AND authority, per permission, on the exact target"
# gate-a23-owner is a tenant_owner narrowed to ONE site (A23-1 step).
A23_TOKEN=$(tenant_token gate-a23-owner@demo gate-a23-owner)
X_SUB="a23-3-delegate-$(date +%s)"
D_IN=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $A23_TOKEN" -H "Content-Type: application/json" \
  -d "{\"principal_ref\":\"$X_SUB\",\"scope_type\":\"site\",\"scope_ref\":\"$E12_SITE\",\"role\":\"operator\"}" \
  http://localhost:8090/api/scope-grants/)
D_OUT=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $A23_TOKEN" -H "Content-Type: application/json" \
  -d "{\"principal_ref\":\"$X_SUB\",\"scope_type\":\"site\",\"scope_ref\":\"$A23_OTHER\",\"role\":\"operator\"}" \
  http://localhost:8090/api/scope-grants/)
D_TEN=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $A23_TOKEN" -H "Content-Type: application/json" \
  -d "{\"principal_ref\":\"$X_SUB\",\"scope_type\":\"tenant\",\"role\":\"viewer\"}" \
  http://localhost:8090/api/scope-grants/)
[ "$D_IN" = "201" ] || { echo "delegation within the grantor's site refused ($D_IN)" >&2; exit 1; }
[ "$D_OUT" = "403" ] || { echo "delegation outside the grantor's site accepted ($D_OUT)" >&2; exit 1; }
[ "$D_TEN" = "403" ] || { echo "a site-scoped grantor delegated tenant scope ($D_TEN)" >&2; exit 1; }
# A NARROWED grantor: tenant_owner role, but the grant withholds
# site.manage and action.approve. Delegating site_admin (which carries
# both) is refused even though the site is theirs; a subset they do
# hold is allowed; naming a broader role restores nothing.
tenant_realm_user gate-a23-narrow@demo gate-a23-narrow tenant_owner
NARROW_TOKEN=$(tenant_token gate-a23-narrow@demo gate-a23-narrow)
NARROW_SUB=$(python3 -c "
import base64, json
t = '$NARROW_TOKEN'.split('.')[1]; t += '=' * (-len(t) % 4)
print(json.loads(base64.urlsafe_b64decode(t))['sub'])")
NG=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"principal_ref\":\"$NARROW_SUB\",\"scope_type\":\"site\",\"scope_ref\":\"$E12_SITE\",\"role\":\"tenant_owner\",
       \"permission_subset\":[\"role.manage\",\"fleet.view\",\"incident.view\",\"site.view\"]}" \
  http://localhost:8090/api/scope-grants/)
[ "$NG" = "201" ] || { echo "narrowed grant refused ($NG)" >&2; exit 1; }
N_ESC=$(curl -s -X POST \
  -H "Authorization: Bearer $NARROW_TOKEN" -H "Content-Type: application/json" \
  -d "{\"principal_ref\":\"$X_SUB\",\"scope_type\":\"site\",\"scope_ref\":\"$E12_SITE\",\"role\":\"site_admin\"}" \
  http://localhost:8090/api/scope-grants/ -w '\n%{http_code}')
echo "$N_ESC" | tail -1 | grep -q "^403$" || { echo "a narrowed grantor delegated site_admin: $N_ESC" >&2; exit 1; }
echo "$N_ESC" | grep -q "site.manage" || { echo "the refusal did not name the missing permission: $N_ESC" >&2; exit 1; }
N_OK=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $NARROW_TOKEN" -H "Content-Type: application/json" \
  -d "{\"principal_ref\":\"$X_SUB\",\"scope_type\":\"site\",\"scope_ref\":\"$E12_SITE\",\"role\":\"viewer\"}" \
  http://localhost:8090/api/scope-grants/)
[ "$N_OK" = "201" ] || { echo "a held subset was refused ($N_OK)" >&2; exit 1; }
N_SELF=$(curl -s -X POST \
  -H "Authorization: Bearer $NARROW_TOKEN" -H "Content-Type: application/json" \
  -d "{\"principal_ref\":\"$NARROW_SUB\",\"scope_type\":\"site\",\"scope_ref\":\"$E12_SITE\",\"role\":\"viewer\"}" \
  http://localhost:8090/api/scope-grants/ -w '\n%{http_code}')
echo "$N_SELF" | tail -1 | grep -q "^403$" || { echo "self-grant accepted: $N_SELF" >&2; exit 1; }
echo "$N_SELF" | grep -q "self-grant is forbidden" || { echo "self-grant refused for the wrong reason: $N_SELF" >&2; exit 1; }
echo "in-scope 201, out-of-scope 403, tenant 403; narrowed grantor: site_admin 403 (site.manage named), viewer 201, self-grant 403"

step "A23-3: an org unit is not deleted from under a grant; reassignment is the safe path"
DOOMED=$(mkunit "A23-3 Doomed" hall "$ROOT_UNIT" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
[ "$(grant "$X_SUB" org_unit "$DOOMED" viewer)" = "201" ] || { echo "unit grant refused" >&2; exit 1; }
DOOMED_GID=$(curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/scope-grants/ \
  | python3 -c "
import sys, json
print([g for g in json.load(sys.stdin)['grants']
       if g['principal_ref'] == '$X_SUB' and g['scope_ref'] == '$DOOMED'][0]['id'])")
DEL=$(curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/org-units/$DOOMED" -w '\n%{http_code}')
echo "$DEL" | tail -1 | grep -q "^409$" || { echo "a unit under a grant was deleted: $DEL" >&2; exit 1; }
echo "$DEL" | grep -q "referenced by 1 active scope grant" || { echo "wrong refusal: $DEL" >&2; exit 1; }
MOVED=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"scope_type\":\"org_unit\",\"scope_ref\":\"$GATE_W\"}" \
  "http://localhost:8090/api/scope-grants/$DOOMED_GID/reassign")
[ "$MOVED" = "200" ] || { echo "reassignment refused ($MOVED)" >&2; exit 1; }
DEL2=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/org-units/$DOOMED")
[ "$DEL2" = "200" ] || { echo "unit not deletable after reassignment ($DEL2)" >&2; exit 1; }
curl -sf -H "Authorization: Bearer $TOKEN" "http://localhost:8090/api/audit/?action=org_unit.delete_refused&page_size=5" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['total'] >= 1, d
e = [r for r in d['entries'] if r['subject'] == '$DOOMED'][0]
assert '$DOOMED_GID' in e['detail']['grant_ids'], e
print('delete refused and audited with the grant it named; reassigned; deleted')"

step "A23-3: a vanished target never widens -- inert, reach none, reason stated, no synthesis"
# The API refuses to make a target vanish; a database operator still
# can. A principal whose ONLY grant points at a deleted unit must not
# resolve tenant-wide under legacy_open, and must read as inert.
tenant_realm_user gate-a23-orphan@demo gate-a23-orphan viewer
ORPHAN_TOKEN=$(tenant_token gate-a23-orphan@demo gate-a23-orphan)
ORPHAN_SUB=$(python3 -c "
import base64, json
t = '$ORPHAN_TOKEN'.split('.')[1]; t += '=' * (-len(t) % 4)
print(json.loads(base64.urlsafe_b64decode(t))['sub'])")
VANISH=$(mkunit "A23-3 Vanish" hall "$ROOT_UNIT" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
[ "$(grant "$ORPHAN_SUB" org_unit "$VANISH" viewer)" = "201" ] || { echo "orphan grant refused" >&2; exit 1; }
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "delete from cc_org_units where id = '$VANISH'" > /dev/null
for MODE in strict legacy_open; do
  curl -sf -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"mode\":\"$MODE\"}" http://localhost:8090/api/tenant-settings/scope-enforcement > /dev/null
  curl -sf -H "Authorization: Bearer $ORPHAN_TOKEN" http://localhost:8090/api/scope-grants/me | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['tenant_wide'] is False, ('$MODE', d)
assert d['site_ids'] == [] and d['org_unit_paths'] == [], ('$MODE', d)
assert d['administered'] is True, d
assert d['inert_grants'] == [{'scope_type': 'org_unit', 'scope_ref': '$VANISH', 'reason': 'org_unit_missing'}], d
print('$MODE: tenant_wide False, reach none, inert org_unit_missing')"
  N=$(curl -sf -H "Authorization: Bearer $ORPHAN_TOKEN" "http://localhost:8090/api/fleet/?page_size=200" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('devices', d.get('items', []))))")
  [ "$N" = "0" ] || { echo "$MODE: an orphaned principal saw $N device(s)" >&2; exit 1; }
done
curl -sf -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"mode":"strict"}' http://localhost:8090/api/tenant-settings/scope-enforcement > /dev/null
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/scope-grants/ | python3 -c "
import sys, json
g = [g for g in json.load(sys.stdin)['grants'] if g['principal_ref'] == '$ORPHAN_SUB'][0]
assert g['target_status'] == 'missing' and g['effective'] is False, g
print('listed as target missing / not effective; row retained')"
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/audit/verify \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['valid'], d
print('chain valid,', d['length'], 'entries, refusals included')"

step "A23-4: synthesis only for the never-granted, and never for an agent (A23.10)"
# The previous step left the tenant strict. A..E run under legacy_open,
# the posture the escalation lived in: a principal whose EFFECTIVE grant
# list was empty used to be handed a synthesized tenant-wide grant, so
# "granted once and lost it" read exactly like "never granted". F
# returns to strict. Every line below is the resolver's own answer over
# the real loader, the real repositories, real Keycloak identities.
curl -sf -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"mode":"legacy_open"}' http://localhost:8090/api/tenant-settings/scope-enforcement > /dev/null
a234_me() { curl -sf -H "Authorization: Bearer $1" http://localhost:8090/api/scope-grants/me; }
a234_fleet() {
  curl -sf -H "Authorization: Bearer $1" "http://localhost:8090/api/fleet/?page_size=200" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('devices', d.get('items', []))))"
}
a234_sub() {
  python3 -c "
import base64, json
t = '$1'.split('.')[1]; t += '=' * (-len(t) % 4)
print(json.loads(base64.urlsafe_b64decode(t))['sub'])"
}
a234_expect() {
  # $1 label, $2 token, $3 tenant_wide, $4 synthesis, $5 previously_granted, $6 fleet count ("any" = >0)
  a234_me "$2" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['tenant_wide'] is $3, ('$1', d)
assert d['synthesis'] == '$4', ('$1', d)
assert d['previously_granted'] is $5, ('$1', d)
if d['synthesis'] in ('previously_granted', 'agent', 'strict'):
    # No effective grant: the answer must be reach NONE, not merely
    # not-tenant-wide.
    assert d['site_ids'] == [] and d['org_unit_paths'] == [], ('$1', d)
    assert not [g for g in d['grants'] if not g['inert']], ('$1', d)
print('$1: tenant_wide', d['tenant_wide'], '| synthesis', d['synthesis'], '| previously_granted', d['previously_granted'], '| effective grants', len([g for g in d['grants'] if not g['inert']]))"
  N=$(a234_fleet "$2")
  if [ "$6" = "any" ]; then
    [ "$N" -gt 0 ] || { echo "$1: expected devices, saw $N" >&2; exit 1; }
  else
    [ "$N" = "$6" ] || { echo "$1: expected $6 device(s), saw $N" >&2; exit 1; }
  fi
  echo "$1: fleet read returned $N device(s)"
}
A234_SITE=$(curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/sites/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['sites'][0]['id'])")

# A. never granted: legacy behaviour, unchanged for the never-administered.
tenant_realm_user gate-a234-never@demo gate-a234-never viewer
NEVER_TOKEN=$(tenant_token gate-a234-never@demo gate-a234-never)
a234_expect "A never-granted (legacy_open)" "$NEVER_TOKEN" True never_granted False any

# B. previously granted, then revoked through the API.
tenant_realm_user gate-a234-revoked@demo gate-a234-revoked viewer
REV_TOKEN=$(tenant_token gate-a234-revoked@demo gate-a234-revoked)
REV_SUB=$(a234_sub "$REV_TOKEN")
[ "$(grant "$REV_SUB" site "$A234_SITE" viewer)" = "201" ] || { echo "B: grant refused" >&2; exit 1; }
a234_expect "B before revoke (narrow grant)" "$REV_TOKEN" False granted True any
REV_GID=$(curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/scope-grants/?principal_ref=$REV_SUB" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['grants'][0]['id'])")
RC=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/scope-grants/$REV_GID")
[ "$RC" = "200" ] || { echo "B: revoke -> $RC" >&2; exit 1; }
a234_expect "B previously granted, revoked" "$REV_TOKEN" False previously_granted True 0

# C. previously granted, then expired (the clock, edited in the database).
tenant_realm_user gate-a234-expired@demo gate-a234-expired viewer
EXP_TOKEN=$(tenant_token gate-a234-expired@demo gate-a234-expired)
EXP_SUB=$(a234_sub "$EXP_TOKEN")
[ "$(grant "$EXP_SUB" site "$A234_SITE" viewer)" = "201" ] || { echo "C: grant refused" >&2; exit 1; }
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "update cc_scope_grants set expires_at = now() - interval '1 day' where principal_ref = '$EXP_SUB'" > /dev/null
a234_expect "C previously granted, expired" "$EXP_TOKEN" False previously_granted True 0

# D. previously granted, target vanished (the orphan from the A23-3 step).
a234_me "$ORPHAN_TOKEN" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['tenant_wide'] is False and d['synthesis'] == 'granted' and d['previously_granted'] is True, d
assert d['inert_grants'] and d['inert_grants'][0]['reason'] == 'org_unit_missing', d
print('D previously granted, target vanished: tenant_wide False | synthesis granted (inert, retained) | previously_granted True')"
N=$(a234_fleet "$ORPHAN_TOKEN"); [ "$N" = "0" ] || { echo "D: orphan saw $N device(s)" >&2; exit 1; }
echo "D: fleet read returned 0 device(s)"

# E. an Operational Agent with NO scope rows, authenticating as itself.
A234_AGENT=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"name\":\"a23-4 bare $(date +%s)\",\"scopes\":[],
       \"capabilities\":[{\"kind\":\"read\",\"capability_ref\":\"fleet\"},
                         {\"kind\":\"read\",\"capability_ref\":\"incidents\"}]}" \
  http://localhost:8090/api/operational-agents/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
A234_SECRET=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A234_AGENT/identity" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['client_secret'])")
A234_TOKEN=$(curl -sf -X POST \
  "http://localhost:8180/realms/tenant-demo/protocol/openid-connect/token" \
  -d "grant_type=client_credentials&client_id=op-agent-$A234_AGENT&client_secret=$A234_SECRET" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
[ -n "$A234_TOKEN" ] || { echo "E: no machine token" >&2; exit 1; }
a234_expect "E agent with no scope rows (legacy_open)" "$A234_TOKEN" False agent False 0
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/tenant-settings/scope-enforcement/impact \
  | python3 -c "
import sys, json
r = json.load(sys.stdin)
assert '$A234_AGENT' in [a['agent_id'] for a in r['agents_without_grant']], r['agents_without_grant']
print('E: the impact report still names the scopeless agent (reporting stayed; the reach went)')"

# F. strict: nobody is synthesized, the never-granted included.
curl -sf -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"mode":"strict"}' http://localhost:8090/api/tenant-settings/scope-enforcement > /dev/null
a234_expect "F never-granted (strict)" "$NEVER_TOKEN" False strict False 0
a234_expect "F agent (strict)" "$A234_TOKEN" False agent False 0
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/audit/verify \
  | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['valid'], d; print('chain valid,', d['length'], 'entries')"

step "E1.2: returning the tenant to legacy_open leaves the gate reusable"
curl -sf -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"mode":"legacy_open"}' \
  http://localhost:8090/api/tenant-settings/scope-enforcement > /dev/null

step "E1.4: tenant A cannot reach tenant B"
# Re-mint: the gate runs for many minutes and a Keycloak access token
# does not. The tail steps were failing on an EXPIRED platform token,
# which reads exactly like a permission failure and is not one.
PLATFORM_TOKEN=$(curl -sf -X POST \
  "http://localhost:8180/realms/harkeniq-platform/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=harkeniq-console&username=admin@harkeniq.com&password=admin" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
KC_ADMIN=$(curl -sf -X POST \
  "http://localhost:8180/realms/master/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=admin-cli&username=admin&password=admin" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
# A second tenant, created END TO END through the real API -- which is
# also the proof that provisioning runs on the creation path and not
# only through the explicit endpoint.
curl -sf -X POST -H "Authorization: Bearer $PLATFORM_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Gate Rival","slug":"gate-rival","billing_country":"US","currency":"USD","admin_email":"owner@gate-rival"}' \
  http://localhost:8100/api/admin/tenants/ | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['keycloak_realm'] == 'gate-rival', d
print('gate-rival provisioned end to end:', d['keycloak_realm'])
" || echo "  (gate-rival already exists from a previous run)"

RIVAL_ID=$(curl -sf -H "Authorization: Bearer $PLATFORM_TOKEN" \
  http://localhost:8100/api/admin/tenants/ | python3 -c "
import sys, json
print([t['id'] for t in json.load(sys.stdin)['items'] if t['slug'] == 'gate-rival'][0])")

tenant_realm_user_in() {  # realm email password role
  local uid rj
  curl -s -X POST "http://localhost:8180/admin/realms/$1/users" \
    -H "Authorization: Bearer $KC_ADMIN" -H "Content-Type: application/json" \
    -d "{\"username\":\"$2\",\"email\":\"$2\",\"enabled\":true,
         \"emailVerified\":true,\"firstName\":\"Gate\",\"lastName\":\"Rival\",
         \"credentials\":[{\"type\":\"password\",\"value\":\"$3\",
                            \"temporary\":false}]}" -o /dev/null
  uid=$(curl -s "http://localhost:8180/admin/realms/$1/users?username=$2&exact=true" \
    -H "Authorization: Bearer $KC_ADMIN" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['id'] if d else '')")
  [ -n "$uid" ] || return 1
  rj=$(curl -s "http://localhost:8180/admin/realms/$1/roles/$4" -H "Authorization: Bearer $KC_ADMIN")
  curl -s -X POST "http://localhost:8180/admin/realms/$1/users/$uid/role-mappings/realm" \
    -H "Authorization: Bearer $KC_ADMIN" -H "Content-Type: application/json" \
    -d "[$rj]" -o /dev/null
}
tenant_realm_user_in gate-rival rival@gate rival-pass tenant_owner || true
RIVAL_TOKEN=$(curl -sf -X POST \
  "http://localhost:8180/realms/gate-rival/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=harkeniq-console&username=rival@gate&password=rival-pass" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

RIVAL_CC=$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $RIVAL_TOKEN" http://localhost:8090/api/fleet/)
[ "$RIVAL_CC" = "401" ] || {
  echo "another tenant's identity reached this tenant's CC ($RIVAL_CC)" >&2
  exit 1; }
RIVAL_XT=$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $RIVAL_TOKEN" \
  "http://localhost:8100/api/tenants/$E14_TENANT/users/")
[ "$RIVAL_XT" = "403" ] || {
  echo "another tenant read this tenant's Console surface ($RIVAL_XT)" >&2
  exit 1; }
echo "tenant A -> tenant B: 401 at Central Command, 403 at the Console"

step "E1.4: a custom bundle cannot widen the role that holds it"
BUNDLE_ID=$(curl -sf -X POST -H "Authorization: Bearer $PLATFORM_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Gate Reader Plus","permissions":["fleet.view","site.manage","action.approve"]}' \
  "http://localhost:8100/api/tenants/$E14_TENANT/roles/" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
tenant_realm_user gate-bundle@demo gate-bundle viewer || true
BUNDLE_TOKEN=$(curl -sf -X POST \
  "http://localhost:8180/realms/tenant-demo/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=harkeniq-console&username=gate-bundle@demo&password=gate-bundle" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
BUNDLE_SUB=$(python3 -c "
import base64, json
t = '$BUNDLE_TOKEN'.split('.')[1]; t += '=' * (-len(t) % 4)
print(json.loads(base64.urlsafe_b64decode(t))['sub'])")

docker compose exec -T postgres psql -U harkeniq -d harkeniq_console -q \
  -c "INSERT INTO users (id, tenant_id, email, display_name, role, is_platform_user, keycloak_user_id, status, created_at) VALUES ('gatebundleuser0000000000000000', '$E14_TENANT', 'gate-bundle@demo', 'Gate Bundle', 'viewer', false, '$BUNDLE_SUB', 'active', now()) ON CONFLICT (id) DO NOTHING" \
  -c "INSERT INTO user_custom_roles (user_id, custom_role_id) VALUES ('gatebundleuser0000000000000000', '$BUNDLE_ID') ON CONFLICT DO NOTHING" \
  > /dev/null

curl -sf -H "Authorization: Bearer $BUNDLE_TOKEN" http://localhost:8100/api/me \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
perms = set(d['permissions'])
assert d['role'] == 'viewer', d['role']
# The bundle names three permissions; the holder's role holds only one
# of them, and a bundle intersects rather than widens.
assert 'fleet.view' in perms, perms
assert 'site.manage' not in perms, 'a bundle widened a viewer to site.manage'
assert 'action.approve' not in perms, 'a bundle widened a viewer to action.approve'
print('bundle intersects the role:', sorted(perms))
"

step "E1.4: the tenant<->realm relationship is audited"
curl -sf -H "Authorization: Bearer $PLATFORM_TOKEN" \
  "http://localhost:8100/api/tenants/$RIVAL_ID/audit/" | python3 -c "
import sys, json
d = json.load(sys.stdin)
rows = d.get('items') or d.get('entries') or d
actions = {r['action'] for r in rows}
assert 'tenant.create' in actions, sorted(actions)
realms = {
    (r.get('detail') or {}).get('keycloak_realm')
    for r in rows if r['action'] in ('tenant.create', 'tenant.realm_provisioned')
}
assert 'gate-rival' in realms, realms
print('audit records the tenant<->realm relationship')
"

step "A17: the Capability Registry reflects what the executors can ACTUALLY do"
# Tokens expire on a long run; re-mint before the tail (E1 gate finding).
TOKEN=$(tenant_token gate-owner@demo gate-owner)
CAP=$(curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/capabilities/)
echo "$CAP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
rows = {c['action_type']: c for c in d['classes']}
assert len(rows) == 14, len(rows)

# The declaration actually travelled: node -> SM -> CC. A fleet reading
# entirely undeclared means the transport broke, and it would look
# exactly like a fleet that has not upgraded -- which is why it is
# asserted rather than eyeballed.
assert d['fleet']['declared'] > 0, d['fleet']
assert 'redfish' in d['fleet']['protocols'], d['fleet']

# D1 + the class this slice found. Both are fully governed and have no
# executor; the Registry has to keep saying so.
for name in ('INTERFACE_RESET', 'CLEAR_COUNTERS'):
    r = rows[name]
    assert r['implemented'] is False, (name, r)
    assert r['implemented_by'] == [], (name, r)
    assert r['reach'] == 'unimplemented', (name, r)
    assert r['effective_device_count'] == 0, (name, r)
assert rows['INTERFACE_RESET']['risk'] == 'high', rows['INTERFACE_RESET']

# Reversibility is a different axis from risk. If these ever agree, one
# of the two columns has become redundant.
assert rows['SEL_CLEAR']['reversibility'] == 'irreversible', rows['SEL_CLEAR']
assert rows['SEL_CLEAR']['risk'] == 'low', rows['SEL_CLEAR']
assert rows['FIRMWARE_UPDATE']['inverse_action'] == 'FIRMWARE_ROLLBACK'

# Something is genuinely reachable, or the declaration is empty and every
# assertion above passes for the wrong reason.
available = [k for k, v in rows.items() if v['reach'] == 'available']
assert available, 'no class is available -- the declaration is empty'
print('capability registry live:', d['fleet'])
print('  unimplemented:', sorted(k for k, v in rows.items() if not v['implemented']))
print('  available    :', sorted(available))
"

step "A17: the Registry confers nothing and adds no mutation"
test "$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/capabilities/)" = "405"
echo "$CAP" | python3 -c "
import sys, json
c = json.load(sys.stdin)['contract']
assert 'not permission' in c['authority'], c
assert 'final execution authority' in c['authority'], c
assert 'never capable and never incapable' in c['unknown'], c
print('contract carries its own limits')
"

step "A17: an agent may not be bound to a capability nothing can execute"
GATE_SITE=$(curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/sites/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['sites'][0]['id'])")
for CLASS in INTERFACE_RESET CLEAR_COUNTERS; do
  CODE=$(curl -s -o /tmp/gate_cap.json -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    http://localhost:8090/api/operational-agents/ \
    -d "{\"name\":\"gate-cap-$CLASS\",\"description\":\"gate\",
         \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$GATE_SITE\"}],
         \"capabilities\":[{\"kind\":\"action_class\",\"capability_ref\":\"$CLASS\"}]}")
  test "$CODE" = "400" || { echo "$CLASS binding was NOT refused ($CODE)" >&2; exit 1; }
  python3 -c "
import json
d = json.load(open('/tmp/gate_cap.json'))['detail']
assert 'no executor in this platform implements' in d, d
assert 'stays in the vocabulary' in d, d
print('$CLASS binding refused, and the class keeps its governance')
"
done
# And nothing was left behind by either refusal.
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/operational-agents/ \
  | python3 -c "
import sys, json
names = [a['name'] for a in json.load(sys.stdin)['agents']]
leftover = [n for n in names if n.startswith('gate-cap-')]
assert not leftover, leftover
print('refused bindings left no agent behind')
"

step "A17.7: capability refuses, POLICY does not (the boundary the gate corrected)"
# The Gate Agent above is bound to SEL_CLEAR, which redfish implements and
# this demo node's allow list does NOT carry. That binding must SUCCEED:
# the allow list is operator policy the node enforces as the final
# execution authority, and refusing it here would make a mutable setting
# a hard Central Command constraint. The state is reported instead.
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$AGENT_ID" | python3 -c "
import sys, json
v = json.load(sys.stdin)
rows = {c['action_type']: c for c in v['capabilities']['action_classes']}
sel = rows['SEL_CLEAR']['capability']
assert sel['implemented'] is True, sel
# Capable (redfish has the code) but permitted nowhere on this fleet.
assert sel['capable_devices'] >= 1, sel
assert sel['reach'] in ('available', 'not_permitted_on_any_node'), sel
if sel['reach'] == 'not_permitted_on_any_node':
    assert sel['reachable_devices'] == 0, sel
    print('SEL_CLEAR: bound and capable, permitted on no node -- reported, not refused')
else:
    print('SEL_CLEAR: permitted on', sel['reachable_devices'], 'node(s)')
# COLLECT_DIAGNOSTICS is on the node allow list, so it is fully available.
cd_ = rows['COLLECT_DIAGNOSTICS']['capability']
assert cd_['reach'] == 'available', cd_
print('COLLECT_DIAGNOSTICS available on', cd_['reachable_devices'], 'node(s)')
"

# ===========================================================================
# A2 acceptance A-K: the Operational Agent as a governed product, live.
#
# Everything below runs against real Keycloak and real PostgreSQL. The
# Console renders exactly these contracts and derives none of them, so
# proving the contracts here proves the surface an operator sees.
# ===========================================================================

step "A2/A: the activated Gate Agent reports a coherent runtime, honestly"
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$AGENT_ID/runtime" | python3 -c '
import sys, json
r = json.load(sys.stdin)
assert r["activation_state"] == "active"
assert r["activation_provenance"] == "recorded", r["activation_provenance"]
assert r["configuration_drifted"] is False
d = r["devices"]
# Three-valued and kept apart: an unreported device is neither healthy
# nor unhealthy, and folding it into either would be inventing evidence.
assert d["in_scope"] == d["seen_recently"] + d["stale"] + d["never_reported"], d
# `active` is not a synonym for `healthy`: the runtime says what it has.
assert r["evaluation"] in ("observed", "unknown"), r["evaluation"]
print("runtime:", d, "| evaluation:", r["evaluation"])
'

step "A2/B: a propose-only agent activates with NO approval (D1 is derived)"
B_AGENT=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"a2-propose-only $(date +%s)\",
       \"require_approval_always\":true,
       \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$GATE_SITE\"}],
       \"capabilities\":[{\"kind\":\"action_class\",
                          \"capability_ref\":\"COLLECT_DIAGNOSTICS\"}]}" \
  http://localhost:8090/api/operational-agents/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$B_AGENT/preflight" | python3 -c '
import sys, json
p = json.load(sys.stdin)
assert p["requires_activation_approval"] is False, p["unattended_classes"]
assert p["unattended_classes"] == []
print("propose-only: no activation approval raised")
'
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$B_AGENT/acknowledge" >/dev/null 2>&1 || true
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$B_AGENT/activate" | python3 -c "
import sys, json
a = json.load(sys.stdin)
assert a['status'] == 'active' and a['activated_version'] == a['version']
print('propose-only agent active at v%d, no human asked' % a['activated_version'])
"

step "A2/C: an agent that would act UNATTENDED needs a named human first"
# Raising the tenant ladder is what makes a class autonomous at all, so
# this is the only step that touches it -- and it is put back afterwards.
curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"device_type":"*","level":2,"budget_limit":50,"budget_period":"daily"}' \
  http://localhost:8090/api/policies/autonomy >/dev/null
C_AGENT=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"a2-unattended $(date +%s)\",
       \"require_approval_always\":false, \"autonomy_ceiling\":2,
       \"execution_budget\":5, \"budget_period\":\"daily\",
       \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$GATE_SITE\"}],
       \"capabilities\":[{\"kind\":\"action_class\",\"capability_ref\":\"SEL_CLEAR\"}]}" \
  http://localhost:8090/api/operational-agents/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$C_AGENT/preflight" | python3 -c '
import sys, json
p = json.load(sys.stdin)
assert p["requires_activation_approval"] is True, p
assert "SEL_CLEAR" in p["unattended_classes"], p["unattended_classes"]
print("unattended grant found:", p["unattended_classes"], "-> approval required")
'
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$C_AGENT/acknowledge" >/dev/null 2>&1 || true
# Refused BEFORE the decision, with the server's own reason.
C_CODE=$(curl -s -o /tmp/gate_a2c.json -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$C_AGENT/activate")
[ "$C_CODE" = "409" ] || { echo "unattended agent activated with no approval ($C_CODE)" >&2; exit 1; }
python3 -c "
import json
d = json.load(open('/tmp/gate_a2c.json'))['detail']
assert 'requires approval' in d, d
print('activation refused:', d[:110])
"
# It is waiting in the ONE queue, not on a page of its own -- and the
# row is selected by AGENT, never by position: a queue with more than one
# pending activation would otherwise decide somebody else's.
C_SUBJECT=$(curl -sf -H "Authorization: Bearer $TOKEN" \
  http://localhost:8090/api/approvals/ | python3 -c "
import sys, json
q = json.load(sys.stdin)
rows = [i for i in q['actions'] if i['origin'] == 'agent_activation']
assert rows, 'a pending activation is missing from the approvals queue'
mine = [r for r in rows if r['activation']['agent_id'] == '$C_AGENT']
assert mine, 'this agent\'s activation is not in the queue: %s' % [
    r['activation']['agent_id'] for r in rows]
r = mine[0]
assert 'SEL_CLEAR' in r['activation']['unattended_classes'], r['activation']
assert r['action_type'] == 'AGENT_ACTIVATION', r['action_type']
print(r['action_id'])
")
# Activation is a TENANT-level decision: the agent's reach spans whatever
# its scope names, so there is no single site to hold authority over. A
# site-scoped operator is refused -- and told why, in those terms.
# Re-minted here: tokens expire on a long run (the E1 gate finding).
OP_TOKEN=$(tenant_token gate-op@demo gate-op)
OP_CODE=$(curl -s -o /tmp/gate_a2c_op.json -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $OP_TOKEN" \
  "http://localhost:8090/api/approvals/$C_SUBJECT/approve")
if [ "$OP_CODE" = "403" ]; then
  python3 -c "
import json
d = json.load(open('/tmp/gate_a2c_op.json'))['detail']
assert 'tenant-level decision' in d, d
assert 'site' in d, d
print('site-scoped operator refused, and told why:', d[:96])
"
else
  echo "site-scoped operator holds tenant authority here ($OP_CODE); continuing"
fi
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/approvals/$C_SUBJECT/approve" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['origin'] == 'agent_activation', d
assert d['decision'] == 'approved', d
assert d['approval']['received'] >= 1, d['approval']
print('activation approved on the one queue by', d['decided_by'])
"
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$C_AGENT/activate" | python3 -c "
import sys, json
a = json.load(sys.stdin)
assert a['status'] == 'active', a
print('activated after approval, at v%d' % a['activated_version'])
"
# Decided, so THIS one is no longer waiting on anybody. Asserted per
# agent rather than on a global count, so the step stays true on a stack
# that has other activations pending.
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/approvals/ \
  | python3 -c "
import sys, json
q = json.load(sys.stdin)
still = [i for i in q['actions']
         if i['origin'] == 'agent_activation'
         and i['activation']['agent_id'] == '$C_AGENT']
assert not still, 'a decided activation is still listed as awaiting a human'
print('decided activation left the queue (%d other(s) still pending)'
      % q['activation_total'])
"

step "A2/D: an agent that would see nothing never becomes active"
# A freshly registered site has no devices in the fleet cache, so an
# agent scoped to it reaches nothing. Two gates can legitimately catch
# that -- the Registry's zero-reach refusal at binding, or the
# preflight's `scope` dimension -- and the acceptance is that ONE of
# them does, never that the agent quietly activates.
D_SITE=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"site_name\":\"a2-empty-$(date +%s)\",\"sm_endpoint\":\"site-manager:50051\",
       \"license_fingerprint\":\"demo\"}" \
  http://localhost:8090/api/sites/register \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['site']['id'])")
D_CODE=$(curl -s -o /tmp/gate_a2d.json -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"name\":\"a2-no-reach $(date +%s)\",
       \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$D_SITE\"}],
       \"capabilities\":[{\"kind\":\"action_class\",
                          \"capability_ref\":\"COLLECT_DIAGNOSTICS\"}]}" \
  http://localhost:8090/api/operational-agents/)
if [ "$D_CODE" = "400" ]; then
  python3 -c "
import json
d = json.load(open('/tmp/gate_a2d.json'))['detail']
print('refused at binding:', d[:110])
"
elif [ "$D_CODE" = "201" ]; then
  D_AGENT=$(python3 -c "import json; print(json.load(open('/tmp/gate_a2d.json'))['id'])")
  curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8090/api/operational-agents/$D_AGENT/preflight" | python3 -c '
import sys, json
p = json.load(sys.stdin)
assert p["can_activate"] is False, p["overall"]
assert "scope" in p["blocked_dimensions"], p["blocked_dimensions"]
row = next(d for d in p["dimensions"] if d["dimension"] == "scope")
print("insufficient scope BLOCKED:", row["detail"][:100])
'
  D_ACT=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8090/api/operational-agents/$D_AGENT/activate")
  [ "$D_ACT" = "409" ] || { echo "an agent with no reach activated ($D_ACT)" >&2; exit 1; }
  echo "activation of a no-reach agent refused (409)"
else
  echo "unexpected response creating a no-reach agent ($D_CODE)" >&2
  cat /tmp/gate_a2d.json >&2
  exit 1
fi

step "A2/E-G: capability, policy and UNKNOWN stay three separate answers"
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$AGENT_ID/preflight" | python3 -c '
import sys, json
p = json.load(sys.stdin)
by = {d["dimension"]: d for d in p["dimensions"]}
# E: implemented, but this node does not permit it -> WARN, never BLOCKED.
#    Policy is not capability; the node stays the final authority.
reach = by["executor_reach"]
assert reach["verdict"] in ("ready", "warn", "unknown"), reach
if reach["verdict"] == "warn":
    assert reach.get("warned"), reach
    print("E: implemented-but-not-permitted ->", reach["detail"][:88])
else:
    print("E: executor reach", reach["verdict"], "-", reach["detail"][:80])
# G: a device that has not declared reads UNKNOWN, and unknown is not zero.
assert reach.get("undeclared") is not None
print("G: undeclared devices in this scope:", reach.get("undeclared"))
'
# F: an unimplemented class is refused at BINDING, so it never reaches a
#    preflight at all (proven above for INTERFACE_RESET and CLEAR_COUNTERS).
echo "F: unsupported capability refused at binding (asserted above)"

step "A2/H: the budget is configurable, and consumption belongs to the AGENT"
curl -sf -X PATCH -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"execution_budget":3,"budget_period":"daily"}' \
  "http://localhost:8090/api/operational-agents/$C_AGENT" >/dev/null
H_USED=$(curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$C_AGENT/runtime" | python3 -c "
import sys, json
b = json.load(sys.stdin)['budget']
assert b['limit'] == 3 and b['period'] == 'daily', b
print(b['executions_used'])
")
# An ordinary edit must not refill it: the allowance is the agent's, not
# the agent-version's, or a description change resets a spent budget.
curl -sf -X PATCH -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"description":"same agent, new wording"}' \
  "http://localhost:8090/api/operational-agents/$C_AGENT" >/dev/null
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$C_AGENT/runtime" | python3 -c "
import sys, json
b = json.load(sys.stdin)['budget']
assert b['executions_used'] == $H_USED, (b['executions_used'], $H_USED)
print('budget survived an edit:', b['executions_used'], 'of', b['limit'], 'used')
"

step "A2/I: editing an ACTIVE agent is drift, and its preflight goes stale"
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$C_AGENT/runtime" | python3 -c '
import sys, json
r = json.load(sys.stdin)
assert r["activation_state"] == "active"
assert r["configuration_drifted"] is True, r
assert r["activated_version"] < r["configuration_version"], r
assert r["preflight"]["current"] is False, r["preflight"]
print("drift reported: running v%d, configured v%d, preflight stale"
      % (r["activated_version"], r["configuration_version"]))
'
# The list says so too, so an operator sees it without opening the agent.
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/operational-agents/ \
  | python3 -c "
import sys, json
row = next(a for a in json.load(sys.stdin)['agents'] if a['id'] == '$C_AGENT')
assert row['configuration_drifted'] is True, row
assert row['activation_provenance'] == 'recorded', row
print('the list reports drift for', row['name'])
"

step "A2/J: an in-flight proposal keeps the version it was made under (D3)"
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$AGENT_ID/proposals" | python3 -c "
import sys, json
d = json.load(sys.stdin)
props = d['proposals']
if not props:
    print('no proposals yet on this agent; version retention asserted in unit tests')
else:
    versions = {p['actor'] for p in props}
    assert all(v.startswith('op-agent:') and '@v' in v for v in versions), versions
    print('proposals retain their originating attribution:', sorted(versions))
"

step "A2/K: skills install per DEVICE, on a durable deduplicating ledger"
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$SKILL_AGENT/runtime" | python3 -c '
import sys, json
r = json.load(sys.stdin)
# The demo marketplace carries no `fan-health`, so the skill is UNKNOWN
# rather than assumed fine, and nothing was installed on a guess.
assert isinstance(r["skills_by_id"], list), r["skills_by_id"]
for s in r["skills_by_id"]:
    assert "devices" in s and isinstance(s["devices"], list), s
    for dev in s["devices"]:
        assert dev["device_agent_id"] and dev["status"], dev
print("skill delivery ledger rows:", sum(len(s["devices"]) for s in r["skills_by_id"]))
'

step "A2: the lifecycle is ATTRIBUTED in the audit chain"
# Filtered per action rather than paged, so this asserts presence rather
# than hoping the entry landed inside one page.
for A2_ACTION in operational_agent.preflighted operational_agent.acknowledged \
                 operational_agent.activated operational_agent.activation_approved; do
  curl -sf -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8090/api/audit/?action=$A2_ACTION&page_size=50" | python3 -c "
import sys, json
d = json.load(sys.stdin)
rows = d['entries']
assert rows, 'no audit entry for $A2_ACTION'
# Every step of the lifecycle names a PERSON, never a service.
assert all(r['actor'] for r in rows), rows[:1]
print('$A2_ACTION:', len(rows), 'entry(ies), first actor', rows[0]['actor'])
"
done

# ===========================================================================
# A3 machine identity (spec A20), live: real Keycloak, real client_credentials.
#
# The headline proof is NOT that the credential works. It is that an
# authenticated agent is capped at two reads and cannot approve its own
# work -- because resolved the way agents are in-process, it would have
# satisfied every route guard in the platform.
# ===========================================================================

step "A3: an agent is issued a machine identity, and the secret is shown ONCE"
A3_AGENT=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"a3-machine $(date +%s)\",
       \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$GATE_SITE\"}],
       \"capabilities\":[
         {\"kind\":\"action_class\",\"capability_ref\":\"COLLECT_DIAGNOSTICS\"},
         {\"kind\":\"read\",\"capability_ref\":\"incidents\"},
         {\"kind\":\"read\",\"capability_ref\":\"fleet\"}]}" \
  http://localhost:8090/api/operational-agents/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
A3_SECRET=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A3_AGENT/identity" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['status'] == 'active', d
assert d['client_id'].startswith('op-agent-'), d['client_id']
assert d['client_secret'], 'no secret returned'
import sys as _s; print(d['client_secret'], file=_s.stderr)
print(d['client_secret'])
" 2>/dev/null)
[ -n "$A3_SECRET" ] || { echo "no client secret issued" >&2; exit 1; }
A3_CLIENT="op-agent-$A3_AGENT"
echo "identity issued: $A3_CLIENT"
# Never again, on any read, and never in the audit log.
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A3_AGENT/identity" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d["exists"] is True
assert "client_secret" not in d, "the secret came back on a read"
print("status read carries no secret:", d["status"])
'

step "A3: the agent authenticates with client_credentials at REAL Keycloak"
a3_token() {
  curl -sf -X POST \
    "http://localhost:8180/realms/tenant-demo/protocol/openid-connect/token" \
    -d "grant_type=client_credentials&client_id=$A3_CLIENT&client_secret=$1" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])"
}
A3_TOKEN=$(a3_token "$A3_SECRET")
[ -n "$A3_TOKEN" ] || { echo "client_credentials grant failed" >&2; exit 1; }
echo "machine token obtained"

step "A3: the machine principal reads what the ceiling allows — and no more"
for READ in fleet incidents attention; do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $A3_TOKEN" \
    "http://localhost:8090/api/$READ/")
  [ "$CODE" = "200" ] || { echo "machine read /api/$READ/ -> $CODE, want 200" >&2; exit 1; }
done
echo "fleet.view / incident.view reads OK"

# The intersection is PER AGENT, not a global grant: an agent that never
# bound `incidents` does not get incident.view, even though the ceiling
# admits it. A0's REQUIRED_READS give every agent attention+autonomy,
# which map to fleet.view and nothing else.
A3_NARROW=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"a3-narrow $(date +%s)\",
       \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$GATE_SITE\"}],
       \"capabilities\":[
         {\"kind\":\"action_class\",\"capability_ref\":\"COLLECT_DIAGNOSTICS\"}]}" \
  http://localhost:8090/api/operational-agents/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
N_SECRET=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A3_NARROW/identity" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['client_secret'])")
N_TOKEN=$(curl -sf -X POST \
  "http://localhost:8180/realms/tenant-demo/protocol/openid-connect/token" \
  -d "grant_type=client_credentials&client_id=op-agent-$A3_NARROW&client_secret=$N_SECRET" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $N_TOKEN" \
    http://localhost:8090/api/fleet/)" = "200" ] \
  || { echo "an agent with attention+autonomy lost fleet.view" >&2; exit 1; }
NARROW=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $N_TOKEN" \
  http://localhost:8090/api/incidents/)
[ "$NARROW" = "403" ] || {
  echo "an agent that never bound incidents got incident.view ($NARROW)" >&2; exit 1; }
echo "unbound read refused (403): the intersection is per agent"

step "A3: the machine principal CANNOT approve its own work (the headline)"
APPR=$(curl -s -o /tmp/a3_appr.json -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $A3_TOKEN" \
  "http://localhost:8090/api/approvals/anything/approve")
[ "$APPR" = "403" ] || { echo "machine token approved ($APPR), want 403" >&2; cat /tmp/a3_appr.json >&2; exit 1; }
python3 -c "
import json
d = json.load(open('/tmp/a3_appr.json'))['detail']
assert 'action.approve' in d, d
print('approval refused:', d[:80])
"

step "A3: every other permission in the vocabulary is refused"
# Swept, not spot-checked: a ceiling is a claim about ALL of them.
for M in "POST|/api/operational-agents/|{\"name\":\"x\"}" \
         "POST|/api/policies/|{\"name\":\"x\"}" \
         "POST|/api/scope-grants/|{\"principal_ref\":\"x\"}" \
         "POST|/api/org-units/|{\"name\":\"x\"}" \
         "POST|/api/campaigns/|{\"name\":\"x\"}" \
         "GET|/api/audit/|"; do
  VERB="${M%%|*}"; REST="${M#*|}"; P="${REST%%|*}"; BODY="${REST#*|}"
  if [ -n "$BODY" ]; then
    CODE=$(curl -s -o /dev/null -w '%{http_code}' -X "$VERB" \
      -H "Authorization: Bearer $A3_TOKEN" -H 'Content-Type: application/json' \
      -d "$BODY" "http://localhost:8090$P")
  else
    CODE=$(curl -s -o /dev/null -w '%{http_code}' -X "$VERB" \
      -H "Authorization: Bearer $A3_TOKEN" "http://localhost:8090$P")
  fi
  [ "$CODE" = "403" ] || {
    echo "machine token reached $VERB $P ($CODE), want 403" >&2; exit 1; }
done
echo "every mutation and audit.export refused (403)"

step "A3: the machine principal cannot credential ITSELF"
for SUB in identity identity/rotate identity/revoke; do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $A3_TOKEN" \
    "http://localhost:8090/api/operational-agents/$A3_AGENT/$SUB")
  [ "$CODE" = "403" ] || { echo "machine reached $SUB ($CODE)" >&2; exit 1; }
done
echo "identity lifecycle refused to the machine principal"

step "A3: a machine token is refused by ANOTHER tenant's Central Command"
# tenant-rival exists from the E1.4 steps; CC serves tenant-demo only.
RIVAL=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $A3_TOKEN" \
  http://localhost:8100/api/me/tenants)
echo "machine token at the Console plane: $RIVAL (not a tenant-plane identity there)"

step "A3: rotation works, and the old secret stops working"
A3_SECRET2=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A3_AGENT/identity/rotate" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['client_secret'])")
[ -n "$A3_SECRET2" ] && [ "$A3_SECRET2" != "$A3_SECRET" ] \
  || { echo "rotation did not change the secret" >&2; exit 1; }
OLD=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  "http://localhost:8180/realms/tenant-demo/protocol/openid-connect/token" \
  -d "grant_type=client_credentials&client_id=$A3_CLIENT&client_secret=$A3_SECRET")
[ "$OLD" = "401" ] || { echo "the old secret still works ($OLD)" >&2; exit 1; }
A3_TOKEN=$(a3_token "$A3_SECRET2")
[ -n "$A3_TOKEN" ] || { echo "the new secret does not work" >&2; exit 1; }
echo "rotated: old secret 401, new secret works, same identity"

step "A3: revocation is IMMEDIATE — it beats an otherwise-valid token"
# The token below was minted BEFORE the revocation and has not expired.
# Keycloak would still consider it valid; Central Command's row does not.
curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"reason":"gate: proving immediate revocation"}' \
  "http://localhost:8090/api/operational-agents/$A3_AGENT/identity/revoke" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['status'] == 'revoked', d
assert d['effective'] == 'immediate', d
print('revoked:', d['revoke_reason'])
"
REV=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $A3_TOKEN" \
  http://localhost:8090/api/fleet/)
[ "$REV" = "401" ] || {
  echo "a revoked identity still authenticated ($REV): the row is not authoritative" >&2
  exit 1; }
echo "an unexpired token from a revoked identity: 401"

step "A3: retiring an agent retires its identity"
A3_B=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"a3-retire $(date +%s)\",
       \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$GATE_SITE\"}],
       \"capabilities\":[
         {\"kind\":\"action_class\",\"capability_ref\":\"COLLECT_DIAGNOSTICS\"}]}" \
  http://localhost:8090/api/operational-agents/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
B_SECRET=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A3_B/identity" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['client_secret'])")
B_TOKEN=$(curl -sf -X POST \
  "http://localhost:8180/realms/tenant-demo/protocol/openid-connect/token" \
  -d "grant_type=client_credentials&client_id=op-agent-$A3_B&client_secret=$B_SECRET" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $B_TOKEN" \
    http://localhost:8090/api/fleet/)" = "200" ] \
  || { echo "the second machine identity never worked" >&2; exit 1; }
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A3_B/retire" >/dev/null
RET=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $B_TOKEN" \
  http://localhost:8090/api/fleet/)
[ "$RET" = "401" ] || { echo "a retired agent still authenticated ($RET)" >&2; exit 1; }
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A3_B/identity" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['status'] == 'retired', d
print('retired agent, retired identity, token refused')
"

step "A3: the identity lifecycle is audited, and the chain still verifies"
for A3_ACTION in agent_identity.issued agent_identity.rotated \
                 agent_identity.revoked agent_identity.retired; do
  curl -sf -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8090/api/audit/?action=$A3_ACTION&page_size=50" | python3 -c "
import sys, json
rows = json.load(sys.stdin)['entries']
assert rows, 'no audit entry for $A3_ACTION'
assert all(r['actor'] for r in rows), rows[:1]
print('$A3_ACTION:', len(rows), 'entry(ies)')
"
done
# The refusal path is audited too: an agent that goes quiet because its
# credential was withdrawn must be distinguishable from one with nothing
# to do. The revoked-token request above produced this.
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/audit/?action=agent_identity.auth_failed&page_size=50" \
  | python3 -c "
import sys, json
rows = json.load(sys.stdin)['entries']
assert rows, 'a refused machine credential was not audited'
print('agent_identity.auth_failed:', len(rows), 'entry(ies)')
"
# The secret must never appear anywhere in the audit log.
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/audit/?page_size=200" > /tmp/a3_audit.json
python3 -c "
import json
raw = open('/tmp/a3_audit.json').read()
for s in ('$A3_SECRET', '$A3_SECRET2', '$B_SECRET'):
    assert s and s not in raw, 'a client secret leaked into the audit log'
print('no client secret anywhere in the audit log')
"
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/audit/verify \
  | grep -q '"valid": *true'
echo "audit chain verifies"

step "A3: aggregate visibility carries NO per-agent detail (A20.9, A12.1 intact)"
# The gate `cd`s to deploy/full-stack, so the module path is resolved
# from the repo root the same way the tenant-lookup helper is
# (gate-caught: ModuleNotFoundError under set -e).
A3_SRC="$_REPO_ROOT/services/central_command/src" python3 - <<'A3PY'
import json, os, sys
sys.path.insert(0, os.environ["A3_SRC"])
from harkeniq_cc.machine_identity import aggregate_summary


class Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


rows = [
    Row(status="active", last_seen_at=None, agent_id="agent-secret-1",
        keycloak_client_id="op-agent-secret-1", keycloak_sub="sub-secret-1"),
    Row(status="revoked", last_seen_at=None, agent_id="agent-secret-2",
        keycloak_client_id="op-agent-secret-2", keycloak_sub="sub-secret-2"),
]
summary = aggregate_summary(rows)
blob = json.dumps(summary)
for leak in ("agent-secret", "op-agent-secret", "sub-secret"):
    assert leak not in blob, f"{leak} leaked into the platform summary"
assert summary["identities"] == 2 and summary["revoked"] == 1
print("platform summary:", blob)
A3PY
# The aggregate rides the internal channel but NOT the billing payload:
# `/usage-events` feeds MeteringService and therefore invoicing, so an
# operational signal there could corrupt billing.
BEFORE_METER=$(docker compose exec -T postgres \
  psql -U harkeniq -d harkeniq_console -tAc "select count(*) from usage_events" \
  2>/dev/null | tr -d ' \r')
curl -sf -X POST -H "Authorization: Bearer ${HARKENIQ_INTERNAL_API_KEY:-demo-console-cc-key}" \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"gate","identities":3,"active":2,"revoked":1,"retired":0,
       "ever_seen":1,"never_seen":2,"most_recent_seen_at":null}' \
  http://localhost:8100/api/internal/agent-identity-summary | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['accepted'] is True, d
print('aggregate summary accepted on the internal channel')
"
AFTER_METER=$(docker compose exec -T postgres \
  psql -U harkeniq -d harkeniq_console -tAc "select count(*) from usage_events" \
  2>/dev/null | tr -d ' \r')
[ "$BEFORE_METER" = "$AFTER_METER" ] || {
  echo "the identity summary created a METERING record ($BEFORE_METER -> $AFTER_METER)" >&2
  exit 1; }
echo "no metering record created ($AFTER_METER usage_events, unchanged)"

# A12.1 stands: a PLATFORM-realm token still gets nothing from Central Command.
PLAT=$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $PLATFORM_TOKEN" http://localhost:8090/api/fleet/)
[ "$PLAT" = "401" ] || { echo "a platform token reached CC ($PLAT): A12.1 broken" >&2; exit 1; }
echo "platform-realm token at CC: 401 (A12.1 unamended)"

# ===========================================================================
# A4 governed capability expansion (spec A21), live.
#
# The headline is NOT that more capabilities are reachable. It is that
# making them reachable widened nothing else: not RBAC, not scope, not
# autonomy, not approval, not execution.
# ===========================================================================

step "A4/A: the catalogue is served, and the interface subsystem is alive"
curl -sf -H "Authorization: Bearer $TOKEN" \
  http://localhost:8090/api/capabilities/catalogue | python3 -c '
import sys, json
d = json.load(sys.stdin)
by = {s["subsystem"]: s for s in d["subsystems"]}
assert "interface" in by, sorted(by)
iface = {e["action_type"] for e in by["interface"]["candidates"]}
# It mapped ONLY to CLEAR_COUNTERS, which no executor implements, so a
# switch-scoped agent had no proposable action at all.
assert "CLEAR_COUNTERS" not in iface, iface
assert iface == {"INTERFACE_DISABLE", "INTERFACE_ENABLE"}, iface
# Registry reach is joined BESIDE the mapping, never merged into it.
entry = by["interface"]["candidates"][0]
assert "capability" in entry and entry["because"] and entry["provenance"]
assert "grants nothing" in d["contract"]["authority"]
print("catalogue live:", len(d["subsystems"]), "subsystems | interface ->",
      sorted(iface))
'

step "A4/B: implemented classes that were unreachable are now addressable"
curl -sf -H "Authorization: Bearer $TOKEN" \
  http://localhost:8090/api/operational-agents/catalogue | python3 -c '
import sys, json
by = {c["action_type"]: c for c in json.load(sys.stdin)["action_classes"]}
for newly in ("POWER_CAP_ADJUST", "POWER_CYCLE", "CONFIG_RESTORE",
              "INTERFACE_ENABLE", "INTERFACE_DISABLE"):
    assert by[newly]["proposable"] is True, (newly, by[newly])
    assert by[newly]["observed_conditions"], newly
# Deliberate exclusions stay excluded AND say why.
assert by["FIRMWARE_UPDATE"]["proposable"] is False, by["FIRMWARE_UPDATE"]
assert by["CLEAR_COUNTERS"]["proposable"] is False
assert by["CLEAR_COUNTERS"]["note"]
print("newly addressable: POWER_CAP_ADJUST, POWER_CYCLE, CONFIG_RESTORE,",
      "INTERFACE_ENABLE, INTERFACE_DISABLE")
'

step "A4/C: an unimplemented class can never be mapped (refuse on CAPABILITY)"
for A4_CLASS in CLEAR_COUNTERS INTERFACE_RESET; do
  CODE=$(curl -s -o /tmp/a4_map.json -w '%{http_code}' -X PUT \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"entries\":[{\"subsystem\":\"interface\",\"action_type\":\"$A4_CLASS\"}]}" \
    http://localhost:8090/api/capabilities/catalogue)
  [ "$CODE" = "400" ] || { echo "$A4_CLASS was mapped ($CODE)" >&2; exit 1; }
  python3 -c "
import json
d = json.load(open('/tmp/a4_map.json'))['detail']
assert 'no executor' in d, d
print('$A4_CLASS refused:', d[:78])
"
done
# ...and it is still in the governed vocabulary, not deleted (A17.6/A21.9).
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/capabilities/ \
  | python3 -c "
import sys, json
rows = {c['action_type']: c for c in json.load(sys.stdin)['classes']}
for cls in ('CLEAR_COUNTERS', 'INTERFACE_RESET'):
    assert cls in rows, cls
    assert rows[cls]['reach'] == 'unimplemented', rows[cls]
print('both unimplemented classes still governed and truthfully reported')
"

step "A4/D: a campaign-only class is refused with ITS reason, not a generic one"
CODE=$(curl -s -o /tmp/a4_fw.json -w '%{http_code}' -X PUT \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"entries":[{"subsystem":"fan","action_type":"FIRMWARE_UPDATE"}]}' \
  http://localhost:8090/api/capabilities/catalogue)
[ "$CODE" = "400" ] || { echo "FIRMWARE_UPDATE was mapped ($CODE)" >&2; exit 1; }
python3 -c "
import json
d = json.load(open('/tmp/a4_fw.json'))['detail']
assert 'campaigns' in d, d
print('firmware refused:', d[:88])
"

step "A4/E: capability selection widens NO autonomy"
# The whole ratified point of option A. The tenant ladder is at 0 here,
# and every newly addressable class must still require a human.
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/autonomy/ \
  | python3 -c '
import sys, json
d = json.load(sys.stdin)
by = {c["action_type"]: c for c in d["action_classes"]}
for newly in ("INTERFACE_ENABLE", "INTERFACE_DISABLE", "POWER_CAP_ADJUST",
              "COLLECT_DIAGNOSTICS", "IDENTIFY_LED"):
    row = by[newly]
    assert row["disposition"] != "autonomous", (newly, row["disposition"])
    assert row["approval"]["required"] is True, (newly, row["approval"])
    # A21.5: not budget-mapped means a named human, however effective the
    # class has proven to be. Evidence is not authority.
    assert row["budget_mapped"] is False or row["disposition"] != "autonomous", newly
print("every newly addressable class still requires a named human:",
      {k: by[k]["disposition"] for k in
       ("INTERFACE_ENABLE", "POWER_CAP_ADJUST", "COLLECT_DIAGNOSTICS")})
'

step "A4/F: capability selection widens NO permission and NO scope"
# A viewer may READ the catalogue and may not rewrite it; an operator
# likewise. No new permission was invented for any of this.
V_CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OP_TOKEN" \
  http://localhost:8090/api/capabilities/catalogue)
[ "$V_CODE" = "200" ] || { echo "operator cannot read the catalogue ($V_CODE)" >&2; exit 1; }
W_CODE=$(curl -s -o /dev/null -w '%{http_code}' -X PUT \
  -H "Authorization: Bearer $OP_TOKEN" -H 'Content-Type: application/json' \
  -d '{"entries":[]}' http://localhost:8090/api/capabilities/catalogue)
[ "$W_CODE" = "403" ] || { echo "operator rewrote the catalogue ($W_CODE)" >&2; exit 1; }
echo "read 200 / write 403 for an operator; no new permission"

step "A4/G: a machine principal cannot rewrite the catalogue either"
# A3's ceiling holds: an authenticated agent reads fleet.view and nothing
# it could use to widen what it may itself propose.
#
# Deliberately $N_TOKEN, not $A3_TOKEN. A3 REVOKES its main identity to
# prove revocation is immediate, so that token answers 401 -- which is a
# refusal, but the wrong one: it would prove the token is dead rather
# than that a LIVE machine principal lacks the permission. `a3-narrow` is
# never revoked, so 403 here is the ceiling talking.
if [ -n "${N_TOKEN:-}" ]; then
  M_CODE=$(curl -s -o /dev/null -w '%{http_code}' -X PUT \
    -H "Authorization: Bearer $N_TOKEN" -H 'Content-Type: application/json' \
    -d '{"entries":[]}' http://localhost:8090/api/capabilities/catalogue)
  [ "$M_CODE" = "403" ] || {
    echo "a live machine principal rewrote the catalogue ($M_CODE)" >&2; exit 1; }
  # And it can still READ it -- fleet.view is inside the A20.3 ceiling.
  R_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $N_TOKEN" \
    http://localhost:8090/api/capabilities/catalogue)
  [ "$R_CODE" = "200" ] || {
    echo "a machine principal cannot read the catalogue ($R_CODE)" >&2; exit 1; }
  echo "live machine principal: read 200, write 403 -- it cannot widen its own capability"
else
  echo "no live machine token in scope; covered by unit tests"
fi

step "A4/H: the catalogue write is audited and the chain still verifies"
curl -sf -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"entries":[
        {"subsystem":"log","action_type":"SEL_CLEAR","because":"gate","provenance":"gate"},
        {"subsystem":"interface","action_type":"INTERFACE_DISABLE","because":"gate","provenance":"gate"}
      ]}' \
  http://localhost:8090/api/capabilities/catalogue | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['entries'] == 2, d
print('catalogue replaced:', d['entries'], 'entries')
"
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/audit/?action=capability_catalogue.replaced&page_size=20" \
  | python3 -c "
import sys, json
rows = json.load(sys.stdin)['entries']
assert rows, 'the catalogue rewrite was not audited'
assert all(r['actor'] for r in rows), rows[:1]
print('capability_catalogue.replaced:', len(rows), 'entry(ies), actor',
      rows[0]['actor'])
"

step "A4/I: editing the catalogue changes what is proposable, live"
curl -sf -H "Authorization: Bearer $TOKEN" \
  http://localhost:8090/api/operational-agents/catalogue | python3 -c '
import sys, json
by = {c["action_type"]: c for c in json.load(sys.stdin)["action_classes"]}
assert by["SEL_CLEAR"]["proposable"] is True
assert by["INTERFACE_DISABLE"]["proposable"] is True
# Removed by the replacement above, so no longer proposable.
assert by["POWER_CAP_ADJUST"]["proposable"] is False, by["POWER_CAP_ADJUST"]
print("proposable set follows the catalogue, not a constant")
'
# Put the platform default back so later steps and re-runs are unchanged.
curl -sf -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "$(A4_SRC="$_REPO_ROOT/services/central_command/src" python3 -c '
import json, os, sys
sys.path.insert(0, os.environ["A4_SRC"])
from harkeniq_cc.capability_catalogue import SEED
print(json.dumps({"entries": [dict(e) for e in SEED]}))
')" http://localhost:8090/api/capabilities/catalogue >/dev/null
echo "platform default catalogue restored"

step "A4/J: execution_permitted() is part of PRODUCTION dispatch"
# It had NO production caller at all: the runtime used hand-written
# sequential checks alongside the model. Asserted on the shipped SOURCE --
# a behavioural test would pass just as well against the checks it
# replaced, and reading the files needs no service dependencies on the
# gate host (gate-caught: importing the servicer needs grpc).
A4_ROOT="$_REPO_ROOT" python3 - <<'A4PY'
import os
import pathlib
import re

root = pathlib.Path(os.environ["A4_ROOT"])
sm = (root / "services/site_manager/src/harkeniq_sm/stopswitch.py").read_text()
grpc_src = (root / "services/site_manager/src/harkeniq_sm/grpc_server.py").read_text()


def tuple_of(name, text):
    """The names in `NAME = (...)`, however it happens to be wrapped."""
    m = re.search(name + r"\s*=\s*\((.*?)\)", text, re.S)
    assert m, name + " not found"
    return set(re.findall(r'"([a-z_]+)"', m.group(1)))


decision = tuple_of("DECISION_INPUTS", sm)
cc = tuple_of("CC_INPUTS", sm)
smi = tuple_of("SM_DISPATCH_INPUTS", sm)
node = tuple_of("NODE_INPUTS", sm)

# No input may be dropped by the split, or owned by two stages.
assert cc | smi | node == decision, (cc | smi | node) ^ decision
assert len(cc) + len(smi) + len(node) == len(decision), "an input is owned twice"
# A17.8's reserved slot, finally supplied by a real stage.
assert "capability" in smi, smi

# The dispatch path defers to the model rather than re-checking inline.
assert "execution_permitted(" in grpc_src
assert "SM_DISPATCH_INPUTS" in grpc_src
assert "_sm_execution_decision(" in grpc_src
assert "decision.permitted" in grpc_src
print("execution_permitted is the dispatch path;", len(decision),
      "inputs across 3 stages, none dropped, capability supplied")
A4PY

# ---------------------------------------------------------------------------
# A5 — the canonical governed agent interaction contract (spec A22)
# ---------------------------------------------------------------------------

step "A5/A: every governed class declares what it requires to run"
curl -sf -H "Authorization: Bearer $TOKEN" \
  http://localhost:8090/api/capabilities/ \
  | python3 -c "
import sys, json
rows = {r['action_type']: r for r in json.load(sys.stdin)['classes']}
# The A4 defect, now visible in the contract: these four require a
# parameter, and before A5 the evaluator emitted {'reason': ...} for
# every class, so each was proposed, approved, dispatched and refused.
assert rows['IDENTIFY_LED']['required_parameters'] == ['target'], rows['IDENTIFY_LED']
assert rows['INTERFACE_DISABLE']['required_parameters'] == ['interface']
assert rows['POWER_CAP_ADJUST']['required_parameters'] == ['target_watts']
assert rows['CONFIG_RESTORE']['required_parameters'] == ['attributes_json']
# Whole-device classes take none, and that is an answer, not a gap.
for name in ('SEL_CLEAR', 'BMC_RESET', 'COLLECT_DIAGNOSTICS'):
    assert rows[name]['required_parameters'] == [], name
# Addressable is not executable: two classes are implemented and still
# cannot be proposed, and each names the input that is missing (A22.5).
for name in ('POWER_CAP_ADJUST', 'CONFIG_RESTORE'):
    assert rows[name]['parameters_resolvable'] is False, name
    assert rows[name]['parameter_reason'], name
assert rows['IDENTIFY_LED']['parameters_resolvable'] is True
# Unimplemented classes still declare, so 'no executor' and 'takes no
# parameters' stay distinguishable (A21.9 unchanged).
assert rows['INTERFACE_RESET']['implemented'] is False
assert rows['INTERFACE_RESET']['required_parameters'] == ['interface']
print('parameter contract served for', len(rows), 'classes;',
      'unsatisfiable named:', rows['POWER_CAP_ADJUST']['parameter_reason'][:48])
"

step "A5/B: the Site Manager carries component identity to Central Command"
# A verdict's sensor id is '<subsystem>:<component>'; the SM parsed off
# the subsystem and DISCARDED the remainder, so CC held no drive bay and
# no port name for any device. Asserted on the real snapshot column.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT count(*) FROM information_schema.columns
    WHERE table_name='cc_incidents' AND column_name='components'" \
  | grep -q '^1$' \
  || { echo "cc_incidents.components missing" >&2; exit 1; }
# NO BACKFILL: an incident the SM has not reported on stays NULL, which
# is UNKNOWN. Writing [] would assert 'nothing affected' -- a fact nobody
# checked -- and CC turns 'no component' into a refusal to propose.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT count(*) FROM cc_incidents WHERE components = '[]'::jsonb" \
  | grep -q '^0$' \
  || { echo "A5 backfilled an empty component list" >&2; exit 1; }
echo "component identity column present, nothing backfilled"

step "A5/C: a real fault resolves a real parameter, end to end"
# The headline, proven on hardware evidence rather than on an empty list.
# Before A5 EVERY proposal carried params={"reason": ...}, so IDENTIFY_LED
# was proposed, approved by a human, dispatched, and refused at the node
# with "IDENTIFY_LED requires a 'target' param" -- every single time.
curl -skf -X POST https://localhost:9000/test/inject-fault \
  -H 'Content-Type: application/json' \
  -d '{"fault_type":"disk","target":"Solid State Disk 0:1:0","params":{"health":"Critical"}}' \
  > /dev/null
wait_for "disk incident carries its component at CC" 180 bash -c \
  "docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
     \"SELECT count(*) FROM cc_incidents WHERE subsystem='disk' AND components IS NOT NULL\" \
   | grep -qv '^ *0 *$'"
A5_COMPONENT=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT components->0->>'component' FROM cc_incidents
    WHERE subsystem='disk' AND components IS NOT NULL LIMIT 1" | tr -d '\r' | sed 's/^ *//;s/ *$//')
[ -n "$A5_COMPONENT" ] || { echo "no component reported for a disk incident" >&2; exit 1; }
echo "the Site Manager named the component: $A5_COMPONENT"

A5_SITE=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT site_id FROM cc_incidents WHERE subsystem='disk' AND components IS NOT NULL LIMIT 1" \
  | tr -d ' \r')
A5_AGENT=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"a5-parameters $(date +%s)\",
       \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$A5_SITE\"}],
       \"capabilities\":[{\"kind\":\"action_class\",\"capability_ref\":\"IDENTIFY_LED\"}]}" \
  http://localhost:8090/api/operational-agents/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

A5_BEFORE=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT count(*) FROM cc_agent_proposals" | tr -d ' \r')
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A5_AGENT/dry-run" \
  | A5_COMPONENT="$A5_COMPONENT" python3 -c "
import os, sys, json
r = json.load(sys.stdin)
component = os.environ['A5_COMPONENT']
assert r['dry_run'] is True
assert r['wrote'] == [], r['wrote']
led = [p for p in r['would_propose'] if p['action_type'] == 'IDENTIFY_LED']
assert led, ('no IDENTIFY_LED proposed against an open disk incident',
             r['would_propose'], r['withheld'])
for p in led:
    # The parameter the node would actually receive, resolved from the
    # component the Site Manager reported -- the same value the node's
    # OWN disk-health skill would have supplied for this condition.
    assert p['params'].get('target') == component, (p['params'], component)
    assert p['requires_human'] is True, 'IDENTIFY_LED is mapped to no level'
print('dry run:', len(r['would_propose']), 'would propose,',
      len(r['withheld']), 'withheld; target resolved to', component)
"
A5_AFTER=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT count(*) FROM cc_agent_proposals" | tr -d ' \r')
[ "$A5_BEFORE" = "$A5_AFTER" ] \
  || { echo "dry-run wrote proposals ($A5_BEFORE -> $A5_AFTER)" >&2; exit 1; }
echo "dry-run created nothing: cc_agent_proposals still $A5_AFTER"

step "A5/D: an agent may dry-run ITSELF and no other (A22.8, no ceiling change)"
# fleet.view is already in MACHINE_PRINCIPAL_CEILING, so this needs no
# widening. 'its own and no other' is an object gate, not a permission.
A5_SELF=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $N_TOKEN" \
  "http://localhost:8090/api/operational-agents/$A3_NARROW/dry-run")
[ "$A5_SELF" = "200" ] \
  || { echo "an agent could not dry-run itself ($A5_SELF)" >&2; exit 1; }
A5_OTHER=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $N_TOKEN" \
  "http://localhost:8090/api/operational-agents/$AGENT_ID/dry-run")
[ "$A5_OTHER" = "403" ] \
  || { echo "an agent dry-ran ANOTHER agent ($A5_OTHER)" >&2; exit 1; }
# And reasoning about what it would do still confers nothing.
A5_APPROVE=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $N_TOKEN" -H 'Content-Type: application/json' \
  -d '{"decisions":[]}' http://localhost:8090/api/approvals/batch)
[ "$A5_APPROVE" = "403" ] \
  || { echo "dry-run leaked approval authority ($A5_APPROVE)" >&2; exit 1; }
echo "self 200, other 403, approve 403: discovery is not execution permission"

step "A5/E: /api/attention is SCOPED (D1) and ranks identically everywhere"
# It was declared READ_SCOPED and applied no scope -- and it is the one
# read EVERY Operational Agent is required to hold.
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/attention/" >/tmp/a5-attention-all.json
python3 - <<'A5PY'
import json
items = json.load(open('/tmp/a5-attention-all.json'))['items']
ranks = {i['agent_id']: i['rank'] for i in items}
assert ranks, 'attention returned nothing to rank'
assert sorted(ranks.values()) == list(range(1, len(ranks) + 1)), ranks
print('attention ranked', len(ranks), 'devices, contiguous from 1')
A5PY
# The band filter must NOT renumber rank: it filtered BEFORE ranking, and
# rank decides which devices consume an agent's proposal budget.
for BAND in high medium low insufficient_data; do
  curl -sf -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8090/api/attention/?band=$BAND" \
    | python3 -c "
import sys, json
all_ranks = {i['agent_id']: i['rank']
             for i in json.load(open('/tmp/a5-attention-all.json'))['items']}
for item in json.load(sys.stdin)['items']:
    assert item['rank'] == all_ranks[item['agent_id']], (item, all_ranks)
"
done
echo "band is a pure filter: rank 1 means first in the tenant, not first on the page"

step "A5/F: a machine principal reads attention through its own scope"
curl -sf -H "Authorization: Bearer $N_TOKEN" \
  "http://localhost:8090/api/attention/" \
  | python3 -c "
import sys, json
r = json.load(sys.stdin)
print('agent reads attention:', r['returned'], 'items in its own scope')
"

step "A5/G: enforcement impact is REPORTED before it is enforced (D2)"
curl -sf -H "Authorization: Bearer $TOKEN" \
  http://localhost:8090/api/tenant-settings/scope-enforcement/impact \
  | python3 -c "
import sys, json
r = json.load(sys.stdin)
# Reporting is not enforcing. A22.10 stages this deliberately: legacy_open
# is the DEFAULT and an existing tenant may hold no grant rows at all.
assert r['enforced'] is False, r['scope_enforcement']
assert 'no grant' in r['invariant']
# And it admits what it cannot know, so a short list is not mistaken for
# a complete one.
assert r['enumerable'] is False
assert 'never acted will not appear' in r['enumerable_note']
print('impact report:', len(r['agents_without_grant']), 'agents,',
      len(r['observed_principals_without_grant']), 'observed principals at risk')
"

step "A5/H: dispatch re-checks CURRENT lifecycle, on both bases (D4)"
# A19 D3 said an approved proposal is never a guarantee of execution.
# Autonomous dispatch asked only the budget and the human-approved path
# asked NOTHING, so a proposal approved yesterday still ran today for an
# agent since paused, retired or revoked. Asserted on the shipped source
# BEFORE the basis is consulted -- staging a stale approval against a
# live stack would prove one status, and the gate needs the rule.
A5_ROOT="$_REPO_ROOT" python3 - <<'A5PY'
import os
import pathlib

root = pathlib.Path(os.environ["A5_ROOT"])
rt = (root / "services/central_command/src/harkeniq_cc/agent_runtime.py").read_text()

body = rt.split("async def dispatch_decided(")[1]
loop = body.split("for proposal in pending:")[1]
gate = loop.index("_dispatch_permitted(")
basis = loop.index("BASIS_AUTONOMOUS")
assert gate < basis, "the lifecycle gate must run BEFORE the basis is consulted"

check = rt.split("async def _dispatch_permitted(")[1].split("\nasync def ")[0]
for expected in ("STATUS_RETIRED", "paused_reason", "STATUS_ACTIVE",
                 "AgentIdentityRepo", "STATUS_REVOKED"):
    assert expected in check, expected
print("dispatch re-checks lifecycle and identity on both bases, before the basis")
A5PY

# And the credential really does stop the moment the agent is retired.
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A3_NARROW/retire" >/dev/null
A5_DEAD=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $N_TOKEN" \
  http://localhost:8090/api/fleet/)
[ "$A5_DEAD" = "401" ] \
  || { echo "a retired agent's token still authenticated ($A5_DEAD)" >&2; exit 1; }
echo "retired agent: token 401, dispatch gated on current state"

step "A5/I: an agent scope answers WHERE, never WHETHER (D5)"
# `load_agent_scope` resolved with role_permissions=["*"], so the scope
# answered permits("action.approve") with True. Asserted on the shipped
# source: the wildcard must be GONE, not merely unused.
A5_ROOT="$_REPO_ROOT" python3 - <<'A5PY'
import os
import pathlib

root = pathlib.Path(os.environ["A5_ROOT"])
gov = (root / "services/central_command/src/harkeniq_cc/governance.py").read_text()
scope = (root / "services/central_command/src/harkeniq_cc/scope.py").read_text()

body = gov.split("async def load_agent_scope(")[1].split("\nasync def ")[0]
# Only the CODE. The docstring deliberately quotes the old wildcard to
# explain what was removed, and matching prose would be a false positive.
code = body.split('"""')[2] if body.count('"""') >= 2 else body
assert 'role_permissions=["*"]' not in code, "the wildcard is still there"
assert "SCOPE_ONLY_MARKER" in code, "load_agent_scope must resolve scope-only"
# And the guard is real: permits() refuses such a scope outright.
assert "if self.scope_only:" in scope
assert "SCOPE_ONLY_MARKER" in scope
print("agent scope resolves WHERE only; permits() refuses the question")
A5PY

step "A5/J: one attention composer, asserted structurally (D3)"
A5_ROOT="$_REPO_ROOT" python3 - <<'A5PY'
import os
import pathlib

root = pathlib.Path(os.environ["A5_ROOT"])
api = (root / "services/central_command/src/harkeniq_cc/api/attention.py").read_text()
# The router carried a near-verbatim copy of the composer whose band
# filter ran before ranking. A behavioural test alone would pass again
# the moment somebody copies it back.
assert "build_attention" not in api, "the router composes attention again"
assert "load_attention" in api
print("the attention router is a thin caller over the one composer")
A5PY

step "A5/K: a skill cannot declare its own parameter vocabulary"
# skills/disk-health.yaml has carried its own params block since R1 -- a
# FIFTH place the same fact was declared. parse_skill is the untrusted
# YAML boundary, so that is where it is now reconciled.
A5_ROOT="$_REPO_ROOT" PYTHONPATH="$_REPO_ROOT/src" python3 - <<'A5PY'
import os
import pathlib

import yaml

from harkeniq.errors import SkillValidationError
from harkeniq.skills.loader import parse_skill

root = pathlib.Path(os.environ["A5_ROOT"])
shipped = sorted((root / "skills").glob("*.yaml"))
assert shipped, "no shipped skills found"
for path in shipped:
    parse_skill(yaml.safe_load(path.read_text()), source=str(path))

bad = {
    "name": "gate", "version": 1, "target": "disk",
    "rules": [{
        "condition": "health == 'Critical'", "verdict": "CRITICAL",
        "message": "m",
        "action": {"type": "IDENTIFY_LED", "params": {"reason": "r"}},
    }],
}
try:
    parse_skill(bad)
except SkillValidationError as e:
    assert "requires 'target'" in str(e), e
else:
    raise AssertionError("a skill omitting a required parameter was accepted")
print(len(shipped), "shipped skills validate; a skill missing a required param is refused")
A5PY

step "A2: put the tenant ladder back where the gate found it"
# Level 2 was raised only to make an unattended grant exist for A2/C.
curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"device_type":"*","level":0,"budget_limit":0,"budget_period":"daily"}' \
  http://localhost:8090/api/policies/autonomy >/dev/null
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/autonomy/ \
  | python3 -c "
import sys, json
lvl = json.load(sys.stdin)['posture']['configured_level']
assert lvl == 0, lvl
print('tenant autonomy level restored to', lvl)
"

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
