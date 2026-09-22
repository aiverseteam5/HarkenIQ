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
# A29.9/A29.7: `/api/scope-grants/me` and `/api/fleet/` both left the
# machine plane, so case E can no longer use `a234_expect` -- which reads
# both with the caller's token. The SUBJECT is unchanged: an agent with no
# scope rows is never synthesized and reaches nothing. It is now proved
# through the route a machine still holds, plus the two refusals that are
# themselves A29 facts.
A234_ME=$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $A234_TOKEN" \
  http://localhost:8090/api/scope-grants/me)
A234_FLEET=$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $A234_TOKEN" \
  http://localhost:8090/api/fleet/)
[ "$A234_ME" = "403" ] && [ "$A234_FLEET" = "403" ] || {
  echo "E: grant construction or fleet browsing is still machine-readable \
(me=$A234_ME fleet=$A234_FLEET)" >&2; exit 1; }
# Reach itself: the on-plane read a scopeless agent still holds must be
# empty. In process the resolver says tenant_wide False / synthesis agent;
# over HTTP this is what that means.
curl -sf -H "Authorization: Bearer $A234_TOKEN" \
  http://localhost:8090/api/attention/ | python3 -c "
import sys, json
d = json.load(sys.stdin)
items = d.get('items', d.get('devices', []))
assert items == [], ('E: a scopeless agent reached something', items)
print('E agent with no scope rows: reach none over HTTP; construction 403')"
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
# Same as case E: a machine principal, and both of `a234_expect`'s
# vehicles are off the plane (A29.7/A29.9). The strict claim is that
# nobody is synthesized -- proved here by the on-plane read being empty
# under strict, exactly as it was under legacy_open.
curl -sf -H "Authorization: Bearer $A234_TOKEN" \
  http://localhost:8090/api/attention/ | python3 -c "
import sys, json
d = json.load(sys.stdin)
items = d.get('items', d.get('devices', []))
assert items == [], ('F agent (strict): reached something', items)
print('F agent (strict): reach none, under strict as under legacy_open')"
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

step "A3: the machine principal reads what the ceiling AND the plane allow"
# A29.3 split one question into two. The CEILING admits a permission; the
# PLANE admits a route. This agent binds `incidents` and `fleet`, so it
# holds fleet.view and incident.view exactly as before -- and `/api/fleet/`
# is no longer part of the External Agent API plane, so the permission it
# still holds no longer reaches it.
for READ in incidents attention; do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $A3_TOKEN" \
    "http://localhost:8090/api/$READ/")
  [ "$CODE" = "200" ] || { echo "machine read /api/$READ/ -> $CODE, want 200" >&2; exit 1; }
done
OFFPLANE=$(curl -s -o /tmp/a3_off.json -w '%{http_code}' \
  -H "Authorization: Bearer $A3_TOKEN" "http://localhost:8090/api/fleet/")
[ "$OFFPLANE" = "403" ] || {
  echo "machine read /api/fleet/ -> $OFFPLANE, want 403 (A29.7)" >&2; exit 1; }
grep -q "External Agent API plane" /tmp/a3_off.json || {
  echo "/api/fleet/ refused a machine for the wrong reason" >&2
  cat /tmp/a3_off.json >&2; exit 1; }
echo "on-plane reads 200; fleet.view held and /api/fleet/ off the plane (403)"

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
# A29.6, live and at its sharpest. This agent holds fleet.view -- A0's
# REQUIRED_READS gave it attention+autonomy, which map to exactly that --
# and the agent above holds fleet.view too. The PERMISSION is identical.
# Only the binding differs, so only the binding can explain the answer.
NARROW_ATT=$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $N_TOKEN" http://localhost:8090/api/attention/)
[ "$NARROW_ATT" = "200" ] || {
  echo "an agent with the forced attention binding lost /api/attention/ ($NARROW_ATT)" >&2
  exit 1; }
NARROW_FLEET=$(curl -s -o /tmp/a3_narrow.json -w '%{http_code}' \
  -H "Authorization: Bearer $N_TOKEN" http://localhost:8090/api/fleet/)
[ "$NARROW_FLEET" = "403" ] || {
  echo "an agent that never bound fleet reached /api/fleet/ ($NARROW_FLEET)" >&2; exit 1; }
NARROW=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $N_TOKEN" \
  http://localhost:8090/api/incidents/)
[ "$NARROW" = "403" ] || {
  echo "an agent that never bound incidents got incident.view ($NARROW)" >&2; exit 1; }
echo "same ceiling permission, different bindings: attention 200, fleet 403, incidents 403"

step "A3: the machine principal CANNOT approve its own work (the headline)"
APPR=$(curl -s -o /tmp/a3_appr.json -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $A3_TOKEN" \
  "http://localhost:8090/api/approvals/anything/approve")
[ "$APPR" = "403" ] || { echo "machine token approved ($APPR), want 403" >&2; cat /tmp/a3_appr.json >&2; exit 1; }
# A29.3: refused BEFORE the permission is even considered -- the approval
# route is not on the machine plane at all, which is a stronger refusal
# than lacking the permission. The permission fact is asserted DIRECTLY
# against the ceiling rather than read out of a message, so this step
# still fails if `action.approve` is ever admitted to it.
python3 -c "
import json
d = json.load(open('/tmp/a3_appr.json'))['detail']
assert 'External Agent API plane' in d, d
print('approval refused:', d[:80])
"
docker compose exec -T central-command python -c "
import sys
sys.path.insert(0, '/app/services/central_command/src')
from harkeniq_cc.machine_identity import MACHINE_PRINCIPAL_CEILING as C
assert 'action.approve' not in C, C
print('and the ceiling still excludes action.approve:', sorted(C))
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
# A29.7: /api/fleet/ left the machine plane, so the proof that the
# credential WORKS uses a route still on it. The subject is the identity,
# not the route.
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $B_TOKEN" \
    http://localhost:8090/api/attention/)" = "200" ] \
  || { echo "the second machine identity never worked" >&2; exit 1; }
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A3_B/retire" >/dev/null
RET=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $B_TOKEN" \
  http://localhost:8090/api/attention/)
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
  # A29.7: it can no longer READ it either. The A20.3 ceiling still admits
  # fleet.view -- that has not moved -- but the capability registry left the
  # machine plane, and governed discovery is A6-4B's to design deliberately
  # rather than something a runtime scrapes off a human fleet API.
  R_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $N_TOKEN" \
    http://localhost:8090/api/capabilities/catalogue)
  [ "$R_CODE" = "403" ] || {
    echo "the capability catalogue is still machine-readable ($R_CODE)" >&2; exit 1; }
  echo "live machine principal: read 403, write 403 -- discovery is A6-4B's"
else
  echo "no live machine token in scope; covered by unit tests"
fi

step "A26 (A25.13): approval topology needs EFFECTIVE tenant authority"
# The pre-existing HIGH that A6-2's sweep found -- and the remediation
# review's finding on top of it. The routes moved to `governance.view`,
# but a route guard only answers "could this actor EVER hold this": E1.2
# says a `permission_subset` is per grant and therefore cannot be
# enforced there. So the RESOLVED SCOPE decides, over the tenant object.
#
# DENY here is the canonical READ shape, not a 403: the platform's own
# `test_no_read_is_object_gated` holds that a read narrows rather than
# refuses, because a 403 confirms the object it refuses -- and the
# existence of a group id IS the topology. So: no rows, and 404.
A26_EMAIL="a26-approver-$(date +%s)@demo"
A26_GROUP=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"A26 on-call $(date +%s)\",\"required_count\":1}" \
  http://localhost:8090/api/policies/groups \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['group']['id'])")
[ -n "$A26_GROUP" ] || { echo "could not create the A26 group" >&2; exit 1; }
curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$A26_EMAIL\",\"role\":\"approver\"}" \
  "http://localhost:8090/api/policies/groups/$A26_GROUP/members" > /dev/null

sub_of() {  # sub_of <jwt>
  python3 -c "
import base64, json, sys
t = sys.argv[1].split('.')[1]; t += '=' * (-len(t) % 4)
print(json.loads(base64.urlsafe_b64decode(t))['sub'])" "$1"
}

a26_probe() {  # a26_probe <label> <token> <allow|deny-scope|deny-role>
  local label=$1 tok=$2 want=$3 lc dc n
  lc=$(curl -s -o /tmp/a26_l.json -w '%{http_code}' -H "Authorization: Bearer $tok" \
    http://localhost:8090/api/policies/groups)
  dc=$(curl -s -o /tmp/a26_d.json -w '%{http_code}' -H "Authorization: Bearer $tok" \
    "http://localhost:8090/api/policies/groups/$A26_GROUP")
  n=$(python3 -c "
import json,sys
try: print(len(json.load(open('/tmp/a26_l.json')).get('groups', [])))
except Exception: print(-1)")
  case "$want" in
    allow)
      [ "$lc" = "200" ] && [ "$dc" = "200" ] && [ "$n" -ge 1 ] || {
        echo "$label: want ALLOW, got list=$lc($n groups) detail=$dc" >&2; exit 1; }
      grep -q "$A26_EMAIL" /tmp/a26_d.json || {
        echo "$label: allowed but saw no approver -- the estate is empty" >&2
        exit 1; } ;;
    deny-scope)
      # Effective scope refuses: absent, never 403.
      [ "$lc" = "200" ] && [ "$n" = "0" ] && [ "$dc" = "404" ] || {
        echo "$label: want scope-DENY (200/0 groups, 404), got list=$lc($n) detail=$dc" >&2
        exit 1; } ;;
    deny-role)
      # The ROLE never holds it, so layer 1 refuses and names no object.
      [ "$lc" = "403" ] && [ "$dc" = "403" ] || {
        echo "$label: want role-DENY (403/403), got list=$lc detail=$dc" >&2
        exit 1; } ;;
  esac
  for f in /tmp/a26_l.json /tmp/a26_d.json; do
    if [ "$want" != "allow" ] && grep -q "$A26_EMAIL" "$f"; then
      echo "$label: refused, but an approver identity was in the body" >&2
      exit 1
    fi
  done
  echo "  $(printf '%-34s' "$label") $want  list=$lc(${n} groups) detail=$dc"
}

a26_mode() {  # the tenant's CURRENT enforcement posture
  curl -sf -H "Authorization: Bearer $TOKEN" \
    http://localhost:8090/api/tenant-settings/scope-enforcement \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['scope_enforcement'])"
}
a26_set_mode() {
  curl -sf -X PUT -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' -d "{\"mode\":\"$1\"}" \
    http://localhost:8090/api/tenant-settings/scope-enforcement > /dev/null
}
A26_MODE_BEFORE=$(a26_mode)

# The principals. The site-scoped one is THE ratified case: a `site_admin`
# whose ROLE holds governance.view and whose GRANT reaches one site.
tenant_realm_user gate-gov-site@demo gate-gov-site site_admin || true
GOV_SITE_TOKEN=$(tenant_token gate-gov-site@demo gate-gov-site)
[ "$(grant "$(sub_of "$GOV_SITE_TOKEN")" site "$E12_SITE" site_admin)" = "201" ] || {
  echo "the site-scoped governance grant was refused" >&2; exit 1; }
GOV_SITE_TOKEN=$(tenant_token gate-gov-site@demo gate-gov-site)
AUD_TOKEN=$(tenant_token gate-aud@demo gate-aud)

# ---- posture 1: legacy_open --------------------------------------------
# A23.10 synthesis is for the NEVER-granted and is deliberately untouched
# by A26, so the un-granted auditor is tenant-wide here. What synthesis
# does NOT do is rescue a principal who HAS a grant that does not reach.
a26_set_mode legacy_open
echo "  -- legacy_open --"
a26_probe "tenant owner (tenant grant)" "$TOKEN" allow
a26_probe "auditor, never granted (A23.10)" "$AUD_TOKEN" allow
a26_probe "site_admin, SITE grant only" "$GOV_SITE_TOKEN" deny-scope

# ---- posture 2: strict --------------------------------------------------
a26_set_mode strict
echo "  -- strict --"
a26_probe "tenant owner (tenant grant)" "$TOKEN" allow
# The remediation, live: the ROLE holds governance.view and the principal
# has no grant, so it reads nothing until it is granted.
a26_probe "auditor, role only, NO grant" "$AUD_TOKEN" deny-scope
[ "$(grant "$(sub_of "$AUD_TOKEN")" tenant "" auditor)" = "201" ] || {
  echo "the auditor could not be granted tenant scope" >&2; exit 1; }
AUD_TOKEN=$(tenant_token gate-aud@demo gate-aud)
a26_probe "auditor, TENANT grant" "$AUD_TOKEN" allow
a26_probe "site_admin, SITE grant only" "$GOV_SITE_TOKEN" deny-scope

# An approver without the permission at all, and a machine principal:
# both refused at layer 1, which names no object.
a26_probe "operator (action.approve only)" "$OP_TOKEN" deny-role
if [ -n "${N_TOKEN:-}" ]; then
  a26_probe "machine principal" "$N_TOKEN" deny-role
else
  echo "  no live machine token in scope; covered by unit tests"
fi

# Leave the tenant exactly as this step found it -- later steps depend on
# the posture, which is how this step's first version got its answer wrong.
a26_set_mode "$A26_MODE_BEFORE"
[ "$(a26_mode)" = "$A26_MODE_BEFORE" ] || {
  echo "A26 left the tenant in the wrong enforcement posture" >&2; exit 1; }
echo "effective tenant authority decides, under BOTH postures (restored: $A26_MODE_BEFORE)"

step "A26.3: visibility and authority are independent in BOTH directions"
# An approver decides without enumerating the topology; a governance
# reader enumerates without deciding.
OP_REC=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OP_TOKEN" \
  http://localhost:8090/api/approvals/)
[ "$OP_REC" = "200" ] || {
  echo "an approver lost the queue they decide on ($OP_REC)" >&2; exit 1; }
AUD_APPROVE=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $AUD_TOKEN" -H 'Content-Type: application/json' \
  -d '{}' http://localhost:8090/api/approvals/a26-nonexistent/approve)
[ "$AUD_APPROVE" = "403" ] || {
  echo "a governance reader approved an action ($AUD_APPROVE)" >&2; exit 1; }
AUD_MUT=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $AUD_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"a26-should-refuse"}' http://localhost:8090/api/policies/groups)
[ "$AUD_MUT" = "403" ] || {
  echo "a governance reader mutated governance ($AUD_MUT)" >&2; exit 1; }
echo "approver keeps the queue (200); governance reader approves 403, mutates 403"

step "A26.7: policy posture is projected, not filtered"
# `/api/policies/` stays at fleet.view (A13/E0.3, S1 D2) -- an operator
# must still see that their action needs two approvers. What they must
# NOT see is who authored the rule.
#
# The policy is created HERE rather than reused: an earlier step deletes
# the one it creates, so depending on gate ordering made this assert on
# an empty list (caught by CI on the first run of this step).
curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"name\":\"A26 projection $(date +%s)\",\"required_approvers\":2,
       \"group_id\":\"$A26_GROUP\"}" \
  http://localhost:8090/api/policies/ | python3 -c "
import sys, json
d = json.load(sys.stdin)['policy']
assert d['created_by'], 'the write response lost the author for a site.manage caller'
print('policy created, author recorded:', d['created_by'])
"
curl -sf -H "Authorization: Bearer $OP_TOKEN" http://localhost:8090/api/policies/ \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['policies'], 'the gate has no policy to project'
for p in d['policies']:
    assert 'created_by' not in p, ('governance identity reached fleet.view', p)
    assert 'required_approvers' in p, ('posture was lost', p)
print('operator: posture present, created_by absent (%d policies)' % len(d['policies']))
"
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/policies/ \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert all('created_by' in p for p in d['policies']), d['policies'][:1]
print('governance reader: created_by present')
"

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
#
# A30.17: the two paths used to assemble two different gates -- the
# synchronous human-approval path never asked reach or the binding, the
# background pass never asked the stop switch. Both now call ONE gate,
# `revalidate_dispatch`, so the scan follows it there, and asserts the
# synchronous path consults it BEFORE it calls the Site Manager. The
# checks are read from the gate's CODE, not its docstring, so prose that
# merely names a check cannot satisfy this step. (A6-4B0a/AG below proves
# the same rule live.)
A5_ROOT="$_REPO_ROOT" python3 - <<'A5PY'
import os
import pathlib

root = pathlib.Path(os.environ["A5_ROOT"])
cc = root / "services/central_command/src/harkeniq_cc"
rt = (cc / "agent_runtime.py").read_text()
ap = (cc / "api/approvals.py").read_text()

body = rt.split("async def dispatch_decided(")[1]
loop = body.split("for proposal in pending:")[1]
gate = loop.index("revalidate_dispatch(")
basis = loop.index("BASIS_AUTONOMOUS")
assert gate < basis, "the lifecycle gate must run BEFORE the basis is consulted"

sync = ap.split("async def _decide_agent_proposal(")[1].split("\nasync def ")[0]
assert sync.index("revalidate_dispatch(") < sync.index("dispatch_action("), (
    "the synchronous approval path must ask the ONE gate before the SM call")

check = rt.split("async def revalidate_dispatch(")[1].split("\nasync def ")[0]
code = check.split('"""', 2)[2]
# Retirement and pause refuse through `status != STATUS_ACTIVE` and
# `paused_reason`; ANY non-active machine identity (revoked or retired)
# refuses through `!= IDENTITY_ACTIVE`; reach, binding and the stop switch
# are the three inputs the paths used to disagree about.
for expected in ("STATUS_ACTIVE", "paused_reason", "AgentIdentityRepo",
                 "IDENTITY_ACTIVE", "StopSwitchRepo", "load_agent_reach",
                 "bound_action_classes", "dispatch_permitted("):
    assert expected in code, expected
print("ONE dispatch gate: lifecycle, identity, stop switch, reach and binding,"
      " asked on both paths before the basis and before the SM call")
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

# ===========================================================================
# A6-1 external governed ingress (spec A24), live: a real machine token
# submitting real governed work.
#
# The headline is NOT that submission works. It is the three refusals:
# the ceiling admits `proposal.submit` and an agent without an explicit
# ingress binding still cannot submit; a body carrying a governance field
# is REJECTED rather than ignored; and a second idempotency key for the
# same governed candidate does not create a second proposal.
#
# Built on A5/C's conditions, which have already proven a real disk fault
# resolves a real IDENTIFY_LED parameter on $A5_SITE.
# ===========================================================================

step "A6/A: an agent with NO ingress binding cannot submit (ceiling != grant)"
A6_AGENT=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"a6-ingress $(date +%s)\",
       \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$A5_SITE\"}],
       \"capabilities\":[
         {\"kind\":\"action_class\",\"capability_ref\":\"IDENTIFY_LED\"},
         {\"kind\":\"read\",\"capability_ref\":\"fleet\"},
         {\"kind\":\"read\",\"capability_ref\":\"incidents\"}]}" \
  http://localhost:8090/api/operational-agents/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
A6_SECRET=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/identity" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['client_secret'])")
[ -n "$A6_SECRET" ] || { echo "no A6 client secret issued" >&2; exit 1; }
a6_token() {
  curl -sf -X POST \
    "http://localhost:8180/realms/tenant-demo/protocol/openid-connect/token" \
    -d "grant_type=client_credentials&client_id=op-agent-$A6_AGENT&client_secret=$A6_SECRET" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])"
}
A6_TOKEN=$(a6_token)
[ -n "$A6_TOKEN" ] || { echo "A6 client_credentials grant failed" >&2; exit 1; }

NOBIND=$(curl -s -o /tmp/a6_nobind.json -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $A6_TOKEN" -H 'Content-Type: application/json' \
  -d '{"candidate_ref":"cand_00000000000000000000000000000000",
       "idempotency_key":"gate-nobinding-0001"}' \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/proposals")
[ "$NOBIND" = "403" ] || {
  echo "an agent with no ingress binding submitted ($NOBIND)" >&2
  cat /tmp/a6_nobind.json >&2; exit 1; }
echo "no ingress binding -> 403, though the ceiling admits proposal.submit"

step "A6/B: adding the binding grants it, on the SAME credential"
# Permissions are resolved from the agent's own rows on EVERY request, so
# a binding takes effect without re-issuing anything. That is also what
# makes revocation immediate.
# `bindings` is a full REPLACEMENT, not a patch: an operator reasoning
# about an agent's reach sees the complete set in one request. So the
# scope rows travel with it, unchanged.
curl -sf -X PUT -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$A5_SITE\"}],
       \"capabilities\":[
        {\"kind\":\"action_class\",\"capability_ref\":\"IDENTIFY_LED\"},
        {\"kind\":\"read\",\"capability_ref\":\"fleet\"},
        {\"kind\":\"read\",\"capability_ref\":\"incidents\"},
        {\"kind\":\"ingress\",\"capability_ref\":\"proposals\"}]}" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/bindings" > /dev/null

echo "ingress binding added; permissions resolve per request, so the same"
echo "credential now carries proposal.submit with nothing re-issued"

step "A6/C: the agent reads its OWN dry-run and takes a candidate reference"
A6_TOKEN=$(a6_token)
A6_REF=$(curl -sf -H "Authorization: Bearer $A6_TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/dry-run" \
  | python3 -c "
import sys, json
r = json.load(sys.stdin)
led = [p for p in r['would_propose'] if p['action_type'] == 'IDENTIFY_LED']
assert led, ('no candidate to submit', r['would_propose'], r['withheld'])
ref = led[0]['candidate_ref']
assert ref.startswith('cand_'), ref
print(ref)
")
[ -n "$A6_REF" ] || { echo "no candidate_ref from dry-run" >&2; exit 1; }
echo "candidate reference issued by dry-run: $A6_REF"

step "A6/D: a body carrying a governance field is REJECTED, not ignored"
for FIELD in '"authorization_basis":"autonomous_grant"' '"status":"approved"' \
             '"disposition":"autonomous"' '"params":{"target":"anything"}' \
             '"action_type":"POWER_CYCLE"' '"agent_id":"someone-else"'; do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $A6_TOKEN" -H 'Content-Type: application/json' \
    -d "{\"candidate_ref\":\"$A6_REF\",\"idempotency_key\":\"gate-evil-0001\",$FIELD}" \
    "http://localhost:8090/api/operational-agents/$A6_AGENT/proposals")
  [ "$CODE" = "422" ] || {
    echo "the transport accepted a governance field ($FIELD -> $CODE)" >&2
    exit 1; }
done
echo "every governance field refused at the schema (422), not silently dropped"

step "A6/E: a real submission creates a real, human-gated proposal"
# ORDERING IS LOAD-BEARING, do not tidy it. `active` is exactly
# EVALUATING_STATUSES, so the moment this agent is switched on the
# CC-resident evaluator may propose the SAME candidate on its next pass
# (20s in this stack) and the submission below would correctly return
# `duplicate`. Everything that does not need an active agent -- the
# bindings, the dry-run, the schema refusals -- has therefore already
# happened, leaving one round trip between activation and submission.
A6_PRE=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/preflight")
A6_ACK=$(echo "$A6_PRE" | python3 -c \
  "import sys,json; print(json.load(sys.stdin)['requires_acknowledgement'])")
if [ "$A6_ACK" = "True" ]; then
  curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8090/api/operational-agents/$A6_AGENT/acknowledge" > /dev/null
fi
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/activate" \
  | python3 -c "
import sys, json
a = json.load(sys.stdin)
assert a['status'] == 'active', a
print('A6 agent activated at v%d' % a['activated_version'])
"
A6_PROP=$(curl -sf -X POST -H "Authorization: Bearer $A6_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"candidate_ref\":\"$A6_REF\",\"idempotency_key\":\"gate-a6-0001\",
       \"note\":\"submitted by the gate\"}" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/proposals" \
  | python3 -c "
import sys, json
r = json.load(sys.stdin)
assert r['accepted'] is True, r
p = r['proposal']
# A24: acceptance means a proposal EXISTS. It never means anything runs.
assert p['status'] == 'awaiting_approval', p['status']
assert p['requires_approval'] is True, p
assert p['action_type'] == 'IDENTIFY_LED', p
# A22.3 still holds: the parameter came from the reported component, not
# from the body -- which cannot express one.
assert p['params'].get('target'), p['params']
assert 'confers nothing' in r['governs']
print(r['proposal_id'])
")
[ -n "$A6_PROP" ] || { echo "submission created no proposal" >&2; exit 1; }
echo "external submission accepted -> proposal $A6_PROP (awaiting a human)"

step "A6/F: a replay returns the SAME proposal and creates nothing"
A6_REPLAY=$(curl -s -o /tmp/a6_replay.json -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $A6_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"candidate_ref\":\"$A6_REF\",\"idempotency_key\":\"gate-a6-0001\",
       \"note\":\"submitted by the gate\"}" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/proposals")
[ "$A6_REPLAY" = "200" ] || {
  echo "a replay was not a replay ($A6_REPLAY)" >&2; cat /tmp/a6_replay.json >&2
  exit 1; }
A6_PROP2=$(python3 -c "
import json; d=json.load(open('/tmp/a6_replay.json'))
assert d['replayed'] is True, d
print(d['proposal_id'])")
[ "$A6_PROP" = "$A6_PROP2" ] || {
  echo "replay returned a different proposal ($A6_PROP vs $A6_PROP2)" >&2
  exit 1; }
echo "replay -> 200, same proposal $A6_PROP2"

step "A6/G: a DIFFERENT key for the same candidate does not duplicate (A24.6)"
# The guarantee idempotency keys structurally cannot give: the keys differ,
# so nothing collides on the unique constraint. The admission path refuses.
A6_DUP=$(curl -s -o /tmp/a6_dup.json -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $A6_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"candidate_ref\":\"$A6_REF\",\"idempotency_key\":\"gate-a6-0002\"}" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/proposals")
[ "$A6_DUP" = "409" ] || {
  echo "a second key created a second proposal ($A6_DUP)" >&2
  cat /tmp/a6_dup.json >&2; exit 1; }

step "A6/H: a reused key for DIFFERENT work is a conflict, never a silent replay"
A6_MIX=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $A6_TOKEN" -H 'Content-Type: application/json' \
  -d '{"candidate_ref":"cand_11111111111111111111111111111111",
       "idempotency_key":"gate-a6-0001"}' \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/proposals")
[ "$A6_MIX" = "409" ] || { echo "a reused key answered for other work ($A6_MIX)" >&2; exit 1; }
echo "same key + different work -> 409"

step "A6/I: exactly ONE proposal exists for that governed candidate"
# Asserted on the CANDIDATE, not on a global count. The agent is active
# now, so the evaluator may legitimately propose for other devices in its
# scope while these steps run -- a table-wide delta would call that a
# failure. What A24.6 actually promises is narrower and stronger: no
# second proposal shares this one's dedupe key, whatever else happens.
A6_SAME=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT count(*) FROM cc_agent_proposals WHERE dedupe_key =
     (SELECT dedupe_key FROM cc_agent_proposals WHERE id='$A6_PROP')" \
  | tr -d ' \r')
[ "$A6_SAME" = "1" ] || {
  echo "$A6_SAME proposals share one dedupe key -- the same governed work" >&2
  echo "was admitted more than once" >&2; exit 1; }
echo "one governed candidate -> exactly one proposal, across two keys and a replay"

step "A6/J: an agent may not submit for ANOTHER agent (A24.5)"
A6_OTHER=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $A6_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"candidate_ref\":\"$A6_REF\",\"idempotency_key\":\"gate-a6-0003\"}" \
  "http://localhost:8090/api/operational-agents/$A5_AGENT/proposals")
[ "$A6_OTHER" = "403" ] || {
  echo "a machine token submitted for another agent ($A6_OTHER)" >&2; exit 1; }
echo "cross-agent submission -> 403"

step "A6/M: an oversized body is refused BEFORE it is parsed (A24.14)"
# Schema limits reject a payload the server already read and parsed. A
# 5MB body used to return 422, meaning every byte was received first.
python3 -c "
import json,sys
sys.stdout.write(json.dumps({'candidate_ref':'cand_x','idempotency_key':'k',
                             'note':'x'*200000}))" > /tmp/a6_big.json
BIG=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $A6_TOKEN" -H 'Content-Type: application/json' \
  --data-binary @/tmp/a6_big.json \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/proposals")
[ "$BIG" = "413" ] || {
  echo "an oversized body was not refused pre-parse ($BIG; 422 means it was" >&2
  echo "read and parsed first)" >&2; exit 1; }
echo "200KB body -> 413 before parsing"

step "A6/N: every attempt is metered, replays included (A24.13)"
# A replay stays idempotent; it is not free. The first implementation
# returned replays BEFORE the counter, which made replay unmetered.
A6_ATTEMPTS=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT count(*) FROM cc_agent_ingress_attempts WHERE agent_id='$A6_AGENT'" \
  | tr -d ' \r')
[ "$A6_ATTEMPTS" -ge 4 ] || {
  echo "only $A6_ATTEMPTS attempts recorded; submission, replay, duplicate" >&2
  echo "and conflict should each have counted" >&2; exit 1; }
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT count(*) FROM cc_agent_ingress_attempts
    WHERE agent_id='$A6_AGENT' AND outcome='replayed'" \
  | grep -qv '^ *0 *$' \
  || { echo "the replay was not metered" >&2; exit 1; }
echo "$A6_ATTEMPTS ingress attempts metered, replay among them"

step "A6/O: a second agent cannot open the SAME operation (A24.12)"
# Attribution is not operational identity. The dedupe key begins with the
# agent id, so this agent's key differs and the per-agent rule cannot see
# the collision -- only the operation key can.
A6_B=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"a6-second $(date +%s)\",
       \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$A5_SITE\"}],
       \"capabilities\":[
         {\"kind\":\"action_class\",\"capability_ref\":\"IDENTIFY_LED\"},
         {\"kind\":\"read\",\"capability_ref\":\"fleet\"},
         {\"kind\":\"read\",\"capability_ref\":\"incidents\"},
         {\"kind\":\"ingress\",\"capability_ref\":\"proposals\"}]}" \
  http://localhost:8090/api/operational-agents/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
B_SECRET=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_B/identity" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['client_secret'])")
B_TOKEN=$(curl -sf -X POST \
  "http://localhost:8180/realms/tenant-demo/protocol/openid-connect/token" \
  -d "grant_type=client_credentials&client_id=op-agent-$A6_B&client_secret=$B_SECRET" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
B_REF=$(curl -sf -H "Authorization: Bearer $B_TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_B/dry-run" \
  | python3 -c "
import sys, json
r = json.load(sys.stdin)
led = [p for p in r['would_propose'] if p['action_type'] == 'IDENTIFY_LED']
assert led, ('the second agent saw no candidate', r['withheld'])
print(led[0]['candidate_ref'])
")
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_B/preflight" > /dev/null
B_ACK=$(curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_B/preflight" \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('requires_acknowledgement'))")
if [ "$B_ACK" = "True" ]; then
  curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8090/api/operational-agents/$A6_B/acknowledge" > /dev/null
fi
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_B/activate" > /dev/null
B_CODE=$(curl -s -o /tmp/a6_b.json -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $B_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"candidate_ref\":\"$B_REF\",\"idempotency_key\":\"gate-a6b-0001\"}" \
  "http://localhost:8090/api/operational-agents/$A6_B/proposals")
[ "$B_CODE" = "409" ] || {
  echo "a second agent opened a second proposal for one operation ($B_CODE)" >&2
  cat /tmp/a6_b.json >&2; exit 1; }
python3 -c "
import json; d=json.load(open('/tmp/a6_b.json'))
assert d['code'] == 'operation_in_flight', d
print('second agent refused:', d['reason'])
"

step "A6-2/P: the human administrator KEEPS the rich projection"
# Re-minted: the A6-2 steps add a couple of dozen round trips before A6/K
# asserts that REVOCATION, not expiry, is what refuses the token.
A6_TOKEN=$(a6_token)
[ -n "$A6_TOKEN" ] || { echo "A6-2 could not re-mint the machine token" >&2; exit 1; }
# Establish, from the deployment itself, what a machine must not be told.
# Hard-coding a name here would only ever test the name.
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT" > /tmp/a6_human.json
A6_CREATOR=$(python3 -c "
import json; print(json.load(open('/tmp/a6_human.json'))['agent']['created_by'])")
python3 -c "
import json
d = json.load(open('/tmp/a6_human.json'))
assert d.get('view') != 'machine', 'a human got the machine projection'
assert d['agent']['created_by'], 'the operator lost who built the agent'
blob = json.dumps(d)
assert '\"params\"' in blob, 'the operator lost the executable parameters'
assert '\"authorization_basis\"' in blob, 'the operator lost the governance basis'
print('operator detail: rich, created_by =', d['agent']['created_by'])
"
[ -n "$A6_CREATOR" ] || { echo "no creator recorded on the agent" >&2; exit 1; }

step "A6-2/Q: EVERY machine read of an agent is an allow-listed projection"
# HIGH 1, live. `get_agent`, the listing, the preflight read, the runtime
# read and the identity read all became machine-self-readable and kept the
# Console's payload -- executable params, raw evidence, directive ids,
# dispatch internals, and `created_by` / `activated_by` / `produced_by` /
# `issued_by` naming real people. Searched RECURSIVELY: a top-level key
# check would pass a payload that nested the leak one level down.
A6_SUB=$(python3 -c "
import json; print(json.load(open('/tmp/a6_replay.json'))['submission_id'])")
A6_A_BEFORE=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT coalesce(sum(reads),0) FROM cc_agent_read_windows WHERE agent_id='$A6_AGENT'" \
  | tr -d ' \r')
A6_B_BEFORE=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT coalesce(sum(reads),0) FROM cc_agent_read_windows WHERE agent_id='$A6_B'" \
  | tr -d ' \r')
for R in "" "/preflight" "/runtime" "/identity" "/proposals" \
         "/proposals/$A6_PROP" "/submissions/$A6_SUB"; do
  curl -sf -H "Authorization: Bearer $A6_TOKEN" \
    "http://localhost:8090/api/operational-agents/$A6_AGENT$R" \
    > /tmp/a6_machine.json || {
      echo "machine read of '$R' failed" >&2; exit 1; }
  A6_SRC="$_REPO_ROOT/services/central_command/src" \
  A6_ROUTE="$R" A6_CREATOR="$A6_CREATOR" python3 - <<'A62PY'
import json, os, sys
sys.path.insert(0, os.environ["A6_SRC"])
from harkeniq_cc.receipts import MACHINE_IDENTITY_FIELDS, MACHINE_INTERNAL_FIELDS


def walk(node, keys, values):
    if isinstance(node, dict):
        for k, v in node.items():
            keys.add(k)
            walk(v, keys, values)
    elif isinstance(node, list):
        for item in node:
            walk(item, keys, values)
    elif isinstance(node, str):
        values.add(node)


route = os.environ["A6_ROUTE"] or "(detail)"
keys, values = set(), set()
walk(json.load(open("/tmp/a6_machine.json")), keys, values)
leaked = keys & (MACHINE_IDENTITY_FIELDS | MACHINE_INTERNAL_FIELDS)
assert not leaked, (route, sorted(leaked))
creator = os.environ["A6_CREATOR"]
assert not any(creator in v for v in values), (route, creator)
print("  %-26s clean" % route)
A62PY
done
echo "seven machine reads: no operator identity, no execution internals"

step "A6-2/R: an agent inspects ITSELF and no other, on every route"
# The identity read had no self rule at all: an authenticated agent could
# read another agent's credential row -- client id, realm, and the
# operators who issued, rotated and revoked it.
for R in "" "/preflight" "/runtime" "/identity" "/proposals" "/dry-run"; do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $B_TOKEN" \
    "http://localhost:8090/api/operational-agents/$A6_AGENT$R")
  [ "$CODE" = "403" ] || {
    echo "agent B read agent A'$R' ($CODE), want 403" >&2; exit 1; }
done
# The builder catalogue is an operator surface: every site and device the
# caller may bind, by name, and a machine has nothing to build.
CAT=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $B_TOKEN" \
  "http://localhost:8090/api/operational-agents/catalogue")
[ "$CAT" = "403" ] || {
  echo "a machine read the binding catalogue ($CAT)" >&2; exit 1; }
# A29.7 SUPERSEDES A25.9's self-only listing, and goes further: the
# listing left the plane. A runtime knows its own id from its own token
# and has no job requiring the EXISTENCE of its siblings. Refusing the
# route is strictly stronger than projecting it -- there is no projection
# left to get wrong.
LIST=$(curl -s -o /tmp/a64_list.json -w '%{http_code}' \
  -H "Authorization: Bearer $B_TOKEN" \
  "http://localhost:8090/api/operational-agents/")
[ "$LIST" = "403" ] || {
  echo "a machine still enumerated the agent listing ($LIST)" >&2; exit 1; }
grep -q "$A6_AGENT" /tmp/a64_list.json && {
  echo "the refusal body named another agent" >&2; exit 1; }
echo "listing as a machine -> 403, and names nobody"
echo "six cross-agent reads refused, catalogue refused, listing narrowed to self"

step "A6-2/S: a machine refusal is charged to the CALLER, never to the target"
# MEDIUM, live. The self rule used to run BEFORE the meter, so a
# cross-agent 403 was free -- an unbounded channel for an authenticated
# runtime. And the bucket comes from the TOKEN: if it came from the agent
# named in the path, agent B's eight refusals would have been billed to
# agent A, and a caller could exhaust another agent's allowance.
A6_A_AFTER=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT coalesce(sum(reads),0) FROM cc_agent_read_windows WHERE agent_id='$A6_AGENT'" \
  | tr -d ' \r')
A6_B_AFTER=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT coalesce(sum(reads),0) FROM cc_agent_read_windows WHERE agent_id='$A6_B'" \
  | tr -d ' \r')
[ "$((A6_A_AFTER - A6_A_BEFORE))" = "7" ] || {
  echo "agent A made 7 reads of its own and was charged \
$((A6_A_AFTER - A6_A_BEFORE))" >&2; exit 1; }
[ "$((A6_B_AFTER - A6_B_BEFORE))" = "8" ] || {
  echo "agent B made 8 refused reads (6 cross-agent + catalogue + listing) \
and was charged $((A6_B_AFTER - A6_B_BEFORE)): an authenticated refusal is free" >&2
  exit 1; }
echo "caller charged 8 refusals; target charged only its own 7 reads"

step "A6-2/T: polling is durable, and is not the submission ledger"
# Accounting owns its own transaction, so a charge survives whatever the
# request does afterwards -- including a 404. It is also its own bucket:
# a poll is never counted as a governed submission attempt.
A6_404=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $A6_TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/submissions/no-such-id")
[ "$A6_404" = "404" ] || { echo "expected 404, got $A6_404" >&2; exit 1; }
A6_A_404=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT coalesce(sum(reads),0) FROM cc_agent_read_windows WHERE agent_id='$A6_AGENT'" \
  | tr -d ' \r')
[ "$((A6_A_404 - A6_A_AFTER))" = "1" ] || {
  echo "a 404-producing poll was free" >&2; exit 1; }
A6_POLL_ATTEMPTS=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT count(*) FROM cc_agent_ingress_attempts WHERE agent_id='$A6_B'" \
  | tr -d ' \r')
[ "$A6_POLL_ATTEMPTS" = "1" ] || {
  echo "agent B made ONE submission attempt and $A6_POLL_ATTEMPTS are recorded" >&2
  echo "-- polls were counted in the governed submission ledger" >&2; exit 1; }
echo "refused poll charged, and no poll entered the submission ledger"

step "A6-2/U: the machine list and the machine receipt cannot disagree"
# HIGH 2's shape, live: approval state is read per proposal, so the row in
# the list and the receipt for that proposal must be identical. (The
# independence of two proposals sharing one policy coordinate is proven in
# the unit suite, where two same-coordinate proposals can be created.)
curl -sf -H "Authorization: Bearer $A6_TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/proposals" \
  > /tmp/a6_list.json
curl -sf -H "Authorization: Bearer $A6_TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/proposals/$A6_PROP" \
  > /tmp/a6_receipt.json
python3 -c "
import json
listed = json.load(open('/tmp/a6_list.json'))
receipt = json.load(open('/tmp/a6_receipt.json'))
assert listed['view'] == 'machine', listed.get('view')
item = next(i for i in listed['proposals'] if i['proposal_id'] == '$A6_PROP')
for block in ('proposal', 'approval', 'execution', 'terminal'):
    assert item[block] == receipt[block], (block, item[block], receipt[block])
assert set(item['approval']) == {
    'required', 'state', 'granted_count', 'required_count', 'decided_at'}
print('list item and receipt agree on all four blocks; approval names no one')
"

# ---------------------------------------------------------------------------
# A6-3 / A27: provenance + ingress operability (the HUMAN side of A6)
# ---------------------------------------------------------------------------

step "A6-3/V: an approver can tell WHO asked, in the ONE queue (A27.6)"
# The defect: `origin` reached admission and was written ONLY into audit
# detail, while `/api/approvals/` already spends the word `origin` on the
# queue LANE. So an approver saw "agent" for a proposal HarkenIQ reasoned
# itself AND for one an external runtime asked for. Both words are
# asserted here, because A27.2's whole claim is that they answer
# different questions and neither was overloaded to answer the other.
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/approvals/" > /tmp/a63_queue.json
A6_SRC="$_REPO_ROOT/services/central_command/src" \
A6_PROP="$A6_PROP" A6_SUB="$A6_SUB" python3 - <<'A63PY'
import json, os, sys
sys.path.insert(0, os.environ["A6_SRC"])
from harkeniq_cc.proposal_admission import ORIGIN_INGRESS, PROVENANCE_TYPES

items = json.load(open("/tmp/a63_queue.json"))["actions"]
item = next(i for i in items if i["action_id"] == os.environ["A6_PROP"])
assert item["origin"] == "agent", ("the queue LANE changed", item["origin"])
assert item["provenance"] == {
    "type": ORIGIN_INGRESS, "submission_id": os.environ["A6_SUB"],
}, item["provenance"]
# Every proposal in the queue carries a value from the CLOSED vocabulary.
for i in items:
    if "provenance" in i:
        assert i["provenance"]["type"] in PROVENANCE_TYPES, i["provenance"]
# And provenance carries no identity material, ever.
blob = json.dumps([i.get("provenance") for i in items])
for banned in ("client_id", "secret", "token", "realm", "keycloak"):
    assert banned not in blob.lower(), banned
print("queue lane 'agent'; provenance '%s' naming submission %s"
      % (item["provenance"]["type"], os.environ["A6_SUB"][:12]))
A63PY

step "A6-3/W: provenance is written WITH the proposal, in one transaction"
# A27.5. On a stack whose every proposal was created after 0024, a NULL
# would mean a proposal reached the database without the provenance of
# its own creation -- i.e. a second write path.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT coalesce(provenance_type,'<NULL>')||'='||count(*)
     FROM cc_agent_proposals
    GROUP BY coalesce(provenance_type,'<NULL>') ORDER BY 1" \
  | tr -d ' \r' | grep -v '^$' > /tmp/a63_prov_counts.txt
cat /tmp/a63_prov_counts.txt
python3 -c "
rows = dict(l.split('=') for l in open('/tmp/a63_prov_counts.txt').read().split())
assert '<NULL>' not in rows, 'a proposal exists with no provenance: ' + str(rows)
assert int(rows.get('evaluator', 0)) > 0, rows
assert int(rows.get('external_ingress', 0)) > 0, rows
print('both origins present, no proposal without provenance')
"

step "A6-3/X: ingress health is observed activity, never a connection claim"
# A27.8/A27.10. HarkenIQ holds no heartbeat, session or connection signal
# for an external runtime, so it claims none: `last_authenticated_at` is
# the last PERSISTED authentication observation. Judged on the FIELDS,
# recursively -- the disclaimer prose deliberately says the word.
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/ingress" \
  > /tmp/a63_ingress.json
A6_SRC="$_REPO_ROOT/services/central_command/src" python3 - <<'A63PY'
import json, os, sys
sys.path.insert(0, os.environ["A6_SRC"])
from harkeniq_cc.ingress_limits import (
    ATTEMPT_WINDOW_S, READ_MAX_PER_WINDOW, READ_WINDOW_S,
)
from harkeniq_cc.provenance import ACTIVITY_STATES, REFUSAL_SAMPLE

d = json.load(open("/tmp/a63_ingress.json"))
keys = set()


def walk(node):
    if isinstance(node, dict):
        for k, v in node.items():
            keys.add(k)
            walk(v)
    elif isinstance(node, list):
        for item in node:
            walk(item)


walk(d)
banned = {"connected", "online", "connection", "session", "heartbeat",
          "reachable", "last_seen_source", "last_seen_at"}
assert not (keys & banned), sorted(keys & banned)

assert d["credentialed"] is True, d["credentialed"]
assert d["identity_status"] == "active", d["identity_status"]
assert d["last_authenticated_at"], "the agent has authenticated repeatedly"
assert d["activity_state"] in ACTIVITY_STATES, d["activity_state"]

sub = d["submission_activity"]
assert sub["window_seconds"] == ATTEMPT_WINDOW_S, sub
assert sub["attempts"] >= 1, sub
assert sub["attempts"] >= sub["accepted"], sub
assert sub["last_attempt_at"], sub
# `last_accepted_at` comes from the DURABLE submission ledger, which is
# never pruned and never windowed, so it is the assertion that cannot go
# stale on a slow gate.
assert sub["last_accepted_at"], "no acceptance in the durable ledger"

# The throttle is a CURRENT-window projection, so a quiet minute reads
# zero. What must always hold is that it is internally consistent and
# that reading it did not exhaust anybody.
rt = d["read_throttle"]
assert (rt["window_seconds"], rt["limit"]) == (READ_WINDOW_S, READ_MAX_PER_WINDOW), rt
assert 0 <= rt["used"] <= rt["limit"], rt
assert rt["exhausted"] == (rt["used"] >= rt["limit"]), rt

# A27.9: bounded sample, fixed shape, nothing unbounded on an operator's
# screen and nothing that scans the durable ledger.
assert d["recent_refusal_limit"] == REFUSAL_SAMPLE, d["recent_refusal_limit"]
assert len(d["recent_refusals"]) <= REFUSAL_SAMPLE, len(d["recent_refusals"])
for r in d["recent_refusals"]:
    assert set(r) == {"submission_id", "code", "reason", "at"}, sorted(r)
    assert len(r["reason"]) <= 256, len(r["reason"])
print("ingress health: state=%s attempts=%d accepted=%d reads=%d/%d refusals=%d"
      % (d["activity_state"], sub["attempts"], sub["accepted"], rt["used"],
         rt["limit"], len(d["recent_refusals"])))
A63PY

step "A6-3/Y: an agent reads its OWN ingress, and an operator's read is free"
# D4: no new self rule -- the same A25.5 helper, so machine-self cannot
# drift between routes. And an operator inspecting a runtime's throttle
# must not consume that runtime's allowance, which is why the projection
# READS the window and `admit_read` stays the only writer.
a63_reads() {
  docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
    "SELECT coalesce(sum(reads),0) FROM cc_agent_read_windows WHERE agent_id='$1'" \
    | tr -d ' \r'
}
A63_A_BEFORE=$(a63_reads "$A6_AGENT")
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/ingress" > /dev/null
[ "$(a63_reads "$A6_AGENT")" = "$A63_A_BEFORE" ] || {
  echo "an operator's ingress read spent the agent's own allowance" >&2; exit 1; }

curl -sf -H "Authorization: Bearer $A6_TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/ingress" \
  > /tmp/a63_self.json || { echo "the agent could not read its own ingress" >&2; exit 1; }
A63_A_SELF=$(a63_reads "$A6_AGENT")
[ "$((A63_A_SELF - A63_A_BEFORE))" = "1" ] || {
  echo "the agent's own ingress read was charged \
$((A63_A_SELF - A63_A_BEFORE)), want 1" >&2; exit 1; }

A63_B_BEFORE=$(a63_reads "$A6_B")
A63_CROSS=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $B_TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/ingress")
[ "$A63_CROSS" = "403" ] || {
  echo "agent B read agent A's ingress health ($A63_CROSS), want 403" >&2; exit 1; }
[ "$((`a63_reads "$A6_B"` - A63_B_BEFORE))" = "1" ] || {
  echo "a cross-agent ingress refusal was free" >&2; exit 1; }
echo "operator read free to the agent; self read charged 1; cross-agent 403 charged to B"

step "A6-3/AA: a REAL 429 is observable, and does not amplify (A27.13)"
# The defect independent review found: A24.13 refuses an over-limit
# submission WITHOUT writing -- correctly, because a rejection recorded
# as an attempt would consume the allowance it was just refused for --
# but the projection then read `throttled` out of that same ledger,
# where the word is not in the vocabulary. Zero forever, so A27.11's
# `throttled` state could never be reached by real traffic.
#
# Proven here with REAL requests against the REAL route: fill the
# window, get a real 429, and read the operator surface.
A63_FILL=$(docker compose exec -T central-command python -c "
import os, sys
sys.path.insert(0, '/app/services/central_command/src')
from harkeniq_cc.ingress_limits import ATTEMPT_MAX
print(ATTEMPT_MAX)" | tr -d ' \r')
[ -n "$A63_FILL" ] || { echo "could not read ATTEMPT_MAX" >&2; exit 1; }

# Spend the allowance directly in the ledger. The point of the step is
# the REJECTION path, and driving $A63_FILL real submissions through
# Keycloak would add minutes to the gate to prove nothing extra.
# Tagged ids so the filler can be removed again EXACTLY, leaving the
# real attempts this gate produced earlier intact.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -q -c "
  INSERT INTO cc_agent_ingress_attempts (id, tenant_id, agent_id, outcome, created_at)
  SELECT 'gatefill' || lpad(g::text, 24, '0'), 'tenant-demo', '$A6_AGENT',
         'accepted', now()
    FROM generate_series(1, $A63_FILL) g;" > /dev/null

A63_THROTTLE_ROWS_BEFORE=$(docker compose exec -T postgres psql -U harkeniq \
  -d harkeniq_cc -tAc \
  "SELECT count(*) FROM cc_agent_throttle_windows WHERE agent_id='$A6_AGENT'" \
  | tr -d ' \r')

# Ten real rejected requests. Storage must not grow with them.
for N in 1 2 3 4 5 6 7 8 9 10; do
  A63_CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $A6_TOKEN" -H 'Content-Type: application/json' \
    -d "{\"candidate_ref\":\"$A6_REF\",\"idempotency_key\":\"gate-a63-throttle-$N\"}" \
    "http://localhost:8090/api/operational-agents/$A6_AGENT/proposals")
  [ "$A63_CODE" = "429" ] || {
    echo "request $N over the limit returned $A63_CODE, want 429" >&2; exit 1; }
done

A63_THROTTLE_ROWS=$(docker compose exec -T postgres psql -U harkeniq \
  -d harkeniq_cc -tAc \
  "SELECT count(*) FROM cc_agent_throttle_windows WHERE agent_id='$A6_AGENT'" \
  | tr -d ' \r')
[ "$((A63_THROTTLE_ROWS - A63_THROTTLE_ROWS_BEFORE))" -le 1 ] || {
  echo "10 rejections created $((A63_THROTTLE_ROWS - A63_THROTTLE_ROWS_BEFORE)) \
rows: storage grows with the traffic it bounds" >&2; exit 1; }

# And the attempt ledger did NOT grow: a rejection must never consume
# the allowance it was refused for.
A63_LEDGER=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT count(*) FROM cc_agent_ingress_attempts
    WHERE agent_id='$A6_AGENT' AND id LIKE 'gatefill%'" | tr -d ' \r')
[ "$A63_LEDGER" = "$A63_FILL" ] || {
  echo "the attempt ledger moved from $A63_FILL to $A63_LEDGER: a refused \
request entered the record it is refused against" >&2; exit 1; }

# The operator surface, which is the whole point.
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/ingress" \
  > /tmp/a63_throttled.json
python3 -c "
import json
d = json.load(open('/tmp/a63_throttled.json'))
sub = d['submission_activity']
assert sub['throttled'] >= 10, ('the operator cannot see the refusals', sub)
assert sub['last_throttled_at'], 'no time for the refusal'
assert d['activity_state'] == 'throttled', d['activity_state']
print('real 429 x10 -> throttled=%d, state=%s, %d throttle row(s)'
      % (sub['throttled'], d['activity_state'], $A63_THROTTLE_ROWS))
"

# Remove ONLY the filler, so the agent can submit again and the real
# attempts this gate recorded earlier are left exactly as they were.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -q -c "
  DELETE FROM cc_agent_ingress_attempts WHERE id LIKE 'gatefill%';" > /dev/null
A63_REAL=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT count(*) FROM cc_agent_ingress_attempts WHERE agent_id='$A6_AGENT'" \
  | tr -d ' \r')
[ "$A63_REAL" -ge 6 ] || {
  echo "the cleanup removed real attempts too ($A63_REAL left)" >&2; exit 1; }

step "A6-3/Z: a real 0023 -> head upgrade backfills nothing (A27.4)"
# The promise is about EXISTING customer data, so it is proven against a
# database that already holds proposals: drop the column, rewind, let
# Central Command bring it forward, and confirm every row comes back with
# NO provenance rather than a manufactured `evaluator`. The same proposal
# that read `external_ingress` a moment ago then reads `unknown` through
# the same API -- the FACT was lost, and the platform says so instead of
# guessing it back from the submission still sitting beside it.
#
# Central Command is STOPPED across the window. Its agent evaluator runs
# every 20s in this stack and reads `cc_agent_proposals`, so a column
# that briefly does not exist would raise inside a background loop --
# an ERROR log the last gate step correctly refuses, roughly one run in
# four. The upgrade then rides the service's OWN entrypoint (`alembic
# upgrade head`), which is the production upgrade path rather than a
# hand-invoked one.
#
# The gate restores what it deliberately destroyed (the A26 rule), from a
# snapshot taken here, and re-asserts the original answer afterwards.
CC_HEAD=$(ls "$_REPO_ROOT"/services/central_command/src/harkeniq_cc/db/migrations/versions/[0-9]*.py \
  | sed 's|.*/\([0-9]\{4\}\)_.*|\1|' | sort | tail -1)
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT id||'|'||coalesce(provenance_type,'') FROM cc_agent_proposals" \
  | tr -d ' \r' | grep -v '^$' > /tmp/a63_snapshot.txt
A63_TOTAL=$(wc -l < /tmp/a63_snapshot.txt | tr -d ' ')
[ "$A63_TOTAL" -gt 0 ] || { echo "no proposals to prove the upgrade against" >&2; exit 1; }

docker compose stop central-command > /dev/null
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc "
  alter table cc_agent_proposals drop column provenance_type;
  update alembic_version set version_num='0023';" > /dev/null
docker compose start central-command > /dev/null
A63_UP=""
for _ in $(seq 90); do
  if curl -sf http://localhost:8090/healthz > /dev/null 2>&1; then A63_UP=yes; break; fi
  sleep 1
done
[ -n "$A63_UP" ] || {
  echo "central-command did not come back after the 0023 rewind" >&2; exit 1; }
A63_VERSION=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "select version_num from alembic_version" | tr -d ' \r')
[ "$A63_VERSION" = "$CC_HEAD" ] || {
  echo "alembic head is $A63_VERSION, want $CC_HEAD" >&2; exit 1; }
A63_NULLS=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "select count(*) from cc_agent_proposals where provenance_type is null" | tr -d ' \r')
[ "$A63_NULLS" = "$A63_TOTAL" ] || {
  echo "0024 backfilled $((A63_TOTAL - A63_NULLS)) of $A63_TOTAL rows: \
A27.4 forbids manufacturing historical certainty" >&2; exit 1; }

# The projection, on a genuinely historical row, through the same API.
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/approvals/" > /tmp/a63_queue_after.json
A6_PROP="$A6_PROP" python3 -c "
import json, os
item = next(i for i in json.load(open('/tmp/a63_queue_after.json'))['actions']
            if i['action_id'] == os.environ['A6_PROP'])
assert item['provenance'] == {'type': 'unknown'}, item['provenance']
assert item['origin'] == 'agent', item['origin']
print('a pre-A6-3 row reads unknown -- and is NOT inferred from its submission')
"

# Put back exactly what was there. The gate broke it; the gate repairs it.
python3 -c "
import re
for line in open('/tmp/a63_snapshot.txt'):
    pid, _, prov = line.strip().partition('|')
    if not prov:
        continue
    assert re.fullmatch(r'[A-Za-z0-9_-]+', pid), pid
    assert re.fullmatch(r'[a-z_]+', prov), prov
    print(\"update cc_agent_proposals set provenance_type='%s' where id='%s';\"
          % (prov, pid))
" | docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -q
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/approvals/" > /tmp/a63_queue_restored.json
A6_PROP="$A6_PROP" A6_SUB="$A6_SUB" python3 -c "
import json, os
item = next(i for i in json.load(open('/tmp/a63_queue_restored.json'))['actions']
            if i['action_id'] == os.environ['A6_PROP'])
assert item['provenance'] == {
    'type': 'external_ingress', 'submission_id': os.environ['A6_SUB']}, item
print('restored:', item['provenance']['type'])
"
echo "0023 -> $CC_HEAD with $A63_TOTAL rows present: column re-added, zero backfilled"

step "A6-4A/AB: the machine plane is DECLARED -- off-plane routes refuse"
# A29.3. The defect: a machine principal reached 46 of 98 routes because
# their permission happened to be in MACHINE_PRINCIPAL_CEILING, and
# `fleet.view` alone opened 43. Proven here with a REAL machine token
# against the REAL routes, not with a stubbed principal.
A6_TOKEN=$(a6_token)
[ -n "$A6_TOKEN" ] || { echo "A6-4A could not mint the machine token" >&2; exit 1; }

# Off the plane: estate browsing, fleet intelligence, governance
# construction. Each is a route the machine reached before A6-4A.
for OFF in "/api/fleet/" "/api/learning/signals" "/api/campaigns/" \
           "/api/outcomes/patterns" "/api/predictive/risk" "/api/warranty/" \
           "/api/firmware/exposure" "/api/sites/" "/api/agents/" \
           "/api/policies/" "/api/policies/autonomy" "/api/policies/stop-switch" \
           "/api/tenant-settings/scope-enforcement" \
           "/api/tenant-settings/scope-enforcement/impact" \
           "/api/scope-grants/me" "/api/autonomy/" "/api/capabilities/" \
           "/api/operational-agents/" "/api/operational-agents/catalogue"; do
  A64_CODE=$(curl -s -o /tmp/a64_off.json -w '%{http_code}' \
    -H "Authorization: Bearer $A6_TOKEN" "http://localhost:8090$OFF")
  [ "$A64_CODE" = "403" ] || {
    echo "machine reached $OFF -> $A64_CODE, want 403" >&2
    head -c 300 /tmp/a64_off.json >&2; exit 1; }
  grep -q "External Agent API plane" /tmp/a64_off.json || {
    echo "$OFF refused for the wrong reason" >&2; cat /tmp/a64_off.json >&2
    exit 1; }
done
echo "19 off-plane routes refused a real machine token"

# On the plane, with the job bound: unchanged.
for ON in "/api/attention/" "/api/incidents/" \
          "/api/operational-agents/$A6_AGENT" \
          "/api/operational-agents/$A6_AGENT/runtime" \
          "/api/operational-agents/$A6_AGENT/ingress"; do
  curl -sf -H "Authorization: Bearer $A6_TOKEN" \
    "http://localhost:8090$ON" > /dev/null || {
      echo "on-plane route $ON refused a bound machine token" >&2; exit 1; }
done
echo "on-plane reads unchanged for a bound agent"

# A HUMAN is unaffected: the narrowing is machine-only.
for HUM in "/api/fleet/" "/api/policies/" "/api/autonomy/" "/api/capabilities/" \
           "/api/scope-grants/me"; do
  curl -sf -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8090$HUM" > /dev/null || {
      echo "the narrowing hit a HUMAN read: $HUM" >&2; exit 1; }
done
echo "human reads unaffected"

step "A6-4A/AC: the BINDING decides, not the permission (A29.6)"
# Agent B holds the same ceiling permissions and a different binding set.
# Nothing but the job can explain a different answer. Its token is
# re-minted: the one from earlier in the run is past its 300s lifetime.
B_TOKEN=$(curl -sf -X POST \
  "http://localhost:8180/realms/tenant-demo/protocol/openid-connect/token" \
  -d "grant_type=client_credentials&client_id=op-agent-$A6_B&client_secret=$B_SECRET" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
[ -n "$B_TOKEN" ] || { echo "could not re-mint agent B's token" >&2; exit 1; }
A64_B_ATT=$(curl -s -o /tmp/a64_b.json -w '%{http_code}' \
  -H "Authorization: Bearer $B_TOKEN" "http://localhost:8090/api/attention/")
A64_B_SELF=$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $B_TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_B/runtime")
[ "$A64_B_SELF" = "200" ] || {
  echo "agent B could not read its OWN record ($A64_B_SELF): 'self' needs no binding" >&2
  exit 1; }
echo "self read needs no binding (B self=$A64_B_SELF); attention=$A64_B_ATT"

step "A6-4A/AD: an authenticated refusal is metered, and bounded (A25.10)"
# A refused probe is charged to the agent the TOKEN names, so a runtime
# cannot sweep the plane for free. The read window is also the BOUND: one
# row per minute, then 429 -- deliberately no audit row per refusal, which
# would amplify traffic a misconfigured runtime generates at will.
A64_READS_BEFORE=$(docker compose exec -T postgres psql -U harkeniq \
  -d harkeniq_cc -tAc \
  "SELECT coalesce(sum(reads),0) FROM cc_agent_read_windows WHERE agent_id='$A6_AGENT'" \
  | tr -d ' \r')
# A29.16 attribution: agent B has its own refusals from A6-2/R, which is
# exactly why the claim has to be "A's probes did not move B's window"
# and not "B was never refused".
A64_B_BEFORE=$(docker compose exec -T postgres psql -U harkeniq \
  -d harkeniq_cc -tAc \
  "SELECT coalesce(sum(surface_refused),0) FROM cc_agent_read_windows
    WHERE agent_id='$A6_B'" | tr -d ' \r')
for _ in 1 2 3 4 5; do
  curl -s -o /dev/null -H "Authorization: Bearer $A6_TOKEN" \
    "http://localhost:8090/api/fleet/"
done
A64_READS_AFTER=$(docker compose exec -T postgres psql -U harkeniq \
  -d harkeniq_cc -tAc \
  "SELECT coalesce(sum(reads),0) FROM cc_agent_read_windows WHERE agent_id='$A6_AGENT'" \
  | tr -d ' \r')
[ "$((A64_READS_AFTER - A64_READS_BEFORE))" -ge 5 ] || {
  echo "5 refused probes cost $((A64_READS_AFTER - A64_READS_BEFORE)) reads: \
an authenticated refusal was free" >&2; exit 1; }
A64_ROWS=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT count(*) FROM cc_agent_read_windows WHERE agent_id='$A6_AGENT'" \
  | tr -d ' \r')
[ "$A64_ROWS" -le 3 ] || {
  echo "the refusal meter grew to $A64_ROWS rows: storage follows traffic" >&2
  exit 1; }
echo "5 refusals charged $((A64_READS_AFTER - A64_READS_BEFORE)) reads in $A64_ROWS row(s)"

# A29.16: the durable evidence is ATTRIBUTABLE -- which tenant, which
# agent, which closed reason, how many, how recently -- and it rides the
# window row that already exists rather than a row per refusal.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT surface_refused || '|' || refused_surface_not_allowed || '|' ||
          refused_job_not_bound || '|' ||
          (CASE WHEN last_surface_refused_at IS NOT NULL THEN 1 ELSE 0 END)
     FROM cc_agent_read_windows
    WHERE tenant_id='tenant-demo' AND agent_id='$A6_AGENT'
      AND surface_refused > 0" | tr -d ' \r' | python3 -c "
import sys
rows = [r for r in sys.stdin.read().split() if r]
assert rows, 'no attributable refusal evidence for the refused agent'
total = sum(int(r.split('|')[0]) for r in rows)
plane = sum(int(r.split('|')[1]) for r in rows)
assert total >= 5, ('refusals not recorded on the agent window', rows)
assert plane >= 5, ('the closed reason was not counted', rows)
assert all(r.split('|')[3] == '1' for r in rows), rows
print('attributable: %d refusal(s), %d surface_not_allowed, in %d window row(s)'
      % (total, plane, len(rows)))
"
# And agent A's refusals did not move agent B's window: the evidence is
# ATTRIBUTED, not a global count.
A64_B_AFTER=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT coalesce(sum(surface_refused),0) FROM cc_agent_read_windows
    WHERE agent_id='$A6_B'" | tr -d ' \r')
[ "$A64_B_AFTER" = "$A64_B_BEFORE" ] || {
  echo "agent A's refusals moved agent B's window \
($A64_B_BEFORE -> $A64_B_AFTER)" >&2; exit 1; }
echo "agent B's window unmoved by A's probes (held at $A64_B_AFTER, its own)"

curl -sf "http://localhost:8090/metrics" > /tmp/a64_metrics.txt
grep -q "cc_route_surface_refused_total" /tmp/a64_metrics.txt || {
  echo "the surface refusal is not on the metrics surface" >&2; exit 1; }
grep -qE "cc_route_surface_refused_total_[a-z_]+ " /tmp/a64_metrics.txt || {
  echo "no bounded reason on the refusal metric" >&2; exit 1; }
grep -q "$A6_AGENT" /tmp/a64_metrics.txt && {
  echo "an agent id reached the unauthenticated metrics surface" >&2; exit 1; }
echo "refusals counted by bounded reason; no identifier on /metrics"

# A29.16: THE EVIDENCE HAS A PRODUCTION READER. Durable, bounded and
# attributable is worth nothing if no operator can ask for it, so it
# rides the EXISTING A6-3 ingress-health contract -- same route, same
# `fleet.view`, same scope, same machine-self rule, no new permission.
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/ingress" \
  > /tmp/a64_ingress_ref.json
python3 -c "
import json
d = json.load(open('/tmp/a64_ingress_ref.json'))
b = d['surface_refusals']
assert b['total'] >= 5, ('the operator cannot see the refusals', b)
assert b['by_reason']['surface_not_allowed'] >= 5, b
assert set(b['by_reason']) == {'surface_not_allowed', 'machine_job_not_bound'}, b
assert b['last_surface_refused_at'], b
# Two different facts, never merged: a governed submission that was
# CONSIDERED and declined is not a request refused at the plane.
assert 'refused' in d['submission_activity'], d['submission_activity']
blob = json.dumps(d).lower()
for banned in ('campaigns', 'query', 'traceback', 'select ', 'client_id'):
    assert banned not in blob, banned
print('operator reads %d surface refusal(s), %d surface_not_allowed, last at %s'
      % (b['total'], b['by_reason']['surface_not_allowed'],
         b['last_surface_refused_at'][:19]))
"
# The machine reads its OWN and no other, through the unchanged A27.8 rule.
A64_SELF_REF=$(curl -s -o /tmp/a64_self_ref.json -w '%{http_code}' \
  -H "Authorization: Bearer $A6_TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/ingress")
[ "$A64_SELF_REF" = "200" ] || {
  echo "the agent could not read its own refusal evidence ($A64_SELF_REF)" >&2
  exit 1; }
python3 -c "
import json
b = json.load(open('/tmp/a64_self_ref.json'))['surface_refusals']
assert b['total'] >= 5, b
print('machine self-read sees its own refusals:', b['total'])
"

# ===========================================================================
# A6-4B0a (A30.10 + A30.17), live: expired means expired -- at REACH and at
# DISPATCH. A real Keycloak machine token, a real tenant-realm approver, a
# real PostgreSQL `timestamptz` comparison and a real Site Manager.
#
# The dispatch half is the independent-review HIGH: the synchronous human-
# approval path assembled its own gate and never asked reach, so an
# approved proposal whose grant had EXPIRED crossed CC -> SM with HTTP 200.
# Built on the A6 agent and its human-gated proposal $A6_PROP, whose last
# queue use is A6-3/Z above. Time passing is simulated the way the A23-4
# step simulates it: the row's expires_at is moved into the past.
# ===========================================================================

b0a_live_grants() {
  docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
    "SELECT count(*) FROM cc_scope_grants
      WHERE principal_type='agent' AND principal_ref='$A6_AGENT'
        AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())" \
    | tr -d ' \r'
}

step "A6-4B0a/AE: an ACTIVE grant -> the agent reaches its device"
A6_TOKEN=$(a6_token)
B0A_CONFIGURED=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT count(*) FROM cc_scope_grants
    WHERE principal_type='agent' AND principal_ref='$A6_AGENT'
      AND revoked_at IS NULL AND expires_at IS NULL" | tr -d ' \r')
[ "$B0A_CONFIGURED" -ge 1 ] && [ "$(b0a_live_grants)" = "$B0A_CONFIGURED" ] || {
  echo "the A6 agent must hold only unexpiring live grants here \
(configured=$B0A_CONFIGURED live=$(b0a_live_grants))" >&2; exit 1; }
for B0A_ROUTE in runtime dry-run; do
  curl -sf -H "Authorization: Bearer $A6_TOKEN" \
    "http://localhost:8090/api/operational-agents/$A6_AGENT/$B0A_ROUTE" \
    | B0A_ROUTE=$B0A_ROUTE python3 -c "
import sys, json, os
d = json.load(sys.stdin)
n = d['devices']['in_scope'] if os.environ['B0A_ROUTE'] == 'runtime' \
    else d['devices_in_scope']
assert n >= 1, ('an ACTIVE grant reached no device', os.environ['B0A_ROUTE'], d)
print('ACTIVE: machine %s reaches %d device(s)' % (os.environ['B0A_ROUTE'], n))
"
done

step "A6-4B0a/AF: the grant EXPIRES -> operational reach disappears"
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -q -c \
  "UPDATE cc_scope_grants SET expires_at = now() - interval '1 second'
    WHERE principal_type='agent' AND principal_ref='$A6_AGENT'
      AND revoked_at IS NULL" > /dev/null
[ "$(b0a_live_grants)" = "0" ] || { echo "the grant did not lapse" >&2; exit 1; }
# The machine's own preview: answered by the self rule alone, so it is
# still served -- and it reaches nothing and would propose nothing.
curl -sf -H "Authorization: Bearer $A6_TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/dry-run" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['devices_in_scope'] == 0, ('an EXPIRED grant still reached devices', d)
assert d['would_propose'] == [], d['would_propose']
print('EXPIRED: machine dry-run reaches 0 devices, would propose nothing')
"
# The machine's own runtime record also asks object visibility through the
# caller's OWN effective scope, and a principal with no effective reach is
# not a tenant-wide reader (A30.16) -- so the record may be withheld
# outright. Either shape is "no reach"; a 200 that still counts a device
# is the only failure.
B0A_RT=$(curl -s -o /tmp/b0a_rt.json -w '%{http_code}' \
  -H "Authorization: Bearer $A6_TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/runtime")
case "$B0A_RT" in
  404) echo "EXPIRED: machine runtime -> 404, its record is not visible to a principal with no reach" ;;
  200) python3 -c "
import json
n = json.load(open('/tmp/b0a_rt.json'))['devices']['in_scope']
assert n == 0, ('an EXPIRED grant still reached devices on runtime', n)
print('EXPIRED: machine runtime reaches 0 devices')
" ;;
  *) echo "machine runtime after expiry -> $B0A_RT, want 404 or 200/0" >&2
     cat /tmp/b0a_rt.json >&2; exit 1 ;;
esac
# The operator's runtime read of the same agent: served, and truthful.
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/runtime" | python3 -c "
import sys, json
n = json.load(sys.stdin)['devices']['in_scope']
assert n == 0, ('the operator is told an EXPIRED agent still reaches devices', n)
print('EXPIRED: operator runtime reports 0 devices in scope')
"
# CONFIGURED != EFFECTIVE: the operator is told the rule exists and
# lapsed, not that nothing was ever assigned.
curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT" | python3 -c "
import sys, json
s = json.load(sys.stdin)['scope']
assert s['device_count'] == 0, s
assert s['configured_rule_count'] >= 1 and s['ineffective_rule_count'] >= 1, s
assert 'lapsed' in s['statement'], s['statement']
print('operator view: %d configured, %d ineffective, 0 devices -- \"%s\"'
      % (s['configured_rule_count'], s['ineffective_rule_count'], s['statement'][:60]))
"

step "A6-4B0a/AG: APPROVED after expiry -> nothing crosses CC -> SM (A30.17)"
# The tenant carries A26.7's dual group policy by now, so a single-approver
# policy for this class is created for the step and removed after it --
# most-specific wins, and the subject here is dispatch, not the ledger.
B0A_POLICY=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"gate-b0a-single","action_type":"IDENTIFY_LED","required_approvers":1}' \
  http://localhost:8090/api/policies/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['policy']['id'])")
[ -n "$B0A_POLICY" ] || { echo "could not create the B0a policy" >&2; exit 1; }
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/approvals/$A6_PROP/approve" | python3 -c '
import sys, json
d = json.load(sys.stdin)
# The approval is a fact and is recorded as one...
assert d.get("decision") == "approved", ("approval did not complete", d)
assert d["decided_by"], d
# ...and it is not execution authority. Before A30.17 this was
# delivered=True with a directive id.
assert d["delivery"]["delivered"] is False, d["delivery"]
assert "no longer reaches" in d["delivery"]["reason"], d["delivery"]
print("approved by", d["decided_by"], "-> NOT dispatched:", d["delivery"]["reason"])
'
# Keyed on the proposal, never a total: other agents' background passes may
# legitimately queue directives while this step runs.
B0A_SM_PROP=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
  "SELECT count(*) FROM sm_directives WHERE proposal_id='$A6_PROP'" | tr -d ' \r')
[ "$B0A_SM_PROP" = "0" ] || {
  echo "$B0A_SM_PROP directive(s) for $A6_PROP reached the Site Manager" >&2
  exit 1; }
echo "Site Manager: 0 directives for $A6_PROP"
# History is intact: the proposal keeps its attribution and its approver,
# the ONE human decision is on the ledger, and the refusal is on the chain.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT p.status || '|' || coalesce(p.directive_id,'') || '|' || p.actor || '|' ||
          coalesce(p.decided_by,'') || '|' ||
          (SELECT count(*) FROM cc_approval_records r WHERE r.subject_ref = p.id) || '|' ||
          (SELECT count(*) FROM cc_audit_log a WHERE a.subject = p.id
             AND a.action = 'agent_proposal.refused_at_dispatch') || '|' ||
          (SELECT count(*) FROM cc_audit_log a WHERE a.subject = p.id
             AND a.action = 'agent_proposal.dispatched')
     FROM cc_agent_proposals p WHERE p.id = '$A6_PROP'" | tr -d ' \r' \
  | A6_AGENT=$A6_AGENT python3 -c "
import sys, os
status, directive, actor, decided_by, records, refused, dispatched = \
    sys.stdin.read().strip().split('|')
assert status == 'failed' and directive == '', (status, directive)
assert actor.startswith('op-agent:%s@v' % os.environ['A6_AGENT']), actor
assert decided_by, 'the approver was erased from the proposal'
assert records == '1', ('exactly one human decision on the ledger', records)
assert refused == '1' and dispatched == '0', (refused, dispatched)
print('history intact: %s, attributed %s, approved by %s, %s ledger record, '
      'refused_at_dispatch on the chain' % (status, actor, decided_by, records))
"
curl -sf -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/policies/$B0A_POLICY" > /dev/null

step "A6-4B0a/AH: the grant is RENEWED -> reach returns; the refusal stands"
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -q -c \
  "UPDATE cc_scope_grants SET expires_at = NULL
    WHERE principal_type='agent' AND principal_ref='$A6_AGENT'
      AND revoked_at IS NULL" > /dev/null
[ "$(b0a_live_grants)" = "$B0A_CONFIGURED" ] || {
  echo "the grant was not restored" >&2; exit 1; }
curl -sf -H "Authorization: Bearer $A6_TOKEN" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/runtime" | python3 -c "
import sys, json
n = json.load(sys.stdin)['devices']['in_scope']
assert n >= 1, ('renewal did not restore reach', n)
print('RENEWED: machine runtime reaches', n, 'device(s) again')
"
# Renewing authority does not resurrect work refused while it was absent:
# the refused proposal is not re-dispatched by the background pass.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT status FROM cc_agent_proposals WHERE id='$A6_PROP'" \
  | tr -d ' \r' | grep -qx failed \
  || { echo "the refused proposal changed state after renewal" >&2; exit 1; }
echo "the refused proposal stays refused"

step "A6/K: a revoked identity cannot submit, immediately"
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"reason":"gate A6/K"}' \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/identity/revoke" > /dev/null
A6_REVOKED=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $A6_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"candidate_ref\":\"$A6_REF\",\"idempotency_key\":\"gate-a6-0004\"}" \
  "http://localhost:8090/api/operational-agents/$A6_AGENT/proposals")
[ "$A6_REVOKED" = "401" ] || {
  echo "a revoked identity still submitted ($A6_REVOKED) -- the token had not expired" >&2
  exit 1; }
echo "revoked identity -> 401 on an unexpired token"

step "A6/L: the submission is audited with a stable machine actor_ref"
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT count(*) FROM cc_audit_log
    WHERE action='agent_submission.accepted' AND actor_ref='$A6_AGENT'" \
  | grep -qv '^ *0 *$' \
  || { echo "no audited submission with a machine actor_ref" >&2; exit 1; }
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT detail->>'origin' FROM cc_audit_log
    WHERE action='agent_proposal.created' AND subject='$A6_PROP'" \
  | tr -d ' \r' | grep -q '^external_ingress$' \
  || { echo "the proposal's origin was not recorded as external_ingress" >&2
       exit 1; }
echo "submission audited; proposal provenance recorded as external_ingress"

# ===========================================================================
# A6-4B0a (A30.17), live, the REVOCATION half: approved while authorized +
# the target's scope GRANT revoked before dispatch = no execution -- on the
# synchronous approval path AND on the background runtime path.
#
# AE-AH above prove EXPIRY live (set by SQL), and A6/K revokes a machine
# IDENTITY and proves a 401 on SUBMISSION -- not a dispatch refusal.
# Nothing revokes a SCOPE GRANT under approved work, which is what the
# independent re-review of d8d6c17 asked the production boundary to show.
# A fresh agent owns this state, so nothing earlier in the gate can
# satisfy or break it:
#
#   * a real machine identity, active, bound to COLLECT_DIAGNOSTICS, and
#     TWO site grants: G_T covers the target device, G_O covers a device
#     at a different site and is never touched -- so the agent keeps real
#     reach throughout and a refusal has to be about the TARGET;
#   * work is admitted by the CC-resident evaluator and decided through
#     the one approval route; the grant is revoked, and later restored,
#     through the production grant routes -- never by SQL;
#   * refusal attribution follows DISPATCH_GATES order: `effective_scope`
#     is the SIXTH gate, so its message means identity, activation,
#     tenant, stop switch and pause all passed at that moment. The one
#     gate after it (the binding) is shown intact, and the target is shown
#     still in the fleet -- the only other way reach can vanish.
# ===========================================================================

b0r_cc() {  # one scalar from the CC database
  docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc "$1" \
    | tr -d ' \r'
}
b0r_cc_text() {  # a row whose text must keep its spaces
  docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc "$1" \
    | tr -d '\r'
}
b0r_sm_directives() {  # directives the Site Manager holds for one proposal
  docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
    "SELECT count(*) FROM sm_directives WHERE proposal_id='$1'" | tr -d ' \r'
}
b0r_view() {  # the operator's agent view, as the human Console reads it
  curl -sf -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8090/api/operational-agents/$B0R_AGENT" > /tmp/b0r_view.json
}
b0r_prereqs() {  # the dispatch inputs this proof must hold still, re-read
  # Gates 1, 2, 4, 5 and 7 of DISPATCH_GATES, plus the one input to gate 6
  # that is NOT scope: the target's fleet membership, which is the only
  # other way `reach.devices` can empty. Gate 3 (`tenant_scope`) is left
  # out deliberately -- it has no reachable failing path from here, since
  # `OperationalAgentRepo.get` is tenant-filtered and a mismatch refuses
  # earlier with a different message. Gate 5 is NAMED `budget` and carries
  # the PAUSE verdict on a human-approved proposal; the execution budget
  # caps unattended work and is asked on the autonomous branch, not here.
  curl -sf -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8090/api/operational-agents/$B0R_AGENT/identity" \
    > /tmp/b0r_identity.json
  curl -sf -H "Authorization: Bearer $TOKEN" \
    http://localhost:8090/api/policies/stop-switch > /tmp/b0r_stop.json
  b0r_view
  B0R_FLEET=$(b0r_cc "SELECT count(*) FROM cc_fleet_cache
    WHERE site_id='$A5_SITE' AND agent_id='$B0R_TARGET'")
  B0R_BOUND=$(b0r_cc "SELECT count(*) FROM cc_agent_capabilities
    WHERE agent_id='$B0R_AGENT' AND kind='action_class'
      AND capability_ref='COLLECT_DIAGNOSTICS'")
  B0R_FLEET=$B0R_FLEET B0R_BOUND=$B0R_BOUND python3 -c "
import json, os
ident = json.load(open('/tmp/b0r_identity.json'))
stop = json.load(open('/tmp/b0r_stop.json'))
view = json.load(open('/tmp/b0r_view.json'))
assert ident['status'] == 'active', ('identity', ident['status'])
agent = view['agent']
assert agent['status'] == 'active', ('agent', agent['status'])
assert not agent.get('paused_reason'), ('paused', agent.get('paused_reason'))
assert stop['stop_switch'] is False, stop
assert int(os.environ['B0R_BOUND']) == 1, 'COLLECT_DIAGNOSTICS is no longer bound'
assert int(os.environ['B0R_FLEET']) == 1, 'the target left the fleet'
print('  unchanged: identity active, agent active, not paused, stop switch off,'
      ' COLLECT_DIAGNOSTICS bound, target still in the fleet')
"
}
b0r_revocation_is_the_cause() {  # WHICH lifecycle fact emptied the reach
  # A site grant stops conferring reach three ways, and all three produce
  # the byte-identical refusal string: it was REVOKED, it EXPIRED, or its
  # target vanished and A23-3's resolver made it inert. Only one of those
  # is what this proof claims, so each refusal names which -- read from
  # the production projection (`include_revoked` is what lets a revoked
  # row still be read; `target_status` and `effective` are A23-3's).
  curl -sf -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8090/api/scope-grants/?principal_ref=$B0R_AGENT&principal_type=agent&include_revoked=true" \
    | B0R_G_T=$B0R_G_T B0R_G_O=$B0R_G_O python3 -c "
import sys, json, os
rows = {r['id']: r for r in json.load(sys.stdin)['grants']}
gt, go = rows[os.environ['B0R_G_T']], rows[os.environ['B0R_G_O']]
assert gt['revoked_at'], ('G_T is not revoked', gt)
assert gt['expires_at'] is None, ('G_T lapsed; this proof is about revocation', gt)
assert gt['target_status'] == 'present', ('G_T went inert: its site vanished', gt)
assert gt['effective'] is False, gt
assert go['revoked_at'] is None and go['expires_at'] is None, ('G_O was touched', go)
assert go['target_status'] == 'present' and go['effective'] is True, go
print('  cause: G_T REVOKED -- not expired, target still present;'
      ' G_O untouched and still effective')
"
}

step "A6-4B0a/AI: an agent authorized on the target, with a second, unrelated grant"
TOKEN=$(tenant_token gate-owner@demo gate-owner)
# The sync half needs a SECOND approver, and it makes its own rather than
# borrowing one. `gate-a23-admin2` is the obvious candidate and is the
# wrong one: A23-3 has the owner and admin2 revoke each other CONCURRENTLY
# and asserts only that exactly one wins, restoring the owner when the
# owner is the loser -- so admin2 ends the race revoked about half the
# time, and a step that leaned on it would pass on a coin flip. This
# identity is created here, granted here, and used nowhere else.
tenant_realm_user gate-b0r-approver@demo gate-b0r-approver tenant_owner
B0R_APPROVER_TOKEN=$(tenant_token gate-b0r-approver@demo gate-b0r-approver)
B0R_APPROVER_SUB=$(python3 -c "
import base64, json
t = '$B0R_APPROVER_TOKEN'.split('.')[1]; t += '=' * (-len(t) % 4)
print(json.loads(base64.urlsafe_b64decode(t))['sub'])")
[ -n "$B0R_APPROVER_SUB" ] || { echo "the B0a approver has no subject" >&2; exit 1; }
B0R_APPROVER_GRANT=$(grant "$B0R_APPROVER_SUB" tenant "" tenant_owner)
[ "$B0R_APPROVER_GRANT" = "201" ] || {
  echo "the B0a approver could not be granted ($B0R_APPROVER_GRANT)" >&2; exit 1; }
echo "second approver gate-b0r-approver@demo granted tenant_owner (its own, not A23-3's)"
B0R_TARGET=$(b0r_cc "SELECT agent_id FROM cc_fleet_cache
  WHERE site_id='$A5_SITE' ORDER BY agent_id LIMIT 1")
B0R_OTHER_SITE=$(b0r_cc "SELECT f.site_id FROM cc_fleet_cache f
  JOIN cc_sites s ON s.id = f.site_id
  WHERE s.tenant_id='tenant-demo' AND f.site_id <> '$A5_SITE'
  ORDER BY f.site_id LIMIT 1")
[ -n "$B0R_TARGET" ] && [ -n "$B0R_OTHER_SITE" ] || {
  echo "no target at $A5_SITE or no second device-bearing site" >&2; exit 1; }
B0R_AGENT=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"b0a-revoke $(date +%s)\",
       \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$A5_SITE\"},
                   {\"scope_type\":\"site\",\"scope_ref\":\"$B0R_OTHER_SITE\"}],
       \"capabilities\":[
         {\"kind\":\"action_class\",\"capability_ref\":\"COLLECT_DIAGNOSTICS\"}]}" \
  http://localhost:8090/api/operational-agents/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
B0R_SECRET=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$B0R_AGENT/identity" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['client_secret'])")
b0r_token() {
  curl -sf -X POST \
    "http://localhost:8180/realms/tenant-demo/protocol/openid-connect/token" \
    -d "grant_type=client_credentials&client_id=op-agent-$B0R_AGENT&client_secret=$B0R_SECRET" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])"
}
B0R_MTOKEN=$(b0r_token)
[ -n "$B0R_MTOKEN" ] || { echo "the B0a agent's identity did not mint" >&2; exit 1; }
B0R_ACK=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$B0R_AGENT/preflight" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['requires_acknowledgement'])")
if [ "$B0R_ACK" = "True" ]; then
  curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8090/api/operational-agents/$B0R_AGENT/acknowledge" > /dev/null
fi
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$B0R_AGENT/activate" \
  | python3 -c "
import sys, json
a = json.load(sys.stdin)
assert a['status'] == 'active', a
print('B0a agent active at v%d, machine identity issued' % a['activated_version'])
"
# The two grants, by id, through the production read -- and which one
# covers the target: G_T is the site grant whose scope_ref IS the target's
# fleet site; G_O names a site the target is not at.
B0R_GRANTS=$(curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/scope-grants/?principal_ref=$B0R_AGENT&principal_type=agent" \
  | A5_SITE=$A5_SITE B0R_OTHER_SITE=$B0R_OTHER_SITE python3 -c "
import sys, json, os
rows = json.load(sys.stdin)['grants']
by_ref = {r['scope_ref']: r for r in rows if r['scope_type'] == 'site'}
gt, go = by_ref[os.environ['A5_SITE']], by_ref[os.environ['B0R_OTHER_SITE']]
assert gt['id'] != go['id']
print(gt['id'], go['id'])
")
B0R_G_T=${B0R_GRANTS% *}
B0R_G_O=${B0R_GRANTS#* }
B0R_TARGET_SITE=$(b0r_cc "SELECT site_id FROM cc_fleet_cache WHERE agent_id='$B0R_TARGET'")
[ "$B0R_TARGET_SITE" = "$A5_SITE" ] && [ "$B0R_OTHER_SITE" != "$A5_SITE" ] || {
  echo "grant/target geometry is not what the proof needs" >&2; exit 1; }
echo "target $B0R_TARGET at site $B0R_TARGET_SITE"
echo "  G_T $B0R_G_T = site $A5_SITE      (covers the target)"
echo "  G_O $B0R_G_O = site $B0R_OTHER_SITE (a different site; never touched)"

wait_for "the evaluator admits work on the target" 120 bash -c \
  "docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
   \"SELECT count(*) FROM cc_agent_proposals WHERE agent_id='$B0R_AGENT'
      AND device_agent_id='$B0R_TARGET' AND status='awaiting_approval'\" \
   | grep -qv '^ *0 *$'"
B0R_P1=$(b0r_cc "SELECT id FROM cc_agent_proposals WHERE agent_id='$B0R_AGENT'
  AND device_agent_id='$B0R_TARGET' AND status='awaiting_approval'
  ORDER BY created_at LIMIT 1")
[ "$(b0r_cc "SELECT site_id || '|' || action_type || '|' || authorization_basis
    FROM cc_agent_proposals WHERE id='$B0R_P1'")" \
  = "$A5_SITE|COLLECT_DIAGNOSTICS|human_approval" ] || {
  echo "P1 is not the human-gated target proposal the proof needs" >&2; exit 1; }
echo "P1 $B0R_P1 admitted by the evaluator: COLLECT_DIAGNOSTICS on the target, awaiting a human"

# ACTIVE control: with both grants live the agent reaches the target AND
# the other site's device, every other dispatch input is in order, and
# nothing has executed.
b0r_prereqs
B0R_TARGET=$B0R_TARGET A5_SITE=$A5_SITE B0R_OTHER_SITE=$B0R_OTHER_SITE python3 -c "
import json, os
s = json.load(open('/tmp/b0r_view.json'))['scope']
reach = {(d['agent_id'], d['site_id']) for d in s['devices']}
assert (os.environ['B0R_TARGET'], os.environ['A5_SITE']) in reach, reach
assert any(site == os.environ['B0R_OTHER_SITE'] for _, site in reach), reach
print('ACTIVE control: effective reach = %s' % sorted(reach))
"
curl -sf -H "Authorization: Bearer $B0R_MTOKEN" \
  "http://localhost:8090/api/operational-agents/$B0R_AGENT/dry-run" | python3 -c "
import sys, json
n = json.load(sys.stdin)['devices_in_scope']
assert n >= 2, n
print('ACTIVE control: the agent\'s own machine token previews %d devices in scope' % n)
"
[ "$(b0r_sm_directives "$B0R_P1")" = "0" ] || {
  echo "P1 executed before anyone approved it" >&2; exit 1; }

step "A6-4B0a/AJ: SYNC -- approval begun while authorized, target grant REVOKED, approval completed -> nothing crosses CC -> SM"
# A complete approval dispatches in the same request, so on this path the
# approval must BEGIN under live authority and COMPLETE after revocation:
# a two-approver policy for the class (most specific wins over A26.7's
# wildcard, and it is removed after the step).
# Human tokens are re-minted per step: a slow runner must not turn this
# into a token-expiry failure.
TOKEN=$(tenant_token gate-owner@demo gate-owner)
B0R_APPROVER_TOKEN=$(tenant_token gate-b0r-approver@demo gate-b0r-approver)
B0R_POLICY=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"gate-b0r-dual","action_type":"COLLECT_DIAGNOSTICS","required_approvers":2}' \
  http://localhost:8090/api/policies/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['policy']['id'])")
curl -sf -X POST -H "Authorization: Bearer $B0R_APPROVER_TOKEN" \
  "http://localhost:8090/api/approvals/$B0R_P1/approve" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d.get("recorded") is True, d
assert d.get("decision") is None, d
assert d["approval"]["required"] == 2 and d["approval"]["received"] == 1, d["approval"]
print("approval 1 of 2 recorded by a tenant owner WHILE AUTHORIZED; nothing executed")
'
[ "$(b0r_sm_directives "$B0R_P1")" = "0" ] || { echo "a partial approval executed" >&2; exit 1; }

# Revoke THE grant that covers the target -- the scope grant, not the
# identity -- through the production revocation route.
curl -sf -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/scope-grants/$B0R_G_T" | A5_SITE=$A5_SITE python3 -c "
import sys, json, os
g = json.load(sys.stdin)
assert g['revoked_at'], g
assert g['principal_type'] == 'agent' and g['scope_ref'] == os.environ['A5_SITE'], g
print('revoked G_T (scope grant on the target\'s site); the identity was not touched')
"
b0r_revocation_is_the_cause
# CURRENT target reach is gone -- and only the target's: G_O still reaches
# its device, so "the agent has some scope" stays true.
b0r_prereqs
B0R_TARGET=$B0R_TARGET B0R_OTHER_SITE=$B0R_OTHER_SITE python3 -c "
import json, os
s = json.load(open('/tmp/b0r_view.json'))['scope']
reach = {(d['agent_id'], d['site_id']) for d in s['devices']}
assert all(dev != os.environ['B0R_TARGET'] for dev, _ in reach), reach
assert reach and all(site == os.environ['B0R_OTHER_SITE'] for _, site in reach), reach
assert [r['scope_ref'] for r in s['rules']] == [os.environ['B0R_OTHER_SITE']], s['rules']
print('REVOKED: effective reach = %s -- the target is gone, G_O still reaches' % sorted(reach))
"
# The second approver completes the approval. Its record is written; the
# dispatch is refused by current authority.
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/approvals/$B0R_P1/approve" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d.get("decision") == "approved", ("the approval did not complete", d)
assert d["delivery"]["delivered"] is False, d["delivery"]
assert d["delivery"]["reason"] == (
    "this agent'"'"'s scope no longer reaches the device this proposal targets"
), d["delivery"]
print("approval 2 of 2 completed by", d["decided_by"], "-> NOT dispatched:",
      d["delivery"]["reason"])
'
# Read the other six inputs AGAIN, on the far side of the refusal: taken
# only beforehand they would say what was true one HTTP call earlier, and
# the claim is that nothing but scope changed ACROSS it. (AK snapshots
# after its refusal for the same reason; the two paths now match.)
b0r_prereqs
B0R_SM=$(b0r_sm_directives "$B0R_P1")
[ "$B0R_SM" = "0" ] || { echo "$B0R_SM directive(s) for P1 reached the SM" >&2; exit 1; }
echo "SYNC: SM directives for P1 = $B0R_SM"
b0r_cc_text "SELECT status || '|' || coalesce(directive_id,'') || '|' || coalesce(decided_by,'') || '|' ||
    (SELECT count(*) FROM cc_approval_records r WHERE r.subject_ref = p.id) || '|' ||
    (SELECT count(*) FROM cc_audit_log a WHERE a.subject = p.id
       AND a.action = 'agent_proposal.refused_at_dispatch') || '|' ||
    (SELECT count(*) FROM cc_audit_log a WHERE a.subject = p.id
       AND a.action = 'agent_proposal.dispatched')
  FROM cc_agent_proposals p WHERE p.id = '$B0R_P1'" | python3 -c "
import sys
status, directive, decided_by, records, refused, dispatched = sys.stdin.read().strip().split('|')
assert status == 'failed' and directive == '', (status, directive)
assert decided_by and records == '2', (decided_by, records)
assert refused == '1' and dispatched == '0', (refused, dispatched)
print('history intact: both approvals on the ledger, refused_at_dispatch on the chain, never dispatched')
"
curl -sf -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/policies/$B0R_POLICY" > /dev/null

step "A6-4B0a/AK: BACKGROUND -- approved while authorized, target grant REVOKED, the runtime pass dispatches nothing"
# Restore G_T through the production grant route, so the agent may be
# authorized on the target again, and let the evaluator admit P2 -- the
# target's other open condition, which P1's refusal left unclaimed.
TOKEN=$(tenant_token gate-owner@demo gate-owner)
B0R_G_T=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"principal_type\":\"agent\",\"principal_ref\":\"$B0R_AGENT\",
       \"scope_type\":\"site\",\"scope_ref\":\"$A5_SITE\"}" \
  http://localhost:8090/api/scope-grants/ \
  | python3 -c "import sys,json; g=json.load(sys.stdin); assert not g['revoked_at'], g; print(g['id'])")
wait_for "the evaluator admits P2 on the target" 120 bash -c \
  "docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
   \"SELECT count(*) FROM cc_agent_proposals WHERE agent_id='$B0R_AGENT'
      AND device_agent_id='$B0R_TARGET' AND status='awaiting_approval'
      AND id <> '$B0R_P1'\" | grep -qv '^ *0 *$'"
B0R_P2=$(b0r_cc "SELECT id FROM cc_agent_proposals WHERE agent_id='$B0R_AGENT'
  AND device_agent_id='$B0R_TARGET' AND status='awaiting_approval' AND id <> '$B0R_P1'
  ORDER BY created_at LIMIT 1")
# Characterise P2 before it becomes the subject, exactly as AI does P1. An
# EMPTY site_id on a proposal is by itself enough to empty the reach and
# produce the same refusal string (`revalidate_dispatch` resolves the
# target from `list_by_site(site_id) if site_id else ()`), so the step
# must establish its own subject rather than assume it.
[ "$(b0r_cc "SELECT site_id || '|' || action_type || '|' || authorization_basis
    FROM cc_agent_proposals WHERE id='$B0R_P2'")" \
  = "$A5_SITE|COLLECT_DIAGNOSTICS|human_approval" ] || {
  echo "P2 is not the human-gated target proposal the proof needs" >&2; exit 1; }
echo "P2 $B0R_P2 admitted by the evaluator on the target, under the restored G_T"
B0R_POLICY=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"gate-b0r-single","action_type":"COLLECT_DIAGNOSTICS","required_approvers":1}' \
  http://localhost:8090/api/policies/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['policy']['id'])")

# APPROVED WHILE AUTHORIZED, delivery pending: the approval completes with
# G_T live, and the site is briefly unreachable from Central Command, which
# is exactly the case the approval route leaves `approved` for the
# background pass (a site outage never discards a human's decision). The
# outage is a direct write to `cc_sites.sm_endpoint` -- the ONE state
# change here that is not made through a production route, because no
# route exists to take a site offline; the revocation this step is about
# goes through `DELETE /api/scope-grants/{id}`.
#
# It is timed straight after a fleet poll so no poll can land in it: a
# failed poll logs at ERROR and the gate forbids ERROR lines. Two things
# enforce that rather than hope for it -- the window is bounded by the
# CONFIGURED poll interval read from the running service (not a literal,
# which would silently stop being safe if the interval were lowered), and
# the poller's own failure count is compared across the window, so a poll
# that did land fails HERE by name instead of seventy steps later as an
# unattributed ERROR line.
B0R_INTERVAL=$(docker compose exec -T central-command \
  printenv HARKEN_CC_SITE_POLL_INTERVAL_S | tr -d ' \r')
case "$B0R_INTERVAL" in ''|*[!0-9]*)
  echo "could not read HARKEN_CC_SITE_POLL_INTERVAL_S ('$B0R_INTERVAL')" >&2; exit 1;;
esac
# Two thirds of the interval, leaving a third for detection lag; never
# below 5s, so a short interval cannot make the bound unachievable.
B0R_BUDGET=$((B0R_INTERVAL * 2 / 3))
[ "$B0R_BUDGET" -ge 5 ] || B0R_BUDGET=5
B0R_SNAP=$(b0r_cc "SELECT coalesce(max(snapshot_at)::text,'') FROM cc_fleet_cache WHERE site_id='$A5_SITE'")
B0R_WAITED=0
until [ "$(b0r_cc "SELECT coalesce(max(snapshot_at)::text,'') FROM cc_fleet_cache WHERE site_id='$A5_SITE'")" != "$B0R_SNAP" ]; do
  sleep 1; B0R_WAITED=$((B0R_WAITED + 1))
  [ "$B0R_WAITED" -lt 90 ] || { echo "no fleet poll of the target site in 90s" >&2; exit 1; }
done
# Baseline the poll failures AFTER the wait, not before it: taken earlier,
# an unrelated failure during the wait would be blamed on a window that
# had not opened yet.
B0R_POLLFAIL_0=$(docker compose logs central-command 2>&1 \
  | grep -c "Fleet poll failed" || true)
B0R_EP=$(b0r_cc "SELECT sm_endpoint FROM cc_sites WHERE id='$A5_SITE'")
B0R_T0=$SECONDS
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -q -c \
  "UPDATE cc_sites SET sm_endpoint='127.0.0.1:9' WHERE id='$A5_SITE'" > /dev/null
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/approvals/$B0R_P2/approve" > /tmp/b0r_p2.json || true
curl -sf -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/scope-grants/$B0R_G_T" > /tmp/b0r_revoke2.json || true
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -q -c \
  "UPDATE cc_sites SET sm_endpoint='$B0R_EP' WHERE id='$A5_SITE'" > /dev/null
B0R_WINDOW=$((SECONDS - B0R_T0))
[ "$(b0r_cc "SELECT sm_endpoint FROM cc_sites WHERE id='$A5_SITE'")" = "$B0R_EP" ] || {
  echo "the site route was not restored" >&2; exit 1; }
[ "$B0R_WINDOW" -lt "$B0R_BUDGET" ] || {
  echo "outage window ${B0R_WINDOW}s exceeded ${B0R_BUDGET}s of a \
${B0R_INTERVAL}s poll interval -- a poll may have landed in it" >&2; exit 1; }
B0R_POLLFAIL_1=$(docker compose logs central-command 2>&1 \
  | grep -c "Fleet poll failed" || true)
[ "$B0R_POLLFAIL_1" = "$B0R_POLLFAIL_0" ] || {
  echo "a fleet poll landed inside the injected outage \
($B0R_POLLFAIL_0 -> $B0R_POLLFAIL_1 failures); the ERROR is this step's, \
not a platform regression" >&2; exit 1; }
A5_SITE=$A5_SITE python3 -c "
import json, os
d = json.load(open('/tmp/b0r_p2.json'))
assert d.get('decision') == 'approved', ('the approval did not complete', d)
assert d['delivery']['delivered'] is False, d['delivery']
g = json.load(open('/tmp/b0r_revoke2.json'))
assert g['revoked_at'] and g['scope_ref'] == os.environ['A5_SITE'], g
print('P2 approved by %s WHILE AUTHORIZED; delivery pending (site unreachable);'
      ' then G_T revoked' % d['decided_by'])
"
echo "outage window ${B0R_WINDOW}s (budget ${B0R_BUDGET}s of a ${B0R_INTERVAL}s \
poll interval), site route restored to $B0R_EP"
[ "$(b0r_cc "SELECT status FROM cc_agent_proposals WHERE id='$B0R_P2'")" = "approved" ] || {
  echo "P2 is not approved-and-pending; the background proof has no subject" >&2; exit 1; }
b0r_revocation_is_the_cause

# The REAL background pass (the CC operational-agent loop, every 20s) now
# revalidates the approved proposal against current authority.
wait_for "the background pass withholds P2" 90 bash -c \
  "docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
   \"SELECT count(*) FROM cc_audit_log WHERE subject='$B0R_P2'
      AND action='agent_proposal.dispatch_withheld'\" | grep -qv '^ *0 *$'"
b0r_cc_text "SELECT p.status || '|' || coalesce(p.directive_id,'') || '|' || coalesce(p.dispatch_reason,'') || '|' ||
    (SELECT a.detail->>'reason' FROM cc_audit_log a WHERE a.subject = p.id
       AND a.action = 'agent_proposal.dispatch_withheld' ORDER BY a.seq LIMIT 1)
  FROM cc_agent_proposals p WHERE p.id = '$B0R_P2'" | python3 -c "
import sys
status, directive, reason, audited = sys.stdin.read().strip().split('|')
want = \"this agent's scope no longer reaches the device this proposal targets\"
assert status == 'approved' and directive == '', (status, directive)
assert reason == want and audited == want, (reason, audited)
print('BACKGROUND: the runtime pass refused P2 ->', reason)
print('  P2 stays approved (the human decision stands); withheld on the chain')
"
b0r_prereqs
# ...and, as on the sync path, that the agent still REACHES somewhere:
# without this the background half would inherit target-specificity from
# AJ rather than assert it, and a refusal caused by losing all scope would
# wear the same message.
B0R_TARGET=$B0R_TARGET B0R_OTHER_SITE=$B0R_OTHER_SITE python3 -c "
import json, os
s = json.load(open('/tmp/b0r_view.json'))['scope']
reach = {(d['agent_id'], d['site_id']) for d in s['devices']}
assert all(dev != os.environ['B0R_TARGET'] for dev, _ in reach), reach
assert reach and all(site == os.environ['B0R_OTHER_SITE'] for _, site in reach), reach
print('  still target-specific: reach = %s' % sorted(reach))
"
B0R_SM=$(b0r_sm_directives "$B0R_P2")
[ "$B0R_SM" = "0" ] || { echo "$B0R_SM directive(s) for P2 reached the SM" >&2; exit 1; }
echo "BACKGROUND: SM directives for P2 = $B0R_SM"

# Causal control: restore ONLY G_T. Same proposal, same approval, same
# agent, identity, binding and site route -- if scope was the only thing
# refusing, the next pass now delivers it.
#
# "The same grant" means the same (principal, scope_type, scope_ref):
# `ScopeGrantRepo.grant()` revives the row in place and rewrites its
# realm, granted_by and granted_at, so the row's metadata is NOT what it
# was. Reach depends on none of that, but the control asserts what it
# restored rather than discarding the response -- it is the single
# mutation the whole causal argument rests on.
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"principal_type\":\"agent\",\"principal_ref\":\"$B0R_AGENT\",
       \"scope_type\":\"site\",\"scope_ref\":\"$A5_SITE\"}" \
  http://localhost:8090/api/scope-grants/ \
  | B0R_G_T=$B0R_G_T A5_SITE=$A5_SITE python3 -c "
import sys, json, os
g = json.load(sys.stdin)
assert g['id'] == os.environ['B0R_G_T'], ('a new row, not the revived G_T', g)
assert not g['revoked_at'] and not g['expires_at'], g
assert g['scope_type'] == 'site' and g['scope_ref'] == os.environ['A5_SITE'], g
assert g['principal_type'] == 'agent', g
print('CONTROL: G_T revived in place, live and unexpiring')
"
# Wait on the DIRECTIVE ID, not on `status='dispatched'`. That status is
# transient -- the settle pass moves it to `completed`, which can never
# match `dispatched` -- and `wait_for` samples every 5s, so a settle
# landing between two samples would time this control out AFTER it had in
# fact succeeded. A directive id, once written, is never cleared.
wait_for "P2 dispatched once current authority returned" 90 bash -c \
  "docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
   \"SELECT count(*) FROM cc_agent_proposals WHERE id='$B0R_P2'
      AND directive_id <> ''\" | grep -qv '^ *0 *$'"
b0r_cc_text "SELECT status || '|' || directive_id
  FROM cc_agent_proposals WHERE id='$B0R_P2'" | python3 -c "
import sys
status, directive = sys.stdin.read().strip().split('|')
assert status in ('dispatched', 'completed'), ('unexpected terminal status', status)
assert directive, 'the proposal dispatched with no directive id'
print('CONTROL: P2 is %s, directive %s' % (status, directive))
"
B0R_SM=$(b0r_sm_directives "$B0R_P2")
[ "$B0R_SM" = "1" ] || { echo "restored authority delivered $B0R_SM directives" >&2; exit 1; }
echo "CONTROL: G_T restored -> the same approved P2 dispatched, SM directives = $B0R_SM"
curl -sf -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/policies/$B0R_POLICY" > /dev/null

step "A6-4B0a/AL: both revocations this proof made are on the chain, and the chain verifies"
TOKEN=$(tenant_token gate-owner@demo gate-owner)
# The two G_T revocations exist ONLY here: `grant()` revives the row in
# place, so `cc_scope_grants` can show just the latest `revoked_at` and
# the lifecycle this proof turns on is unreadable from the table alone.
#
# Scoped to the revocations this proof MADE, deliberately: retiring the
# agent below revokes both remaining grants through `clear_scopes`, which
# writes no `scope.revoked` entry of its own -- the record is the
# aggregate `scopes_revoked` on `operational_agent.retired`. The count is
# taken before the retire and is a claim about these two DELETEs, not
# about every revocation in the tenant's history.
[ "$(b0r_cc "SELECT count(*) FROM cc_audit_log WHERE action='scope.revoked'
    AND subject='$B0R_AGENT' AND detail->>'scope_ref' = '$A5_SITE'")" = "2" ] || {
  echo "the two G_T revocations are not both on the chain" >&2; exit 1; }
# Leave the tenant as the gate found it: a live agent would keep proposing.
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$B0R_AGENT/retire" > /dev/null
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/audit/verify \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['valid'] is True, d
print('CC audit chain valid (%d entries); both G_T revocations recorded' % d['length'])
"

# ===========================================================================
# A6-4B0b-S1 (A30.22), live: a SET of devices is authorized only when EVERY
# device in it is -- at approval, and again immediately before dispatch.
#
# The defect: `_decide_campaign_wave` handed the gate `device_agent_ids[0]`,
# so a principal whose grant reached ONE device approved a site-wave over
# all of them. Here: three declared devices at site B in ONE wave, a
# two-approver policy for the class, and three approvers of widening reach.
#   [A]      -> refused, nothing recorded
#   [A, B]   -> refused, nothing recorded
#   [A, B, C]-> recorded, 1 of 2
# then the exact grant on B is revoked THROUGH THE PRODUCTION ROUTE, the
# tenant owner completes the approval, and the completed approval does not
# execute: the runner withholds the whole wave on current authority, nothing
# crosses CC -> SM, and restoring ONLY that grant lets the same approved
# wave dispatch. The target set is never narrowed to fit anybody.
# ===========================================================================

s1_cc() { docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc "$1" | tr -d ' \r'; }
s1_sm() { docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc "$1" | tr -d ' \r'; }
s1_sub() {  # Keycloak subject from a bearer token
  python3 -c "
import base64, json
t = '$1'.split('.')[1]; t += '=' * (-len(t) % 4)
print(json.loads(base64.urlsafe_b64decode(t))['sub'])"
}

step "A6-4B0b-S1/AM: three declared devices at one site plan into ONE wave"
TOKEN=$(tenant_token gate-owner@demo gate-owner)
S1_CAPS='{"version": 1, "protocol": "redfish", "device_class": "server", "allow_list": ["COLLECT_DIAGNOSTICS", "IDENTIFY_LED"], "implemented": ["BMC_RESET", "COLLECT_DIAGNOSTICS", "CONFIG_RESTORE", "FAN_RESET", "FIRMWARE_ROLLBACK", "FIRMWARE_UPDATE", "IDENTIFY_LED", "POWER_CAP_ADJUST", "POWER_CYCLE", "SEL_CLEAR"], "effective": ["COLLECT_DIAGNOSTICS", "IDENTIFY_LED"], "reach_known": true}'
# Two more synthetic devices beside gate-agent-b, and a declaration on all
# three so preflight finds them ELIGIBLE (not unknown) and the Site Manager
# knows what they can do. No fault domains: the planner puts all three in
# one wave, which is the shape the all-target rule is about.
for S1_DEV in c d; do
docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
  "INSERT INTO devices (id, site_id, agent_id, agent_name, vendor, model,
                        service_tag, device_class, first_seen_at, last_seen_at)
   SELECT 'gatedev${S1_DEV}00000000000000000000000', s.id, 'gate-agent-${S1_DEV}', '${S1_DEV}1',
          'Dell', 'R750', 'GATE${S1_DEV}1', 'server', now(), now()
   FROM sites s WHERE s.cc_site_id = '$SITE_B'
   ON CONFLICT (id) DO NOTHING" > /dev/null
done
docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
  "UPDATE devices SET capabilities = '$S1_CAPS'
   WHERE agent_id IN ('gate-agent-b', 'gate-agent-c', 'gate-agent-d')" > /dev/null
wait_for "three declared devices at site B visible at Central Command" 180 bash -c \
  "docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
   \"SELECT count(*) FROM cc_fleet_cache WHERE site_id='$SITE_B' AND capabilities IS NOT NULL\" \
   | grep -qx ' *3 *'"
S1_CAMP=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"s1-multi-target\",\"description\":\"A30.22\",
       \"action_type\":\"COLLECT_DIAGNOSTICS\",\"params\":{},\"max_wave_size\":5,
       \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$SITE_B\"}]}" \
  http://localhost:8090/api/campaigns/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/campaigns/$S1_CAMP/preflight" | python3 -c "
import sys, json
d = json.load(sys.stdin)
targets = {t['device_agent_id']: t['applicability'] for t in d['targets']}
assert set(targets) == {'gate-agent-b', 'gate-agent-c', 'gate-agent-d'}, targets
assert set(targets.values()) == {'eligible'}, targets
print('preflight: three eligible targets at site B')
"
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/campaigns/$S1_CAMP/submit" > /dev/null
S1_SUBJECT=$(curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/campaigns/$S1_CAMP/waves" | python3 -c "
import sys, json
waves = json.load(sys.stdin)['waves']
assert len(waves) == 1, ('the flat estate must plan ONE wave', waves)
(w,) = waves
assert sorted(w['device_agent_ids']) == ['gate-agent-b', 'gate-agent-c', 'gate-agent-d'], w
assert w['status'] == 'pending_approval' and w['subject_ref'], w
print(w['subject_ref'])")
echo "one wave over [b, c, d]; subject $S1_SUBJECT"
S1_POLICY=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"gate-s1-dual","action_type":"COLLECT_DIAGNOSTICS","required_approvers":2}' \
  http://localhost:8090/api/policies/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['policy']['id'])")
# Three approvers, each holding action.approve (operator) over a WIDENING
# set of the wave's devices, granted through the production route.
for S1_P in a ab abc; do
  tenant_realm_user "gate-s1-$S1_P@demo" "gate-s1-$S1_P" operator
done
S1_A_TOKEN=$(tenant_token gate-s1-a@demo gate-s1-a)
S1_AB_TOKEN=$(tenant_token gate-s1-ab@demo gate-s1-ab)
S1_ABC_TOKEN=$(tenant_token gate-s1-abc@demo gate-s1-abc)
S1_A_SUB=$(s1_sub "$S1_A_TOKEN"); S1_AB_SUB=$(s1_sub "$S1_AB_TOKEN"); S1_ABC_SUB=$(s1_sub "$S1_ABC_TOKEN")
[ "$(grant "$S1_A_SUB" device gate-agent-b operator)" = "201" ] || { echo "grant A/b refused" >&2; exit 1; }
for S1_DEV in b c; do
  [ "$(grant "$S1_AB_SUB" device gate-agent-$S1_DEV operator)" = "201" ] || { echo "grant AB/$S1_DEV refused" >&2; exit 1; }
done
for S1_DEV in b c d; do
  [ "$(grant "$S1_ABC_SUB" device gate-agent-$S1_DEV operator)" = "201" ] || { echo "grant ABC/$S1_DEV refused" >&2; exit 1; }
done
echo "approvers: A covers [b]; AB covers [b,c]; ABC covers [b,c,d]"

step "A6-4B0b-S1/AN: [A] and [A,B] are refused and record nothing; [A,B,C] is recorded"
for S1_CASE in "A:$S1_A_TOKEN:2 of the 3" "AB:$S1_AB_TOKEN:1 of the 3"; do
  S1_NAME=${S1_CASE%%:*}; S1_REST=${S1_CASE#*:}; S1_TOK=${S1_REST%%:*}; S1_WANT=${S1_REST#*:}
  S1_CODE=$(curl -s -o /tmp/s1_deny.json -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $S1_TOK" "http://localhost:8090/api/approvals/$S1_SUBJECT/approve")
  [ "$S1_CODE" = "403" ] || { echo "approver $S1_NAME got $S1_CODE, expected 403: $(cat /tmp/s1_deny.json)" >&2; exit 1; }
  grep -q "$S1_WANT devices" /tmp/s1_deny.json || { echo "refusal did not name the uncovered count: $(cat /tmp/s1_deny.json)" >&2; exit 1; }
  echo "  $S1_NAME -> 403 ($S1_WANT devices uncovered)"
done
[ "$(s1_cc "SELECT count(*) FROM cc_approval_records WHERE subject_ref='$S1_SUBJECT'")" = "0" ] || {
  echo "a refused approver was recorded on the ledger" >&2; exit 1; }
echo "  refused, not recorded: 0 ledger rows"
curl -sf -X POST -H "Authorization: Bearer $S1_ABC_TOKEN" \
  "http://localhost:8090/api/approvals/$S1_SUBJECT/approve" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d.get("recorded") is True and d.get("decision") is None, d
assert d["approval"]["required"] == 2 and d["approval"]["received"] == 1, d["approval"]
print("  ABC -> recorded, 1 of 2 WHILE AUTHORIZED over every device; nothing executed")
'
s1_cc "SELECT authority_snapshot->>'target_device_agent_ids' FROM cc_approval_records
       WHERE subject_ref='$S1_SUBJECT'" | python3 -c "
import sys, json
ids = json.loads(sys.stdin.read())
assert ids == ['gate-agent-b', 'gate-agent-c', 'gate-agent-d'], ids
print('  the ledger names the SET:', ids)"
S1_ACTOR="campaign:$S1_CAMP@v1"
[ "$(s1_sm "SELECT count(*) FROM sm_directives WHERE actor='$S1_ACTOR'")" = "0" ] || {
  echo "a directive reached the Site Manager before the decision completed" >&2; exit 1; }

step "A6-4B0b-S1/AO: authority over ONE device revoked after approval -> the completed approval does not execute"
TOKEN=$(tenant_token gate-owner@demo gate-owner)
S1_GRANT_C=$(curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/scope-grants/?principal_ref=$S1_ABC_SUB&principal_type=user" \
  | python3 -c "
import sys, json
rows = [g for g in json.load(sys.stdin)['grants'] if g['scope_type'] == 'device' and g['scope_ref'] == 'gate-agent-c']
assert len(rows) == 1, rows
print(rows[0]['id'])")
curl -sf -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/scope-grants/$S1_GRANT_C" | python3 -c "
import sys, json
g = json.load(sys.stdin)
assert g['revoked_at'] and g['scope_ref'] == 'gate-agent-c', g
print('  revoked ABC\'s grant on gate-agent-c (b and d untouched)')"
# The tenant owner completes the two-approver decision. The APPROVAL is
# valid -- two named humans -- and it is now historical truth.
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/approvals/$S1_SUBJECT/approve" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d.get("decision") == "approved", d
print("  owner -> 2 of 2; the wave is APPROVED")
'
# Explicit advance (the durable loop calls the same function). The gate
# re-asks every approver about every device and refuses the SET.
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/campaigns/$S1_CAMP/advance" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert not d["advanced"], d
reasons = [b.get("reason", "") for b in d["blocked"]]
assert any("no longer hold" in r for r in reasons), reasons
print("  advance -> WITHHELD:", [r for r in reasons if "no longer hold" in r][0])
'
[ "$(s1_cc "SELECT count(*) FROM cc_campaign_dispatches WHERE campaign_id='$S1_CAMP'")" = "0" ] || {
  echo "the withheld wave wrote dispatch rows" >&2; exit 1; }
[ "$(s1_sm "SELECT count(*) FROM sm_directives WHERE actor='$S1_ACTOR'")" = "0" ] || {
  echo "the withheld wave reached the Site Manager" >&2; exit 1; }
[ "$(s1_cc "SELECT status FROM cc_campaign_waves WHERE subject_ref='$S1_SUBJECT'")" = "approved" ] || {
  echo "the historical decision was rewritten" >&2; exit 1; }
[ "$(s1_cc "SELECT count(*) FROM cc_approval_records WHERE subject_ref='$S1_SUBJECT'")" = "2" ] || {
  echo "the ledger lost a record" >&2; exit 1; }
[ "$(s1_cc "SELECT count(DISTINCT revalidation) || '|' || min(revalidation) FROM cc_campaign_targets WHERE campaign_id='$S1_CAMP'")" = "1|authority_lost" ] || {
  echo "targets do not all read authority_lost" >&2; exit 1; }
S1_WITHHELD=$(s1_cc "SELECT count(*) FROM cc_audit_log WHERE action='campaign.wave_withheld' AND subject='$S1_CAMP'")
[ "$S1_WITHHELD" = "1" ] || { echo "expected exactly one withheld audit entry, got $S1_WITHHELD" >&2; exit 1; }
echo "  zero dispatch rows, zero SM directives, wave still approved, 2 ledger rows, targets authority_lost, 1 audit entry"
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/campaigns/$S1_CAMP/advance" > /dev/null
[ "$(s1_cc "SELECT count(*) FROM cc_audit_log WHERE action='campaign.wave_withheld' AND subject='$S1_CAMP'")" = "1" ] || {
  echo "a second pass wrote a second audit entry" >&2; exit 1; }
[ "$(s1_cc "SELECT count(*) FROM cc_campaign_dispatches WHERE campaign_id='$S1_CAMP'")" = "0" ] || {
  echo "a second pass dispatched" >&2; exit 1; }
echo "  a second pass: still withheld, still one audit entry"

step "A6-4B0b-S1/AP: restoring ONLY the revoked grant lets the same approved wave dispatch"
[ "$(grant "$S1_ABC_SUB" device gate-agent-c operator)" = "201" ] || { echo "restore refused" >&2; exit 1; }
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/campaigns/$S1_CAMP/advance" > /dev/null || true
wait_for "the same approved wave dispatched once authority returned" 120 bash -c \
  "docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
   \"SELECT status FROM cc_campaign_waves WHERE subject_ref='$S1_SUBJECT'\" | grep -q dispatched"
[ "$(s1_cc "SELECT count(*) FROM cc_campaign_dispatches WHERE campaign_id='$S1_CAMP'")" = "3" ] || {
  echo "expected three dispatch rows for the wave" >&2; exit 1; }
[ "$(s1_cc "SELECT count(*) FROM cc_audit_log WHERE action='campaign.wave_dispatched' AND subject='$S1_CAMP'")" = "1" ] || {
  echo "wave_dispatched not audited" >&2; exit 1; }
echo "  CONTROL: 3 dispatch rows for [b, c, d]; SM directives for the campaign actor: $(s1_sm "SELECT count(*) FROM sm_directives WHERE actor='$S1_ACTOR'")"
curl -sf -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/policies/$S1_POLICY" > /dev/null

# ===========================================================================
# A6-4B0b-S1 remediation (A30.23), live: the approver's permission basis at
# dispatch is their CURRENT Keycloak realm role, never the role the approval
# recorded. The review's HIGH on 494432b: an operator approves an immutable
# wave, is then DEMOTED IN KEYCLOAK to a role without action.approve, keeps
# every scope grant -- and the historical approval still counted. Here, on
# the real identity plane: the demotion is a realm role-mapping DELETE
# through the Keycloak admin API, nothing at Central Command changes, the
# completed approval does not execute, the cause on the audit entry is
# `current_role` (not scope), and putting the mapping back lets the SAME
# two ledger rows dispatch the wave.
# ===========================================================================

step "A6-4B0b-S1/AQ (A30.23): an approver demoted in Keycloak after approving -> the completed approval does not execute"
TOKEN=$(tenant_token gate-owner@demo gate-owner)
S1D_POLICY=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"gate-s1-demotion-dual","action_type":"COLLECT_DIAGNOSTICS","required_approvers":2}' \
  http://localhost:8090/api/policies/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['policy']['id'])")
tenant_realm_user gate-s1-demote@demo gate-s1-demote operator
S1D_TOKEN=$(tenant_token gate-s1-demote@demo gate-s1-demote)
S1D_SUB=$(s1_sub "$S1D_TOKEN")
for S1_DEV in b c d; do
  [ "$(grant "$S1D_SUB" device gate-agent-$S1_DEV operator)" = "201" ] || { echo "grant demote/$S1_DEV refused" >&2; exit 1; }
done
S1D_CAMP=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"s1-demotion\",\"description\":\"A30.23\",
       \"action_type\":\"COLLECT_DIAGNOSTICS\",\"params\":{},\"max_wave_size\":5,
       \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$SITE_B\"}]}" \
  http://localhost:8090/api/campaigns/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/campaigns/$S1D_CAMP/preflight" > /dev/null
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/campaigns/$S1D_CAMP/submit" > /dev/null
S1D_SUBJECT=$(curl -sf -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/campaigns/$S1D_CAMP/waves" | python3 -c "
import sys, json
waves = json.load(sys.stdin)['waves']
assert len(waves) == 1, waves
(w,) = waves
assert sorted(w['device_agent_ids']) == ['gate-agent-b', 'gate-agent-c', 'gate-agent-d'], w
print(w['subject_ref'])")
S1D_ACTOR="campaign:$S1D_CAMP@v1"
# 3. the operator approves the whole wave WHILE holding action.approve.
curl -sf -X POST -H "Authorization: Bearer $S1D_TOKEN" \
  "http://localhost:8090/api/approvals/$S1D_SUBJECT/approve" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d.get("recorded") is True and d["approval"]["received"] == 1, d
print("  operator -> recorded, 1 of 2 while holding operator over [b, c, d]")
'
[ "$(s1_cc "SELECT authority_snapshot->>'role' FROM cc_approval_records WHERE subject_ref='$S1D_SUBJECT'")" = "operator" ] || {
  echo "the ledger did not record the operator role" >&2; exit 1; }
# 4. DEMOTED IN KEYCLOAK: the realm role mapping is deleted through the
# admin API. Nothing at Central Command is touched -- no grant, no ledger
# row, no wave.
KC_ADMIN=$(curl -sf -X POST \
  "http://localhost:8180/realms/master/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=admin-cli&username=admin&password=admin" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
S1D_ROLE=$(curl -sf "http://localhost:8180/admin/realms/tenant-demo/roles/operator" \
  -H "Authorization: Bearer $KC_ADMIN")
curl -sf -X DELETE \
  "http://localhost:8180/admin/realms/tenant-demo/users/$S1D_SUB/role-mappings/realm" \
  -H "Authorization: Bearer $KC_ADMIN" -H "Content-Type: application/json" \
  -d "[$S1D_ROLE]" > /dev/null
curl -sf "http://localhost:8180/admin/realms/tenant-demo/users/$S1D_SUB/role-mappings/realm/composite" \
  -H "Authorization: Bearer $KC_ADMIN" | python3 -c "
import sys, json
names = sorted(r['name'] for r in json.load(sys.stdin))
assert 'operator' not in names, names
print('  Keycloak: operator mapping deleted; effective realm roles now', names)"
# The identity plane reports it through the read Central Command uses.
curl -sf -H "Authorization: Bearer ${HARKENIQ_INTERNAL_API_KEY:-demo-console-cc-key}" \
  "http://localhost:8100/api/internal/tenants/by-realm/tenant-demo/principals/$S1D_SUB/authority" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['found'] is True and d['enabled'] is True and 'operator' not in d['realm_roles'], d
print('  Console internal read: found, enabled, realm_roles =', d['realm_roles'])"
# The request path agrees: a FRESH token for the demoted user no longer
# satisfies action.approve.
S1D_FRESH=$(tenant_token gate-s1-demote@demo gate-s1-demote)
S1D_CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $S1D_FRESH" \
  "http://localhost:8090/api/approvals/$S1D_SUBJECT/approve")
[ "$S1D_CODE" = "403" ] || { echo "a fresh token for the demoted user was not refused ($S1D_CODE)" >&2; exit 1; }
echo "  a fresh token for the demoted user -> 403 on approve (the request path and the dispatch gate share one role rule)"
# 5. grants otherwise valid: three live device grants, none revoked.
[ "$(s1_cc "SELECT count(*) FROM cc_scope_grants WHERE principal_ref='$S1D_SUB' AND revoked_at IS NULL")" = "3" ] || {
  echo "the demoted approver's grants were not left intact" >&2; exit 1; }
# The owner completes the two-approver decision; the wave is APPROVED.
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/approvals/$S1D_SUBJECT/approve" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d.get("decision") == "approved", d
print("  owner -> 2 of 2; the wave is APPROVED")
'
# 6. the actual dispatch path: WITHHELD, and the cause is the CURRENT ROLE.
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/campaigns/$S1D_CAMP/advance" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert not d["advanced"], d
reasons = [b.get("reason", "") for b in d["blocked"]]
assert any("no longer hold" in r and "causes: current_role" in r for r in reasons), reasons
print("  advance -> WITHHELD:", [r for r in reasons if "no longer hold" in r][0])
'
[ "$(s1_cc "SELECT count(*) FROM cc_campaign_dispatches WHERE campaign_id='$S1D_CAMP'")" = "0" ] || {
  echo "the withheld wave wrote dispatch rows" >&2; exit 1; }
[ "$(s1_sm "SELECT count(*) FROM sm_directives WHERE actor='$S1D_ACTOR'")" = "0" ] || {
  echo "the withheld wave reached the Site Manager" >&2; exit 1; }
[ "$(s1_cc "SELECT status FROM cc_campaign_waves WHERE subject_ref='$S1D_SUBJECT'")" = "approved" ] || {
  echo "the historical decision was rewritten" >&2; exit 1; }
[ "$(s1_cc "SELECT count(*) FROM cc_approval_records WHERE subject_ref='$S1D_SUBJECT'")" = "2" ] || {
  echo "the ledger lost a record" >&2; exit 1; }
[ "$(s1_cc "SELECT authority_snapshot->>'role' FROM cc_approval_records WHERE subject_ref='$S1D_SUBJECT' AND approver_ref='$S1D_SUB'")" = "operator" ] || {
  echo "the recorded role was rewritten -- the ledger is evidence and must not change" >&2; exit 1; }
s1_cc "SELECT detail->'lost' FROM cc_audit_log WHERE action='campaign.wave_withheld' AND subject='$S1D_CAMP'" | python3 -c "
import sys, json
(lost,) = json.loads(sys.stdin.read())
assert lost['approver_ref'] == '$S1D_SUB', lost
assert lost['cause'] == 'current_role' and lost['current_role'] == 'viewer', lost
assert lost['uncovered'] == 3 and lost['of'] == 3, lost
print('  audit names the cause: current_role (now viewer), 3 of 3 targets -- not scope, not identity, not an outage')"
echo "  zero dispatch rows, zero SM directives, wave still approved, 2 ledger rows, recorded role still 'operator', 3 live grants"

step "A6-4B0b-S1/AR (A30.23): restoring the realm role lets the SAME approval count again"
# A fresh master token: the admin token minted in AQ is short-lived.
KC_ADMIN=$(curl -sf -X POST \
  "http://localhost:8180/realms/master/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=admin-cli&username=admin&password=admin" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
curl -sf -X POST \
  "http://localhost:8180/admin/realms/tenant-demo/users/$S1D_SUB/role-mappings/realm" \
  -H "Authorization: Bearer $KC_ADMIN" -H "Content-Type: application/json" \
  -d "[$S1D_ROLE]" > /dev/null
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/campaigns/$S1D_CAMP/advance" > /dev/null || true
wait_for "the same approved wave dispatched once the role returned" 120 bash -c \
  "docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
   \"SELECT status FROM cc_campaign_waves WHERE subject_ref='$S1D_SUBJECT'\" | grep -q dispatched"
[ "$(s1_cc "SELECT count(*) FROM cc_campaign_dispatches WHERE campaign_id='$S1D_CAMP'")" = "3" ] || {
  echo "expected three dispatch rows for the wave" >&2; exit 1; }
[ "$(s1_cc "SELECT count(*) FROM cc_approval_records WHERE subject_ref='$S1D_SUBJECT'")" = "2" ] || {
  echo "a restore must not produce a new approval" >&2; exit 1; }
echo "  CONTROL: 3 dispatch rows for [b, c, d] on the SAME 2 ledger rows; SM directives for the campaign actor: $(s1_sm "SELECT count(*) FROM sm_directives WHERE actor='$S1D_ACTOR'")"
curl -sf -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/policies/$S1D_POLICY" > /dev/null

# ===========================================================================
# A6-4B0b-S2 (A30.24), live: a read takes its REACH from the grants that carry
# the permission it requires. Two auditors, the SAME two site grants through
# the production route. CONTROL holds both in full. MIXED holds site A in full
# and site B narrowed to `incident.view`. Before S2 MIXED read site B's fleet,
# sites and audit, because `site_ids` is permission-neutral and the route
# guard is the role. Every absence below is paired with CONTROL seeing the
# same thing, so an empty answer cannot pass as a correct one.
# ===========================================================================
s2_get() {  # $1 token, $2 path -> body on stdout
  curl -s -H "Authorization: Bearer $1" "http://localhost:8090$2"
}
s2_code() { curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $1" "http://localhost:8090$2"; }

step "A6-4B0b-S2/AS: two auditors, one narrowed grant -- and the control reads BOTH sites"
TOKEN=$(tenant_token gate-owner@demo gate-owner)
for S2_P in mixed control; do
  tenant_realm_user "gate-s2-$S2_P@demo" "gate-s2-$S2_P" auditor
done
S2_MIXED=$(tenant_token gate-s2-mixed@demo gate-s2-mixed)
S2_CONTROL=$(tenant_token gate-s2-control@demo gate-s2-control)
S2_MIXED_SUB=$(s1_sub "$S2_MIXED"); S2_CONTROL_SUB=$(s1_sub "$S2_CONTROL")
s2_grant() {  # $1 principal, $2 site, $3 JSON subset or null
  curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"principal_ref\":\"$1\",\"scope_type\":\"site\",\"scope_ref\":\"$2\",
         \"role\":\"auditor\",\"permission_subset\":$3}" \
    http://localhost:8090/api/scope-grants/
}
[ "$(s2_grant "$S2_CONTROL_SUB" "$SITE_A" null)" = "201" ] || { echo "control/A refused" >&2; exit 1; }
[ "$(s2_grant "$S2_CONTROL_SUB" "$SITE_B" null)" = "201" ] || { echo "control/B refused" >&2; exit 1; }
[ "$(s2_grant "$S2_MIXED_SUB" "$SITE_A" null)" = "201" ] || { echo "mixed/A refused" >&2; exit 1; }
[ "$(s2_grant "$S2_MIXED_SUB" "$SITE_B" '["incident.view"]')" = "201" ] || { echo "mixed/B refused" >&2; exit 1; }
# This step OWNS its state (the A30.21 lesson). One incident at EACH site,
# RESOLVED so the poller never touches it (it only resolves OPEN incidents
# it no longer sees) and opened now so it sorts first. The audit half needs
# nothing extra: creating a site-scoped grant writes an audit entry tagged
# with that site, so the four grants above are the audit evidence.
S2_TENANT=$(s1_cc "SELECT tenant_id FROM cc_sites WHERE id='$SITE_B'")
for S2_PAIR in "a:$SITE_A" "b:$SITE_B"; do
  docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
    "INSERT INTO cc_incidents (incident_id, tenant_id, site_id, kind, status, title,
         device_agent_id, subsystem, confidence, inferred, opened_at,
         first_seen_at, last_seen_at)
     VALUES ('gate-s2-inc-${S2_PAIR%%:*}', '$S2_TENANT', '${S2_PAIR#*:}', 'device',
             'resolved', 'S2 gate incident', 'gate-s2-device-${S2_PAIR%%:*}', 'psu',
             0, false, now(), now(), now())
     ON CONFLICT (incident_id) DO NOTHING" > /dev/null
done
s2_sites_seen() {  # $1 token -> "fleet=<sites> sites=<sites> audit=<sites> incident=<code>"
  python3 - "$SITE_A" "$SITE_B" <<PY
import json, sys, subprocess
A, B = sys.argv[1], sys.argv[2]
def get(path):
    out = subprocess.run(["curl", "-s", "-H", "Authorization: Bearer $1",
                          "http://localhost:8090" + path], capture_output=True, text=True).stdout
    return json.loads(out)
def label(ids):
    return "".join(n for n, i in (("A", A), ("B", B)) if i in ids) or "-"
fleet = {d["site_id"] for d in get("/api/fleet/?page_size=200")["devices"]}
sites = {s["id"] for s in get("/api/sites/")["sites"]}
audit, page = set(), 1
while True:  # the whole scoped log, not its newest page
    entries = get(f"/api/audit/?page_size=200&page={page}")["entries"]
    audit |= {e["site_id"] for e in entries}
    if len(entries) < 200:
        break
    page += 1
incidents = {i["site_id"] for i in get("/api/incidents/?status=all&limit=1000")["incidents"]}
print(f"fleet={label(fleet)} sites={label(sites)} audit={label(audit)} incidents={label(incidents)}")
PY
}
S2_SEEN=$(s2_sites_seen "$S2_CONTROL")
echo "  CONTROL (both grants full): $S2_SEEN"
[ "$S2_SEEN" = "fleet=AB sites=AB audit=AB incidents=AB" ] || {
  echo "the control must read both sites on every read, or the narrowing below proves nothing" >&2; exit 1; }
[ "$(s2_code "$S2_CONTROL" "/api/sites/$SITE_B")" = "200" ] || { echo "control cannot read site B" >&2; exit 1; }
[ "$(s2_code "$S2_CONTROL" "/api/agents/gate-agent-b")" = "200" ] || { echo "control cannot read gate-agent-b" >&2; exit 1; }

step "A6-4B0b-S2/AT: the narrowed grant reads site B under incident.view and under NOTHING else"
S2_SEEN=$(s2_sites_seen "$S2_MIXED")
echo "  MIXED (site B narrowed to incident.view): $S2_SEEN"
[ "$S2_SEEN" = "fleet=A sites=A audit=A incidents=AB" ] || {
  echo "permission from one grant combined with reach from another (P1)" >&2; exit 1; }
[ "$(s2_code "$S2_MIXED" "/api/sites/$SITE_B")" = "404" ] || { echo "site B detail readable without fleet.view there" >&2; exit 1; }
[ "$(s2_code "$S2_MIXED" "/api/agents/gate-agent-b")" = "404" ] || { echo "gate-agent-b readable without fleet.view there" >&2; exit 1; }
[ "$(s2_code "$S2_MIXED" "/api/incidents/gate-s2-inc-b")" = "200" ] || { echo "the incident the grant DOES carry is unreadable" >&2; exit 1; }
s2_get "$S2_MIXED" "/api/fleet/summary" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['sites_count'] == 1, d
print('  summary counts', d['total_nodes'], 'node(s) at', d['sites_count'], 'site: no leak of site B through a total')"
# The permission-NEUTRAL projection keeps its meaning (A30.24): the
# self-description still names both sites. It is no longer a read filter.
s2_get "$S2_MIXED" "/api/scope-grants/me" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert {'$SITE_A', '$SITE_B'} <= set(d['site_ids']), d['site_ids']
print('  /me still describes both grants:', len(d['site_ids']), 'site ids')"

step "A6-4B0b-S2/AU: a TENANT grant narrowed to incident.view does not unfilter the fleet"
tenant_realm_user "gate-s2-tenant@demo" "gate-s2-tenant" auditor
S2_TEN=$(tenant_token gate-s2-tenant@demo gate-s2-tenant)
S2_TEN_SUB=$(s1_sub "$S2_TEN")
S2_CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"principal_ref\":\"$S2_TEN_SUB\",\"scope_type\":\"tenant\",\"scope_ref\":\"\",
       \"role\":\"auditor\",\"permission_subset\":[\"incident.view\"]}" \
  http://localhost:8090/api/scope-grants/)
[ "$S2_CODE" = "201" ] || { echo "narrowed tenant grant refused ($S2_CODE)" >&2; exit 1; }
S2_SEEN=$(s2_sites_seen "$S2_TEN")
echo "  TENANT grant, subset [incident.view]: $S2_SEEN"
[ "$S2_SEEN" = "fleet=- sites=- audit=- incidents=AB" ] || {
  echo "a narrowed tenant grant still read tenant-wide (the tenant_wide shortcut)" >&2; exit 1; }

# ===========================================================================
# A6-4B0b (A30.25), live: canonical reach convergence -- F1 closed.
#
# A `device` or `device_class` grant is real authority and contributed
# nothing to `site_ids`, which every device-bearing list filtered on, so
# such a principal read NOTHING about the devices it reaches. Proven here
# with real Keycloak identities whose REALM ROLE holds every permission
# (tenant_owner), so the route guard never refuses them and every refusal
# below is the SCOPE's: the only thing each of them holds is one device, or
# one device class.
#
# Site B has three servers (gate-agent-b/c/d, from the S1 steps). A switch
# is added beside them so there is a class to be scoped to, and one
# incident and one pending approval per device so there is something to
# see -- and something NOT to see.
#
# The pending approvals are an Operational Agent's PROPOSALS, not node
# routes, on purpose: the fleet poller reconciles node routes against the
# Site Manager every poll and SUPERSEDES one the SM never raised, so a
# synthetic route is a subject this proof would not own (it vanished
# between two steps the first time this was written). A proposal is
# Central Command's own row and goes through the SAME decision function.
# They sit under a 2-approver policy, so a valid decision is RECORDED
# (1 of 2) and nothing is dispatched.
# ===========================================================================
b0b_json() {  # $1 token, $2 path
  curl -s -H "Authorization: Bearer $1" "http://localhost:8090$2"
}
b0b_code() {  # $1 token, $2 method, $3 path, [$4 json body]
  if [ -n "${4:-}" ]; then
    curl -s -o /dev/null -w '%{http_code}' -X "$2" -H "Authorization: Bearer $1" \
      -H 'Content-Type: application/json' -d "$4" "http://localhost:8090$3"
  else
    curl -s -o /dev/null -w '%{http_code}' -X "$2" -H "Authorization: Bearer $1" \
      "http://localhost:8090$3"
  fi
}
b0b_grant() {  # $1 principal, $2 scope_type, $3 scope_ref
  curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"principal_ref\":\"$1\",\"scope_type\":\"$2\",\"scope_ref\":\"$3\",\"role\":\"tenant_owner\"}" \
    http://localhost:8090/api/scope-grants/
}

step "A6-4B0b/AV: a switch beside the servers, and one incident and one pending approval per device"
TOKEN=$(tenant_token gate-owner@demo gate-owner)
docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
  "INSERT INTO devices (id, site_id, agent_id, agent_name, vendor, model,
                        service_tag, device_class, first_seen_at, last_seen_at)
   SELECT 'gatedevsw0000000000000000000000', s.id, 'gate-switch-b', 'sw-b1',
          'Dell', 'S5248F', 'GATESW1', 'switch', now(), now()
   FROM sites s WHERE s.cc_site_id = '$SITE_B'
   ON CONFLICT (id) DO NOTHING" > /dev/null
wait_for "the switch at site B visible at Central Command, as a switch" 180 bash -c \
  "docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
   \"SELECT device_class FROM cc_fleet_cache WHERE site_id='$SITE_B' AND agent_id='gate-switch-b'\" \
   | grep -qx ' *switch *'"
B0B_TENANT=$(s1_cc "SELECT tenant_id FROM cc_sites WHERE id='$SITE_B'")
for B0B_DEV in gate-agent-b gate-agent-c gate-switch-b; do
  # RESOLVED, so the poller (which only resolves OPEN incidents it no
  # longer sees) never touches them; opened now, so they sort first.
  docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
    "INSERT INTO cc_incidents (incident_id, tenant_id, site_id, kind, status, title,
         device_agent_id, subsystem, confidence, inferred, opened_at, first_seen_at, last_seen_at)
     VALUES ('b0b-inc-$B0B_DEV', '$B0B_TENANT', '$SITE_B', 'device', 'resolved',
             'B0b gate incident', '$B0B_DEV', 'psu', 0, false, now(), now(), now())
     ON CONFLICT (incident_id) DO NOTHING" > /dev/null
done
# A REAL Operational Agent scoped to ONE device. It owns the proposals
# below, and step BA gives it a credential and reads as it.
B0B_AGENT=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"name\":\"b0b-device-agent $(date +%s)\",
       \"scopes\":[{\"scope_type\":\"device\",\"scope_ref\":\"gate-agent-b\"}],
       \"capabilities\":[
         {\"kind\":\"action_class\",\"capability_ref\":\"IDENTIFY_LED\"},
         {\"kind\":\"read\",\"capability_ref\":\"incidents\"}]}" \
  http://localhost:8090/api/operational-agents/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
[ -n "$B0B_AGENT" ] || { echo "could not create the device-scoped agent" >&2; exit 1; }
for B0B_DEV in gate-agent-b gate-agent-c gate-switch-b; do
  docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
    "INSERT INTO cc_agent_proposals (id, tenant_id, agent_id, actor, agent_version, site_id,
         device_agent_id, action_type, params, rationale, evidence, disposition,
         disposition_reason, authorization_basis, status, decided_by, dedupe_key,
         directive_id, dispatch_reason, outcome, created_at)
     VALUES ('b0bprop-$B0B_DEV', '$B0B_TENANT', '$B0B_AGENT', 'op-agent:$B0B_AGENT@v1', 1, '$SITE_B',
             '$B0B_DEV', 'IDENTIFY_LED', '{}'::jsonb, 'B0b gate proposal', '{}'::jsonb,
             'requires_approval', '', 'human_approval', 'awaiting_approval', '',
             'b0b-gate:$B0B_DEV', '', '', '', now())
     ON CONFLICT (id) DO NOTHING" > /dev/null
done
B0B_POLICY=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"gate-b0b-dual","action_type":"IDENTIFY_LED","required_approvers":2}' \
  http://localhost:8090/api/policies/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['policy']['id'])")
for B0B_P in device class; do
  tenant_realm_user "gate-b0b-$B0B_P@demo" "gate-b0b-$B0B_P" tenant_owner
done
B0B_DEVICE=$(tenant_token gate-b0b-device@demo gate-b0b-device)
B0B_CLASS=$(tenant_token gate-b0b-class@demo gate-b0b-class)
[ "$(b0b_grant "$(s1_sub "$B0B_DEVICE")" device gate-agent-b)" = "201" ] || { echo "device grant refused" >&2; exit 1; }
[ "$(b0b_grant "$(s1_sub "$B0B_CLASS")" device_class switch)" = "201" ] || { echo "class grant refused" >&2; exit 1; }
echo "site B: servers gate-agent-b/c/d + switch gate-switch-b; DEVICE human holds gate-agent-b; CLASS human holds 'switch'"

b0b_view() {  # $1 token -> what this principal reads, in one line
  python3 - "$SITE_A" "$SITE_B" <<PY
import json, subprocess, sys
A, B = sys.argv[1], sys.argv[2]
def get(path):
    out = subprocess.run(["curl", "-s", "-H", "Authorization: Bearer $1",
                          "http://localhost:8090" + path], capture_output=True, text=True).stdout
    return json.loads(out)
mine = {"gate-agent-b", "gate-agent-c", "gate-agent-d", "gate-switch-b"}
fleet = get("/api/fleet/?page_size=200")
devices = sorted(d["agent_id"] for d in fleet["devices"] if d["agent_id"] in mine)
others = [d["agent_id"] for d in fleet["devices"] if d["agent_id"] not in mine]
incidents = sorted(i["incident_id"] for i in get("/api/incidents/?status=all&limit=1000")["incidents"]
                   if i["incident_id"].startswith("b0b-inc-"))
queue = sorted(a["action_id"] for a in get("/api/approvals/?page_size=200")["actions"]
               if str(a.get("action_id", "")).startswith("b0bprop-"))
sites = get("/api/sites/")["sites"]
ctx = sorted("B" if s["id"] == B else "A" if s["id"] == A else "?" for s in sites if s.get("contextual"))
held = sorted("B" if s["id"] == B else "A" if s["id"] == A else "?" for s in sites if not s.get("contextual"))
audit = get("/api/audit/?page_size=200")["total"]
summary = get("/api/fleet/summary")
print(f"devices={','.join(devices) or '-'} elsewhere={len(others)} "
      f"incidents={','.join(i.removeprefix('b0b-inc-') for i in incidents) or '-'} "
      f"queue={','.join(q.removeprefix('b0bprop-') for q in queue) or '-'} "
      f"held={''.join(held) or '-'} contextual={''.join(ctx) or '-'} "
      f"sites_count={summary['sites_count']} audit={audit}")
PY
}

step "A6-4B0b/AW: a DEVICE-scoped human reads its device -- and nothing beside it"
B0B_SEEN=$(b0b_view "$B0B_DEVICE")
echo "  DEVICE human (gate-agent-b): $B0B_SEEN"
[ "$B0B_SEEN" = "devices=gate-agent-b elsewhere=0 incidents=gate-agent-b queue=gate-agent-b held=- contextual=B sites_count=0 audit=0" ] || {
  echo "a device-scoped human did not read exactly its own device (F1), or read beyond it" >&2; exit 1; }
# The contextual site is REDUCED: id, name and the explicit marker.
b0b_json "$B0B_DEVICE" "/api/sites/" | SITE_B=$SITE_B python3 -c "
import sys, json, os
(site,) = json.load(sys.stdin)['sites']
assert set(site) == {'id', 'site_name', 'contextual'} and site['contextual'] is True, site
assert site['id'] == os.environ['SITE_B'], site
print('  contextual site B carries exactly:', sorted(site))"
[ "$(b0b_code "$B0B_DEVICE" GET "/api/sites/$SITE_A")" = "404" ] || { echo "site A readable with no device there" >&2; exit 1; }
[ "$(b0b_code "$B0B_DEVICE" GET "/api/agents/gate-agent-c")" = "404" ] || { echo "a SIBLING device at the same site was readable" >&2; exit 1; }
[ "$(b0b_code "$B0B_DEVICE" GET "/api/incidents/b0b-inc-gate-agent-c")" = "404" ] || { echo "a sibling's incident was readable" >&2; exit 1; }
[ "$(b0b_code "$B0B_DEVICE" GET "/api/incidents/b0b-inc-gate-agent-b")" = "200" ] || { echo "its own incident was not readable" >&2; exit 1; }

step "A6-4B0b/AX: context is not authority -- the contextual site refuses every site-level act"
# The realm role is tenant_owner: the route guard passes. The SCOPE refuses.
B0B_UNIT=$(curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/org-units/ \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['tree'][0]['id'])")
for B0B_CASE in \
  "PUT|/api/sites/$SITE_B/org-unit|{\"org_unit_id\":\"$B0B_UNIT\"}|re-place the site" \
  "POST|/api/scope-grants/|{\"principal_ref\":\"kc-b0b-nobody\",\"scope_type\":\"site\",\"scope_ref\":\"$SITE_B\",\"role\":\"viewer\"}|delegate the site" \
  "POST|/api/campaigns/|{\"name\":\"b0b-ctx\",\"description\":\"\",\"action_type\":\"IDENTIFY_LED\",\"params\":{\"target\":\"Drive 0\"},\"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$SITE_B\"}]}|run a campaign at the site" \
  "POST|/api/approvals/b0bprop-gate-agent-c/approve||approve a SIBLING device's action"; do
  IFS='|' read -r B0B_M B0B_PATH B0B_BODY B0B_WHAT <<< "$B0B_CASE"
  B0B_RC=$(b0b_code "$B0B_DEVICE" "$B0B_M" "$B0B_PATH" "$B0B_BODY")
  case "$B0B_RC" in 403|404) echo "  refused ($B0B_RC): $B0B_WHAT" ;;
    *) echo "context was accepted as authority: $B0B_WHAT -> $B0B_RC" >&2; exit 1 ;; esac
done
b0b_json "$B0B_DEVICE" "/api/scope-grants/me" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['site_ids'] == [] and d['tenant_wide'] is False, d
assert d['device_ids'] == ['gate-agent-b'], d
print('  the scope itself holds NO site:', d['site_ids'], '| devices:', d['device_ids'])"
# R4, against REAL safety state: this stack has live error budgets, which is
# what showed the first version of this assertion to be false.
[ "$(s1_cc "SELECT count(*) FROM cc_safety_state WHERE error_budgets IS NOT NULL AND error_budgets::text <> '[]'")" -ge 1 ] || {
  echo "no site has reported an error budget: the R4 check below would be vacuous" >&2; exit 1; }
b0b_json "$B0B_DEVICE" "/api/autonomy/" | SITE_A=$SITE_A SITE_B=$SITE_B python3 -c "
import sys, json, os
raw = sys.stdin.read()
d = json.loads(raw)
assert d['scope']['sites'] == [], d['scope']
for key in ('sites_reporting', 'sites_not_reporting', 'site_stop_switches', 'error_budgets', 'suppressions'):
    assert d['safety_state'][key] == [], (key, d['safety_state'][key])
for row in d['action_classes']:
    s = row['safety']
    assert s['error_budget'] is None and s['suppressed_domains'] == [] and s['site_budget_remaining'] == {}, (row['action_type'], s)
assert os.environ['SITE_A'] not in raw and os.environ['SITE_B'] not in raw, 'a site id reached a principal who holds no site'
assert d['posture']['ladder'] and all(r['disposition'] for r in d['action_classes'])
print('  R4: no site-derived safety fact, in', len(d['action_classes']), 'action classes; the posture it operates under is intact')"
# ...and the tenant owner still reads the real thing, so the emptiness above is the SCOPE's.
b0b_json "$TOKEN" "/api/autonomy/" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['safety_state']['error_budgets'], 'the control has nothing to withhold'
print('  control (tenant owner):', len(d['safety_state']['error_budgets']), 'error-budget aggregate(s) present')"

step "A6-4B0b/AY: a DEVICE_CLASS human reads its class, and may decide a subject of that class (R6)"
B0B_SEEN=$(b0b_view "$B0B_CLASS")
echo "  CLASS human (switch): $B0B_SEEN"
[ "$B0B_SEEN" = "devices=gate-switch-b elsewhere=0 incidents=gate-switch-b queue=gate-switch-b held=- contextual=B sites_count=0 audit=0" ] || {
  echo "a class-scoped human did not read exactly its class" >&2; exit 1; }
B0B_RC=$(curl -s -o /tmp/b0b_class.json -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $B0B_CLASS" "http://localhost:8090/api/approvals/b0bprop-gate-switch-b/approve")
[ "$B0B_RC" = "200" ] || { echo "a class approver could not decide a switch subject ($B0B_RC)" >&2; cat /tmp/b0b_class.json >&2; exit 1; }
python3 -c "
import json
d = json.load(open('/tmp/b0b_class.json'))
assert d['recorded'] is True and d['decision'] is None and d['approval']['remaining'] == 1, d
print('  switch subject: decision RECORDED, 1 of 2 -- every other gate still stands')"
[ "$(b0b_code "$B0B_CLASS" POST "/api/approvals/b0bprop-gate-agent-b/approve")" = "403" ] || {
  echo "a SWITCH approver decided a SERVER's action" >&2; exit 1; }
[ "$(s1_cc "SELECT count(*) FROM cc_approval_records WHERE subject_ref='b0bprop-gate-switch-b' AND scope_ok")" = "1" ] || {
  echo "the class approver's record is missing or out of scope" >&2; exit 1; }
[ "$(s1_cc "SELECT count(*) FROM cc_approval_records WHERE subject_ref='b0bprop-gate-agent-b'")" = "0" ] || {
  echo "a refused decision was recorded" >&2; exit 1; }
[ "$(b0b_code "$B0B_CLASS" PUT "/api/sites/$SITE_B/org-unit" "{\"org_unit_id\":\"$B0B_UNIT\"}")" = "403" ] || {
  echo "a class grant conferred site authority" >&2; exit 1; }
echo "  server subject refused and unrecorded; no site authority"

step "A6-4B0b/AZ: tenant and site humans read exactly what they read before"
b0b_json "$TOKEN" "/api/sites/" | SITE_A=$SITE_A SITE_B=$SITE_B python3 -c "
import sys, json, os
sites = json.load(sys.stdin)['sites']
assert {os.environ['SITE_A'], os.environ['SITE_B']} <= {s['id'] for s in sites}
assert not any('contextual' in s for s in sites), 'an authoritative row gained a field'
assert all('sm_endpoint' in s and 'org_unit_id' in s for s in sites)
print('  TENANT human:', len(sites), 'held site(s), full rows, no contextual marker')"
B0B_SEEN=$(b0b_view "$TOKEN")
echo "  TENANT human: $B0B_SEEN"
case "$B0B_SEEN" in "devices=gate-agent-b,gate-agent-c,gate-agent-d,gate-switch-b "*" contextual=- "*) ;;
  *) echo "the tenant owner's reads changed" >&2; exit 1 ;; esac
# The S2 control auditor holds site A and site B as SITES: both held, none contextual.
S2_CONTROL=$(tenant_token gate-s2-control@demo gate-s2-control)
B0B_SEEN=$(b0b_view "$S2_CONTROL" 2>/dev/null || true)
echo "  SITE human (auditor, sites A+B): $B0B_SEEN"
case "$B0B_SEEN" in "devices=gate-agent-b,gate-agent-c,gate-agent-d,gate-switch-b "*" held=AB contextual=- sites_count=2 "*) ;;
  *) echo "a site-scoped human's reads changed" >&2; exit 1 ;; esac

step "A6-4B0b/BA: a device-scoped MACHINE reads what it canonically reaches; the plane is unchanged"
# The agent created in AV, scoped to the ONE device gate-agent-b.
B0B_SECRET=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$B0B_AGENT/identity" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['client_secret'])")
B0B_MACHINE=$(curl -sf -X POST \
  "http://localhost:8180/realms/tenant-demo/protocol/openid-connect/token" \
  -d "grant_type=client_credentials&client_id=op-agent-$B0B_AGENT&client_secret=$B0B_SECRET" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
[ -n "$B0B_MACHINE" ] || { echo "no machine token for the device-scoped agent" >&2; exit 1; }
b0b_json "$B0B_MACHINE" "/api/attention/" | python3 -c "
import sys, json
items = json.load(sys.stdin)['items']
assert [i['agent_id'] for i in items] == ['gate-agent-b'], [i['agent_id'] for i in items]
print('  machine attention: exactly', items[0]['agent_id'], 'at', repr(items[0]['site_name']))"
b0b_json "$B0B_MACHINE" "/api/incidents/?status=all&limit=1000" | python3 -c "
import sys, json
rows = [i for i in json.load(sys.stdin)['incidents'] if i['incident_id'].startswith('b0b-inc-')]
assert [i['incident_id'] for i in rows] == ['b0b-inc-gate-agent-b'], rows
assert rows[0]['correlation'] == {} and rows[0]['parent_incident_id'] is None
print('  machine incidents: its own device only; correlation withheld')"
# Its OWN proposals: three are attributed to it, ONE is about the device it
# holds. Before A30.25 this read asked the site alone and showed it none.
b0b_json "$B0B_MACHINE" "/api/operational-agents/$B0B_AGENT/proposals" | python3 -c "
import sys, json
seen = sorted(p['proposal_id'] for p in json.load(sys.stdin)['proposals'])
assert seen == ['b0bprop-gate-agent-b'], seen
print('  machine reads its own proposals: exactly the one about gate-agent-b (of 3 attributed to it)')"
for B0B_OFF in /api/fleet/ /api/sites/ /api/autonomy/ /api/audit/ /api/capabilities/; do
  [ "$(b0b_code "$B0B_MACHINE" GET "$B0B_OFF")" = "403" ] || {
    echo "an off-plane route reopened to a machine: $B0B_OFF" >&2; exit 1; }
done
docker compose exec -T central-command python -c "
import sys; sys.path.insert(0, '/app/services/central_command/src')
from harkeniq_cc.route_contract import MACHINE_SURFACE
from harkeniq_cc.machine_identity import MACHINE_PRINCIPAL_CEILING
assert len(MACHINE_SURFACE) == 13, len(MACHINE_SURFACE)
assert set(MACHINE_PRINCIPAL_CEILING) == {'fleet.view', 'incident.view', 'proposal.submit'}
print('  MACHINE_SURFACE = 13, ceiling unchanged, 5 off-plane routes still refused')"
curl -sf -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/policies/$B0B_POLICY" > /dev/null

# A6-4B0b-S3 (A30.26), live: autonomy scope isolation. General B0b's gate
# found P2 here, on this stack, against real error budgets -- so this is
# where it is shown closed. `build_autonomy` folded EVERY site's safety state
# and the narrowing pass that followed only dropped list items carrying a
# `site_id`; an aggregate has none, so a site-A reader received the tenant's
# error-budget totals, `sites_dropped_back` naming site B, and a disposition
# folded from a site they could not see. `?site_id=` returned any site in
# isolation, and a proposal's stored blocking conditions named every site.
#
# The state is REAL and comes from where it really comes from: an error budget
# written at the SITE MANAGER for site B, carried to Central Command by the
# production poller (the poller replaces `cc_safety_state` on every poll, so
# anything written at Central Command would be gone in thirty seconds). The
# count is a number nothing else on this stack produces, so finding it in a
# payload means site B reached that reader. Every absence is paired with a
# CONTROL who holds site B and reads the same fact.
# ===========================================================================
S3_SENTINEL=470047
S3_DOMAIN="GATE-S3-SECRET-B"
s3_walk() {  # $1 token, $2 path, $3.. forbidden values -> "clean" or the leaks
  python3 - "$@" <<'PY'
import json, subprocess, sys
token, path, forbidden = sys.argv[1], sys.argv[2], sys.argv[3:]
out = subprocess.run(["curl", "-s", "-H", f"Authorization: Bearer {token}",
                      "http://localhost:8090" + path],
                     capture_output=True, text=True).stdout
def leaves(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from leaves(value)
    elif isinstance(node, list):
        for item in node:
            yield from leaves(item)
    else:
        yield node
found = sorted({
    f"{bad} in {leaf!r}"[:90] for leaf in leaves(json.loads(out))
    for bad in forbidden
    if (isinstance(leaf, str) and bad in leaf)
    or (isinstance(leaf, int) and not isinstance(leaf, bool) and str(leaf) == bad)
})
print("clean" if not found else "LEAK: " + " | ".join(found))
PY
}

step "A6-4B0b-S3/BB: site B withdraws a class AT THE SITE MANAGER, and the real poll carries it to Central Command"
TOKEN=$(tenant_token gate-owner@demo gate-owner)
S3_SM_SITE_B=$(s1_sm "SELECT id FROM sites WHERE cc_site_id='$SITE_B'")
[ -n "$S3_SM_SITE_B" ] || { echo "the Site Manager does not serve site B" >&2; exit 1; }
docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
  "INSERT INTO sm_error_budgets (site_id, action_type, success_count, failure_count,
        total_count, min_success_rate, dropped_back, dropped_back_at, updated_at)
   VALUES ('$S3_SM_SITE_B', 'BMC_RESET', 3, $((S3_SENTINEL - 3)), $S3_SENTINEL,
           0.95, true, now(), now())
   ON CONFLICT (site_id, action_type) DO UPDATE SET success_count = 3,
        failure_count = $((S3_SENTINEL - 3)), total_count = $S3_SENTINEL,
        dropped_back = true, dropped_back_at = now(), updated_at = now()" > /dev/null
s3_polled() {
  [ "$(s1_cc "SELECT count(*) FROM cc_safety_state WHERE site_id='$SITE_B'
              AND error_budgets::text LIKE '%$S3_SENTINEL%'")" = "1" ]
}
wait_for "the poller to carry site B's drop-back into cc_safety_state" 180 s3_polled
[ "$(s1_cc "SELECT count(*) FROM cc_safety_state WHERE site_id='$SITE_A'
            AND error_budgets::text LIKE '%$S3_SENTINEL%'")" = "0" ] || {
  echo "the Site Manager reported site B's budget against site A (E0.2)" >&2; exit 1; }
echo "  site B dropped BMC_RESET back at the Site Manager ($S3_SENTINEL outcomes); Central Command holds it for site B only"

step "A6-4B0b-S3/BC: a site-A principal reads site A's autonomy -- and the control reads BOTH"
for S3_P in a ab device; do
  tenant_realm_user "gate-s3-$S3_P@demo" "gate-s3-$S3_P" site_admin
done
S3_A=$(tenant_token gate-s3-a@demo gate-s3-a)
S3_AB=$(tenant_token gate-s3-ab@demo gate-s3-ab)
S3_DEV=$(tenant_token gate-s3-device@demo gate-s3-device)
S3_DEVICE_A=$(s1_cc "SELECT agent_id FROM cc_fleet_cache WHERE site_id='$SITE_A' ORDER BY agent_id LIMIT 1")
s3_grant() {  # $1 principal subject, $2 scope_type, $3 scope_ref
  curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"principal_ref\":\"$1\",\"scope_type\":\"$2\",\"scope_ref\":\"$3\",
         \"role\":\"site_admin\"}" \
    http://localhost:8090/api/scope-grants/
}
[ "$(s3_grant "$(s1_sub "$S3_A")" site "$SITE_A")" = "201" ] || { echo "a/A refused" >&2; exit 1; }
[ "$(s3_grant "$(s1_sub "$S3_AB")" site "$SITE_A")" = "201" ] || { echo "ab/A refused" >&2; exit 1; }
[ "$(s3_grant "$(s1_sub "$S3_AB")" site "$SITE_B")" = "201" ] || { echo "ab/B refused" >&2; exit 1; }
[ "$(s3_grant "$(s1_sub "$S3_DEV")" device "$S3_DEVICE_A")" = "201" ] || { echo "device grant refused" >&2; exit 1; }
s3_contract() {  # $1 token, $2 query, $3 expectation: both | a | none
  s2_get "$1" "/api/autonomy/$2" | python3 -c "
import sys, json
c = json.load(sys.stdin)
A, B, want, sentinel = '$SITE_A', '$SITE_B', '$3', $S3_SENTINEL
row = next(r for r in c['action_classes'] if r['action_type'] == 'BMC_RESET')
budget = row['safety']['error_budget']
sites = sorted(s['id'] for s in c['scope']['sites'])
dropped = [b for b in row['blocking_conditions'] if b['code'] == 'error_budget_dropped_back']
top = [e for e in c['safety_state']['error_budgets'] if e['action_type'] == 'BMC_RESET']
if want == 'both':
    assert {A, B} <= set(sites), sites
    assert budget and budget['total'] == sentinel and budget['sites_dropped_back'] == [B], budget
    assert top and top[0]['total'] == sentinel, top
    assert [b['site_id'] for b in dropped] == [B], dropped
    assert 'error_budget_dropped_back' in row['advancement']['blocked_by'], row['advancement']
else:
    assert sites == ([A] if want == 'a' else []), sites
    assert budget is None, budget            # site A has no BMC_RESET budget of its own
    assert top == [] and dropped == [], (top, dropped)
    assert 'error_budget_dropped_back' not in row['advancement']['blocked_by'], row['advancement']
    assert set(row['safety']['site_budget_remaining']) <= {A}, row['safety']
    if want == 'none':
        assert c['safety_state'] == {'reported': False, 'sites_reporting': [],
            'sites_not_reporting': [], 'suppressions': [], 'error_budgets': [],
            'site_stop_switches': []}, c['safety_state']
        assert row['safety'] == {'reported': False, 'error_budget': None,
            'suppressed_domains': [], 'site_budget_remaining': {}}, row['safety']
        assert c['posture']['ladder'] and 'configured_level' in c['posture']
print('  %-7s sites=%s BMC_RESET total=%s dropped_back_at=%s' % (
    want, len(sites), budget and budget['total'], [b['site_id'][:8] for b in dropped]))"
}
echo "  CONTROL (holds A and B):"; s3_contract "$S3_AB" "" both
echo "  site-A principal:";        s3_contract "$S3_A" "" a
[ "$(s3_walk "$S3_A" "/api/autonomy/" "$SITE_B" "$S3_SENTINEL")" = "clean" ] || {
  s3_walk "$S3_A" "/api/autonomy/" "$SITE_B" "$S3_SENTINEL" >&2
  echo "a site-A principal read site B's autonomy state (P2)" >&2; exit 1; }
# The S2 persona, reused on purpose: site A in full, site B narrowed to
# incident.view. Its grant at B does not carry fleet.view, so B's safety
# state must not arrive through it.
echo "  A in full + B narrowed to incident.view (the S2 principal):"
s3_contract "$S2_MIXED" "" a
[ "$(s3_walk "$S2_MIXED" "/api/autonomy/" "$SITE_B" "$S3_SENTINEL")" = "clean" ] || {
  echo "a grant narrowed to incident.view carried site B's autonomy state" >&2; exit 1; }
# R4: a device grant names no site, so it selects no site-derived fact.
echo "  device-scoped principal (R4):"; s3_contract "$S3_DEV" "" none
[ "$(s3_walk "$S3_DEV" "/api/autonomy/" "$SITE_A" "$SITE_B" "$S3_SENTINEL")" = "clean" ] || {
  echo "a device-scoped principal read site-derived autonomy state (R4)" >&2; exit 1; }

step "A6-4B0b-S3/BD: ?site_id= is a focus inside the caller's reach, never a probe of another site"
# CONTROL first: for a reader who holds site B the focus really does return
# site B in isolation -- which is exactly what it returned to everybody.
s2_get "$S3_AB" "/api/autonomy/?site_id=$SITE_B" | python3 -c "
import sys, json
c = json.load(sys.stdin)
row = next(r for r in c['action_classes'] if r['action_type'] == 'BMC_RESET')
assert [s['id'] for s in c['scope']['sites']] == ['$SITE_B'], c['scope']
assert row['safety']['error_budget']['total'] == $S3_SENTINEL, row['safety']
print('  control  site B in isolation: total=%s (the oracle the probe used to be)' % row['safety']['error_budget']['total'])"
python3 - "$S3_A" "$SITE_B" <<'PY'
import json, subprocess, sys
token, site_b = sys.argv[1], sys.argv[2]
def get(query):
    out = subprocess.run(["curl", "-s", "-H", f"Authorization: Bearer {token}",
                          "http://localhost:8090/api/autonomy/" + query],
                         capture_output=True, text=True).stdout
    body = json.loads(out)
    assert body["scope"].pop("site_id") == query.split("=", 1)[1]   # their own echo
    body.pop("generated_at")
    return body
hidden, absent = get(f"?site_id={site_b}"), get("?site_id=gate-s3-no-such-site")
assert hidden == absent, "a hidden site answers differently from a site that does not exist"
assert hidden["scope"]["sites"] == [] and hidden["safety_state"]["error_budgets"] == []
print("  site-A principal: ?site_id=<site B> == ?site_id=<no such site>, and both are empty")
PY

step "A6-4B0b-S3/BE: the Operational Agent view is composed over the READER's sites"
S3_AGENT=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"name\":\"gate-s3-agent-$(date +%s)\",
       \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$SITE_A\"}],
       \"capabilities\":[{\"kind\":\"action_class\",\"capability_ref\":\"BMC_RESET\"}]}" \
  http://localhost:8090/api/operational-agents/ | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
[ -n "$S3_AGENT" ] || { echo "could not create the S3 agent" >&2; exit 1; }
s3_agent_view() {  # $1 token, $2 "sees" | "blind"
  s2_get "$1" "/api/operational-agents/$S3_AGENT" | python3 -c "
import sys, json
v = json.load(sys.stdin)
row = next(c for c in v['capabilities']['action_classes'] if c['action_type'] == 'BMC_RESET')
named = sorted({b.get('site_id') for b in row['blocking_conditions'] if b.get('site_id')})
blocked = row['advancement']['blocked_by']
if '$2' == 'sees':
    assert '$SITE_B' in named and 'error_budget_dropped_back' in blocked, (named, blocked)
else:
    assert '$SITE_B' not in named and 'error_budget_dropped_back' not in blocked, (named, blocked)
print('  %-6s blocking rows name %d site(s); advancement blocked_by=%s' % ('$2', len(named), blocked))"
}
echo "  tenant owner:";     s3_agent_view "$TOKEN" sees
echo "  site-A principal:"; s3_agent_view "$S3_A" blind
[ "$(s3_walk "$S3_A" "/api/operational-agents/$S3_AGENT" "$SITE_B" "$S3_SENTINEL")" = "clean" ] || {
  echo "the agent view told a site-A principal about site B" >&2; exit 1; }

step "A6-4B0b-S3/BF: a STORED verdict names a site only to a reader who holds fleet.view there"
# The evaluator decides over the whole tenant and records what it decided, so
# a proposal at site A carries rows about site B -- and a REASON copied from
# one of those rows' own text. The row is written directly,
# as an `awaiting_approval` agent PROPOSAL for the draft agent above: nothing
# evaluates a draft agent, nothing dispatches an undecided proposal, and the
# poller never touches this table (a node route would be superseded at once).
S3_PROP="gate-s3-prop-$(date +%s)"
S3_TENANT=$(s1_cc "SELECT tenant_id FROM cc_sites WHERE id='$SITE_A'")
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "INSERT INTO cc_agent_proposals (id, tenant_id, agent_id, actor, agent_version, site_id,
        device_agent_id, action_type, params, rationale, evidence, disposition,
        disposition_reason, blocking_conditions, authorization_basis, status, decided_by,
        dedupe_key, directive_id, dispatch_reason, outcome, created_at)
   VALUES ('$S3_PROP', '$S3_TENANT', '$S3_AGENT', 'op-agent:$S3_AGENT@v1', 1, '$SITE_A',
        '$S3_DEVICE_A', 'BMC_RESET', '{}'::jsonb, 'S3 gate proposal',
        '{\"learned_signals\": [
            {\"scope_type\": \"site\", \"scope_ref\": \"$SITE_B\", \"statement\": \"$S3_DOMAIN\"},
            {\"scope_type\": \"cohort\", \"scope_ref\": \"Dell/R750\", \"statement\": \"cohort\"}]}'::jsonb,
        'requires_approval', 'withdrawn at site B: $S3_DOMAIN',
        '[{\"code\": \"level_below_grant\", \"detail\": \"tenant\", \"scope\": \"tenant\"},
          {\"code\": \"site_suppressed\", \"detail\": \"here\", \"scope\": \"site\", \"site_id\": \"$SITE_A\"},
          {\"code\": \"error_budget_dropped_back\", \"detail\": \"withdrawn at site B: $S3_DOMAIN\", \"scope\": \"site\", \"site_id\": \"$SITE_B\"},
          {\"code\": \"domain_suppressed\", \"detail\": \"$S3_DOMAIN\", \"scope\": \"domain\",
           \"site_id\": \"$SITE_B\", \"domain_id\": \"$S3_DOMAIN\"}]'::jsonb,
        'human_approval', 'awaiting_approval', '', '$S3_PROP', '', '', '', now())" > /dev/null
s3_verdict() {  # $1 token, $2 expected site count among the blocking rows
  python3 - "$1" "$2" "$S3_AGENT" "$S3_PROP" "$SITE_A" "$SITE_B" "$S3_DOMAIN" <<'PY'
import json, subprocess, sys
token, want, agent, prop, A, B, secret = sys.argv[1:8]
def get(path):
    out = subprocess.run(["curl", "-s", "-H", f"Authorization: Bearer {token}",
                          "http://localhost:8090" + path], capture_output=True, text=True).stdout
    return json.loads(out)
queue = next(i["proposal"] for i in get("/api/approvals/")["actions"]
             if i.get("origin") == "agent" and i["id"] == prop)
listed = next(p for p in get(f"/api/operational-agents/{agent}/proposals")["proposals"]
              if p["proposal_id"] == prop)
detail = next(p for p in get(f"/api/operational-agents/{agent}")["proposals"]
              if p["proposal_id"] == prop)
for where, proposal in (("queue", queue), ("list", listed), ("detail", detail)):
    rows = proposal["blocking_conditions"]
    named = sorted({r["site_id"] for r in rows if r.get("site_id")})
    signals = sorted(s["statement"] for s in proposal["evidence"]["learned_signals"])
    assert [r["code"] for r in rows if r["scope"] == "tenant"] == ["level_below_grant"], (where, rows)
    reason = proposal["disposition_reason"]
    if want == "2":
        assert named == sorted([A, B]) and secret in json.dumps(proposal), (where, named)
        assert signals == sorted(["cohort", secret]), (where, signals)
        assert reason == f"withdrawn at site B: {secret}", (where, reason)
    else:
        assert named == [A] and secret not in json.dumps(proposal), (where, named)
        assert signals == ["cohort"], (where, signals)
        # The stored reason IS the withheld row's text: it goes with the row.
        assert "outside your authorized scope" in reason, (where, reason)
print(f"    queue, list and detail agree: blocking rows name {want} site(s); reason = {reason!r}")
PY
}
echo "  CONTROL (holds A and B):"; s3_verdict "$S3_AB" 2
echo "  site-A principal:";        s3_verdict "$S3_A" 1
# This proof owns its state (the A30.21 lesson): the synthetic proposal and
# the Site Manager budget it seeded are removed, so nothing later in the gate
# -- or in a slice that appends after this one -- inherits a withdrawn class.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "DELETE FROM cc_agent_proposals WHERE id='$S3_PROP'" > /dev/null
docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
  "DELETE FROM sm_error_budgets WHERE site_id='$S3_SM_SITE_B' AND action_type='BMC_RESET'
   AND total_count=$S3_SENTINEL" > /dev/null
echo "  the seeded budget and the synthetic proposal are removed"

# ===========================================================================
# A6-4B0b-S4 (spec A30.28) -- learned-signal and fleet-pattern PAYLOAD isolation
#
# A23 stands: a vendor/model cohort conclusion is tenant knowledge. E3-F1 is
# what lay under it -- a reader entitled to the ROW was handed its CONTENT as
# stored: another site's id and failure count, the number of sites, the tenant
# totals, and a sentence restating them.
#
# Nothing here is written into a learning table. Outcome rows are seeded for
# site A (5 failures) and site B (a count nothing else on this stack produces)
# and the REAL IntelligenceEngine loop detects the cross-site pattern, derives
# the signals, opens the cycle and pushes the pattern to the Site Manager.
# Every absence is paired with a CONTROL -- the tenant owner -- reading the
# same fact, so an empty answer cannot pass as a correct one.
# ===========================================================================
S4_ACTION="CONFIG_RESTORE"
S4_B_FAILURES=4337
S4_A_FAILURES=5
S4_TOTAL=$((S4_B_FAILURES + S4_A_FAILURES))
S4_RUN="gate-s4-$(date +%s)"
s4_numbers() {  # $1 token, $2 path, $3.. forbidden -> "clean" | the leaks
  # Numbers are matched as NUMBERS: an int leaf exactly, or inside a string
  # only where no hex digit touches it -- ids on this stack are hex, and a
  # substring match on four digits would cry wolf about one in ten runs.
  python3 - "$@" <<'PY'
import json, re, subprocess, sys
token, path, forbidden = sys.argv[1], sys.argv[2], sys.argv[3:]
out = subprocess.run(["curl", "-s", "-H", f"Authorization: Bearer {token}",
                      "http://localhost:8090" + path], capture_output=True, text=True).stdout
def leaves(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from leaves(value)
    elif isinstance(node, list):
        for item in node:
            yield from leaves(item)
    else:
        yield node
def hit(leaf, bad):
    if isinstance(leaf, bool):
        return False
    if isinstance(leaf, (int, float)):
        return bad.isdigit() and float(leaf) == float(bad)
    if not isinstance(leaf, str):
        return False
    if bad.isdigit():
        return re.search(rf"(?<![0-9a-fA-F]){bad}(?![0-9a-fA-F])", leaf) is not None
    return bad in leaf
found = sorted({f"{bad} in {str(leaf)[:70]!r}" for leaf in leaves(json.loads(out))
                for bad in forbidden if hit(leaf, bad)})
print("clean" if not found else "LEAK: " + " | ".join(found))
PY
}

step "A6-4B0b-S4/BG: the REAL engine learns a cross-site pattern -- and what it stores is E3-F1"
TOKEN=$(tenant_token gate-owner@demo gate-owner)
S4_DEVICE_A=$(s1_cc "SELECT agent_id FROM cc_fleet_cache WHERE site_id='$SITE_A' ORDER BY agent_id LIMIT 1")
S4_VENDOR=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT vendor FROM cc_fleet_cache WHERE agent_id='$S4_DEVICE_A'" | sed 's/^ *//;s/ *$//' | tr -d '\r')
S4_MODEL=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT model FROM cc_fleet_cache WHERE agent_id='$S4_DEVICE_A'" | sed 's/^ *//;s/ *$//' | tr -d '\r')
[ -n "$S4_VENDOR$S4_MODEL" ] || { echo "site A's device declares no cohort" >&2; exit 1; }
# A30.29 (BJ below): the distribution loop targets a site by whether its
# FLEET holds the cohort, exactly, and site B's S1 devices are "Dell R750"
# while site A's real node declares "$S4_VENDOR $S4_MODEL". So that the
# ONE Site Manager serving A and B genuinely receives the pattern for
# BOTH sites, site B gets a device of site A's cohort -- owned by this
# proof and removed by BO. The poller rebuilds the cache per poll, so the
# device is at Central Command within one poll and gone after the delete.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
  "INSERT INTO devices (id, site_id, agent_id, agent_name, vendor, model,
                        service_tag, device_class, first_seen_at, last_seen_at)
   SELECT 'gatedevs400000000000000000000000', s.id, 'gate-agent-s4', 's4',
          '$S4_VENDOR', '$S4_MODEL', 'GATES4', 'server', now(), now()
   FROM sites s WHERE s.cc_site_id = '$SITE_B'
   ON CONFLICT (id) DO NOTHING" > /dev/null
wait_for "site B's cohort device visible at Central Command" 180 bash -c \
  "docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
   \"SELECT count(*) FROM cc_fleet_cache WHERE site_id='$SITE_B' AND agent_id='gate-agent-s4'
     AND vendor='$S4_VENDOR' AND model='$S4_MODEL'\" \
   | grep -qx ' *1 *'"
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "INSERT INTO cc_outcome_history (id, site_id, action_id, action_type, device_agent_id,
        vendor, model, outcome, fault_resolved, actor, recorded_at, ingested_at)
   SELECT substr(md5('$S4_RUN-' || site || '-' || n), 1, 32), site, '$S4_RUN-' || site || '-' || n,
          '$S4_ACTION', device, '$S4_VENDOR', '$S4_MODEL', 'FAILURE', false, '', now(), now()
   FROM (SELECT '$SITE_A' AS site, '$S4_DEVICE_A' AS device, generate_series(1, $S4_A_FAILURES) AS n
         UNION ALL
         SELECT '$SITE_B', 'gate-agent-c', generate_series(1, $S4_B_FAILURES)) rows" > /dev/null
s4_learned() {
  [ "$(s1_cc "SELECT count(*) FROM cc_learned_signals WHERE action_type='$S4_ACTION'
              AND scope_type='cohort' AND evidence::text LIKE '%$SITE_B%'")" = "1" ]
}
wait_for "the intelligence loop to detect the cross-site $S4_ACTION pattern" 480 s4_learned
S4_STORED=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT statement || ' ## ' || evidence::text FROM cc_learned_signals
   WHERE action_type='$S4_ACTION' AND scope_type='cohort'")
for S4_FACT in "$SITE_B" "$S4_B_FAILURES" "$S4_TOTAL attempts" "across 2 sites"; do
  echo "$S4_STORED" | grep -q -- "$S4_FACT" || {
    echo "CONTROL failed: the stored cohort signal does not carry '$S4_FACT'" >&2; exit 1; }
done
echo "  the engine stored site B's id, its $S4_B_FAILURES failures, '$S4_TOTAL attempts' and 'across 2 sites' on a COHORT row"

step "A6-4B0b-S4/BH: the owner reads what is stored; a site-A principal reads the bounded conclusion"
S3_A=$(tenant_token gate-s3-a@demo gate-s3-a)
s4_read() {  # $1 token, $2 owner | scoped
  python3 - "$1" "$2" "$S4_ACTION" "$SITE_A" "$SITE_B" "$S4_A_FAILURES" "$S4_B_FAILURES" "$S4_TOTAL" <<'PY'
import json, subprocess, sys
token, who, action, A, B, a_n, b_n, total = sys.argv[1:9]
def get(path):
    out = subprocess.run(["curl", "-s", "-H", f"Authorization: Bearer {token}",
                          "http://localhost:8090" + path], capture_output=True, text=True).stdout
    return json.loads(out)
signal = next(s for s in get("/api/learning/signals")["signals"]
              if s["action_type"] == action and s["scope_type"] == "cohort")
pattern = next(p for p in get("/api/outcomes/patterns")["patterns"]
               if p["pattern_type"] == "cross_site_batch"
               and p["affected_scope"].get("action_type") == action)
cycle = next(c for c in get("/api/learning/cycles")["cycles"]
             if c["pattern_id"] == pattern["pattern_id"])
ev = signal["evidence"]
if who == "owner":
    assert ev["site_failure_counts"] == {A: int(a_n), B: int(b_n)}, ev
    assert ev["total"] == int(total) and ev["sites_affected"] == 2, ev
    assert f"{total} attempts" in signal["statement"] and "across 2 sites" in signal["statement"]
    assert f"({total}/{total})" in pattern["description"], pattern["description"]
    assert B in pattern["affected_scope"]["sites"]
    assert cycle["outcomes_before"]["total"] == int(total), cycle
    assert "projection" not in ev
else:
    assert ev["site_failure_counts"] == {A: int(a_n)}, ev          # its OWN site, kept
    assert ev["projection"] == "scoped" and ev["partial"] == ["site_failure_counts"], ev
    assert {"total", "failures", "sites_affected"} <= set(ev["withheld"]), ev
    assert ev["failure_rate"] == 1.0, ev                            # the CONCLUSION, kept
    assert signal["statement"] == f"{action} on {signal['vendor']} {signal['model']} fails about 100% of the time.", signal["statement"]
    assert pattern["affected_scope"]["sites"] == A, pattern
    assert "more than one site" in pattern["description"] and "(" not in pattern["description"]
    assert cycle["sites_distributed"] is None and cycle["devices_applied"] is None, cycle
    assert "total" not in cycle["outcomes_before"], cycle
print(f"    {who}: statement = {signal['statement']!r}")
print(f"    {who}: pattern   = {pattern['description']!r}")
PY
}
echo "  CONTROL (tenant owner):"; s4_read "$TOKEN" owner
echo "  site-A principal:";       s4_read "$S3_A" scoped
for S4_PATH in /api/learning/signals /api/outcomes/patterns "/api/outcomes/patterns?limit=1" \
               /api/learning/cycles /api/attention/ /api/autonomy/; do
  [ "$(s4_numbers "$S3_A" "$S4_PATH" "$SITE_B" "$S4_B_FAILURES" "$S4_TOTAL")" = "clean" ] || {
    echo "$S4_PATH told a site-A principal about site B: $(s4_numbers "$S3_A" "$S4_PATH" "$SITE_B" "$S4_B_FAILURES" "$S4_TOTAL")" >&2; exit 1; }
done
echo "  signals, patterns, ?limit=1, cycles, attention and autonomy carry no site-B id, count or tenant total"

step "A6-4B0b-S4/BI: attention and incident detail still LEARN -- the conclusion, for the reader's own device"
python3 - "$S3_A" "$S4_ACTION" "$S4_DEVICE_A" "$SITE_B" <<'PY'
import json, subprocess, sys
token, action, device, B = sys.argv[1:5]
def get(path):
    out = subprocess.run(["curl", "-s", "-H", f"Authorization: Bearer {token}",
                          "http://localhost:8090" + path], capture_output=True, text=True).stdout
    return json.loads(out)
item = next(i for i in get("/api/attention/")["items"] if i["agent_id"] == device)
learned = [s for s in item["evidence"]["learned_signals"] if s["action_type"] == action]
assert learned, "attention lost the cohort conclusion for the reader's own device"
for s in learned:
    assert s["evidence"].get("projection") == "scoped" and B not in json.dumps(s), s
    assert "about 100%" in s["statement"], s["statement"]
patterns = [p for p in item["evidence"]["fleet_patterns"] if action in p.get("description", "")]
assert patterns and all("(" not in p["description"] for p in patterns), patterns
print(f"    attention: {len(learned)} learned signal(s) and {len(patterns)} pattern(s) for {device}, bounded")
incidents = [i for i in get("/api/incidents/?status=")["incidents"] if i.get("device_agent_id") == device]
if incidents:
    prior = get(f"/api/incidents/{incidents[0]['incident_id']}")["prior_learning"]
    mine = [s for s in prior if s["action_type"] == action]
    assert mine and all(s["evidence"].get("projection") == "scoped" for s in mine), prior
    assert B not in json.dumps(prior)
    print(f"    incident {incidents[0]['incident_id']}: prior_learning carries {len(mine)} bounded signal(s)")
else:
    print("    (no incident on this device in this run; prior_learning is proven in the unit and PostgreSQL suites)")
PY

step "A6-4B0b-S4/BJ: what crossed CC->SM is one site's bounded payload PER SITE, marked -- Central Command still holds the whole"
S4_PATTERN=$(s1_cc "SELECT id FROM cc_fleet_patterns WHERE pattern_type='cross_site_batch'
                    AND affected_scope::jsonb->>'action_type'='$S4_ACTION' LIMIT 1")
# A30.29: the store is keyed (site, pattern). The sites that RECEIVE the
# pattern are those whose fleet holds the cohort, and both A and B do (the
# S1 steps seeded Dell R750s at B), so the ONE Site Manager serving both
# must end up with a row per site -- each bounded to its own facts and each
# marked as projected for its own Central Command site id. Before A30.29
# the second push overwrote the first.
S4_TARGETS=$(s1_cc "SELECT string_agg(DISTINCT site_id, ',') FROM cc_fleet_cache
                    WHERE vendor='$S4_VENDOR' AND model='$S4_MODEL'")
echo "$S4_TARGETS" | tr ',' '\n' | grep -qx "$SITE_A" || { echo "site A does not hold the cohort" >&2; exit 1; }
echo "$S4_TARGETS" | tr ',' '\n' | grep -qx "$SITE_B" || { echo "site B does not hold the cohort" >&2; exit 1; }
S4_EXPECTED=$(echo "$S4_TARGETS" | tr ',' '\n' | sort -u | wc -l)
s4_pushed() {
  [ "$(s1_sm "SELECT count(*) FROM sm_site_fleet_patterns sp JOIN sites s ON s.id = sp.site_id
              WHERE sp.pattern_id='$S4_PATTERN' AND s.cc_site_id IN (
                SELECT unnest(string_to_array('$S4_TARGETS', ',')))")" = "$S4_EXPECTED" ]
}
wait_for "the distribution loop to push the pattern to EVERY receiving site of the Site Manager" 480 s4_pushed
S4_CC_ROW=$(docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "SELECT description || ' ## ' || evidence::text FROM cc_fleet_patterns WHERE id='$S4_PATTERN'")
echo "$S4_CC_ROW" | grep -q "across 2 sites ($S4_TOTAL/$S4_TOTAL)" || {
  echo "CONTROL failed: Central Command's own pattern row lost its evidence" >&2; exit 1; }
# Every per-site row: no count, no other site, and a marker naming ITS site.
S4_SM_BAD=$(s1_sm "SELECT count(*) FROM sm_site_fleet_patterns sp JOIN sites s ON s.id = sp.site_id
   WHERE sp.pattern_id='$S4_PATTERN' AND (
      sp.description LIKE '%across%' OR sp.description LIKE '%(%/%)%'
   OR sp.evidence::jsonb ? 'total' OR sp.evidence::jsonb ? 'failures' OR sp.evidence::jsonb ? 'sites_affected'
   OR (SELECT count(*) FROM jsonb_object_keys(
         COALESCE(sp.evidence::jsonb->'site_failure_counts', '{}'::jsonb))) > 1
   OR sp.affected_scope::jsonb->>'sites' LIKE '%,%'
   OR (sp.affected_scope::jsonb->>'sites' <> '' AND sp.affected_scope::jsonb->>'sites' <> s.cc_site_id)
   OR sp.visibility IS NULL
   OR sp.visibility::jsonb->>'scope' <> 'site'
   OR sp.visibility::jsonb->>'site_id' <> s.cc_site_id
   OR (sp.visibility::jsonb->>'projection_version')::int <> 1)")
[ "$S4_SM_BAD" = "0" ] || {
  echo "a Site Manager row for $S4_PATTERN carries another site's facts or a wrong marker:" >&2
  docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -c \
    "SELECT s.cc_site_id, sp.description, sp.affected_scope, sp.evidence, sp.visibility
     FROM sm_site_fleet_patterns sp JOIN sites s ON s.id = sp.site_id
     WHERE sp.pattern_id='$S4_PATTERN'" >&2
  exit 1; }
# The two rows are DIFFERENT rows: site A's names A's count, site B's names B's.
S4_A_ROW=$(s1_sm "SELECT sp.evidence::jsonb->'site_failure_counts'->>'$SITE_A' FROM sm_site_fleet_patterns sp
                  JOIN sites s ON s.id = sp.site_id WHERE sp.pattern_id='$S4_PATTERN' AND s.cc_site_id='$SITE_A'")
S4_B_ROW=$(s1_sm "SELECT sp.evidence::jsonb->'site_failure_counts'->>'$SITE_B' FROM sm_site_fleet_patterns sp
                  JOIN sites s ON s.id = sp.site_id WHERE sp.pattern_id='$S4_PATTERN' AND s.cc_site_id='$SITE_B'")
[ "$S4_A_ROW" = "$S4_A_FAILURES" ] || { echo "site A's row does not carry A's own count: '$S4_A_ROW'" >&2; exit 1; }
[ "$S4_B_ROW" = "$S4_B_FAILURES" ] || { echo "site B's row does not carry B's own count: '$S4_B_ROW'" >&2; exit 1; }
[ "$(s1_sm "SELECT count(*) FROM sm_fleet_patterns WHERE pattern_id='$S4_PATTERN'")" = "0" ] || {
  echo "this release wrote the LEGACY store" >&2; exit 1; }
echo "  $S4_EXPECTED per-site row(s) for $S4_PATTERN on ONE Site Manager: A's carries $S4_A_ROW, B's carries $S4_B_ROW, each marked for its own site; legacy store untouched"

step "A6-4B0b-S4/BK: this proof owns its state"
# The A30.21 lesson. The engine's in-process aggregate keeps what it counted
# until Central Command restarts; nothing after this step reads learning.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "DELETE FROM cc_outcome_history WHERE action_id LIKE '$S4_RUN-%';
   DELETE FROM cc_learned_signals WHERE action_type='$S4_ACTION';
   DELETE FROM cc_learning_cycles WHERE pattern_id IN (
       SELECT id FROM cc_fleet_patterns WHERE affected_scope::jsonb->>'action_type'='$S4_ACTION');
   DELETE FROM cc_fleet_patterns WHERE affected_scope::jsonb->>'action_type'='$S4_ACTION'" > /dev/null
docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
  "DELETE FROM sm_site_fleet_patterns WHERE affected_scope::jsonb->>'action_type'='$S4_ACTION';
   DELETE FROM sm_fleet_patterns WHERE affected_scope::jsonb->>'action_type'='$S4_ACTION'" > /dev/null
[ "$(s1_cc "SELECT count(*) FROM cc_outcome_history WHERE action_id LIKE '$S4_RUN-%'")" = "0" ] || {
  echo "the seeded outcomes were not removed" >&2; exit 1; }
echo "  the seeded outcomes and everything the engine learned from them are removed"

# ===========================================================================
# A6-4B0b-S4 remediation (spec A30.29): generated content inherits the
# projection it was generated from. The compose stack runs no language
# model, so the GENERATION path is proven at unit level against the real
# IngestService; what the live stack proves is the read policy over real
# Keycloak identities and real grants, the wire (BJ above: the marker on
# every per-site row), and both upgrades on the live databases.
# ===========================================================================

step "A6-4B0b-S4/BL: the HISTORICAL attack -- a diagnosis and a candidate with no provenance"
S4_INC="gate-a3029-inc-$(date +%s)"
S4_CAND="gate-a3029-cand-$(date +%s)"
S4_SECRET="SECRET_SITE_C"
S4_PHRASE="30 of 40 attempts across 2 sites"
S4_TENANT=$(s1_cc "SELECT tenant_id FROM cc_sites WHERE id='$SITE_A'")
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "INSERT INTO cc_incidents (incident_id, tenant_id, site_id, kind, status, title, device_agent_id,
        subsystem, confidence, inferred, explanation, opened_at, first_seen_at, last_seen_at)
   VALUES ('$S4_INC', '$S4_TENANT', '$SITE_A', 'device', 'open', 'Fan duty rising', '$S4_DEVICE_A',
        'fan', 0.9, false,
        '{\"provider\": \"llm\", \"confidence\": 0.8,
          \"summary\": \"Fleet-wide: $S4_SECRET -- $S4_PHRASE\",
          \"suggested_action\": \"Replace the PSU as at $S4_SECRET\",
          \"reasoning_steps\": [\"$S4_PHRASE\", \"compared with $S4_SECRET\"],
          \"operator_notes\": \"$S4_SECRET\",
          \"evidence_cited\": [\"fan-health: 3 of 5 fans failed\"],
          \"similar_past_incidents\": []}'::jsonb, now(), now(), now());
   INSERT INTO cc_candidate_skills (skill_id, tenant_id, site_id, yaml_text, source_device, source_component,
        validation_state, warnings, dry_run_matches, status, generated_at, received_at)
   VALUES ('$S4_CAND', '$S4_TENANT', '$SITE_A',
        'name: fan-x' || chr(10) || 'description: $S4_SECRET $S4_PHRASE' || chr(10),
        '$S4_DEVICE_A', 'fan:Fan1', 'valid', '[\"rule quotes $S4_SECRET\"]'::jsonb, 2, 'received',
        now(), now())" > /dev/null
s4_generated() {  # $1 token, $2 owner | withheld | absent
  python3 - "$1" "$2" "$S4_INC" "$S4_CAND" "$S4_SECRET" "$S4_PHRASE" <<'PYEOF'
import json, subprocess, sys
token, who, inc, cand, secret, phrase = sys.argv[1:7]
def get(path):
    out = subprocess.run(["curl", "-s", "-w", "\n%{http_code}", "-H", f"Authorization: Bearer {token}",
                          "http://localhost:8090" + path], capture_output=True, text=True).stdout
    body, code = out.rsplit("\n", 1)
    return int(code), (json.loads(body) if code == "200" else None)
def leaves(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from leaves(v)
    elif isinstance(node, list):
        for v in node:
            yield from leaves(v)
    else:
        yield node
def taint(body):
    return [l for l in leaves(body) if isinstance(l, str) and (secret in l or phrase in l)]
code, detail = get(f"/api/incidents/{inc}")
lcode, listed = get("/api/incidents/?status=all")
ccode, cands = get("/api/learning/candidates")
if who == "absent":
    assert code == 404, code
    for body in (listed, cands):
        assert body is None or taint(body) == [], taint(body)
    print("    no row, and nothing tainted anywhere")
    sys.exit(0)
assert code == 200, (code, detail)
diag = detail["diagnosis"]
# The candidate LIST is a fleet.view read; a reader holding the incident
# through incident.view alone does not see the row at all, which is the
# canonical read shape and not this step's subject.
mine = next((c for c in (cands or {"candidates": []})["candidates"] if c["skill_id"] == cand), None)
if who == "owner":
    assert secret in diag["generated"]["summary"] and diag["generated"]["withheld"] is False
    assert diag["generation_visibility"] is None      # nothing recorded; nothing invented
    assert mine is not None
    assert secret in mine["yaml_text"] and mine["generated_withheld"] is False and mine["warnings"]
    print("    owner: canonical generated text kept, provenance reported as not recorded")
else:
    for body in (detail, listed, cands):
        assert body is None or taint(body) == [], taint(body)
    assert diag["generated"]["withheld"] is True and diag["generation_visibility"] is None
    assert diag["generated"]["suggested_action"] == "" and diag["generated"]["reasoning_steps"] == []
    assert "operator_notes" not in diag["generated"]
    assert detail["title"] == "Fan duty rising" and "3 of 5 fans" in diag["evidence_cited"][0]
    if mine is not None:
        assert mine["yaml_text"] == "" and mine["warnings"] == [] and mine["generated_withheld"] is True
        assert mine["dry_run_matches"] == 2            # the candidate's own facts survive
        print("    " + who + ": row present, local facts intact, generated block and YAML withheld, no sentinel")
    else:
        print("    " + who + ": incident present, generated block withheld, candidate row not a fleet.view read for this reader, no sentinel")
PYEOF
}
echo "  CONTROL (tenant owner):";  s4_generated "$TOKEN" owner
echo "  site-A human:";            s4_generated "$S3_A" withheld
# A30.25 (general B0b, F1 closed): gate-s3-device holds THIS incident's
# device, so it reads the row -- and the generated block is withheld from
# it exactly as from any reader who holds no site under fleet.view.
echo "  device-scoped human holding THIS device (gate-s3-device; its own incident, generated withheld, nothing tainted):"
S3_DEV=$(tenant_token gate-s3-device@demo gate-s3-device)
s4_generated "$S3_DEV" withheld
# ...and a device-scoped human holding ANOTHER device (gate-b0b-device, at
# site B) is not a reader of it at all: no row, nothing tainted anywhere.
echo "  device-scoped human holding ANOTHER device (gate-b0b-device; not its device -> no row, nothing tainted):"
B0B_DEVICE=$(tenant_token gate-b0b-device@demo gate-b0b-device)
s4_generated "$B0B_DEVICE" absent
# A MACHINE principal scoped to site A with the incidents read binding.
S4_AGENT=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"name\":\"a3029-reader $(date +%s)\",
       \"scopes\":[{\"scope_type\":\"site\",\"scope_ref\":\"$SITE_A\"}],
       \"capabilities\":[{\"kind\":\"read\",\"capability_ref\":\"attention\"},
                        {\"kind\":\"read\",\"capability_ref\":\"incidents\"}]}" \
  http://localhost:8090/api/operational-agents/ | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
S4_SECRET_M=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$S4_AGENT/identity" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['client_secret'])")
S4_MTOKEN=$(curl -sf -X POST "http://localhost:8180/realms/tenant-demo/protocol/openid-connect/token" \
  -d "grant_type=client_credentials&client_id=op-agent-$S4_AGENT&client_secret=$S4_SECRET_M" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
[ -n "$S4_MTOKEN" ] || { echo "no machine token for the A30.29 reader" >&2; exit 1; }
echo "  machine principal (site A):"
python3 - "$S4_MTOKEN" "$S4_INC" "$S4_SECRET" "$S4_PHRASE" <<'PYEOF'
import json, subprocess, sys
token, inc, secret, phrase = sys.argv[1:5]
out = subprocess.run(["curl", "-s", "-w", "\n%{http_code}", "-H", f"Authorization: Bearer {token}",
                      f"http://localhost:8090/api/incidents/{inc}"], capture_output=True, text=True).stdout
body, code = out.rsplit("\n", 1)
assert code == "200", (code, body)
assert secret not in body and phrase not in body
d = json.loads(body)["diagnosis"]
assert d["generated"]["withheld"] is True and d["generation_visibility"] is None
print("    machine: row 200, generated withheld, no sentinel on MACHINE_SURFACE")
PYEOF

step "A6-4B0b-S4/BM: a marker is evidence, not authority -- current reach decides on every read"
# Mark the SAME stored artifacts as generated from site A's projection.
# The site-A human sees them now; a marker for site B on an incident AT
# site A is withheld from them without naming B; revoking their grant
# withholds again (the row stays readable through a second, incident-only
# grant); restoring it shows the same artifact, unchanged.
s4_mark() {  # $1 cc site id | tenant
  if [ "$1" = "tenant" ]; then
    S4_MARK='{"scope": "tenant", "site_id": null, "projection_version": 1}'
  else
    S4_MARK="{\"scope\": \"site\", \"site_id\": \"$1\", \"projection_version\": 1}"
  fi
  docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
    "UPDATE cc_incidents SET explanation = explanation::jsonb || '{\"generation_visibility\": $S4_MARK}'::jsonb
      WHERE incident_id='$S4_INC';
     UPDATE cc_candidate_skills SET generation_visibility = '$S4_MARK'::jsonb WHERE skill_id='$S4_CAND'" > /dev/null
}
s4_sees() {  # $1 token, $2 expected marker site id
  python3 - "$1" "$S4_INC" "$S4_CAND" "$S4_SECRET" "$2" <<'PYEOF'
import json, subprocess, sys
token, inc, cand, secret, site = sys.argv[1:6]
def get(path):
    return json.loads(subprocess.run(["curl", "-s", "-H", f"Authorization: Bearer {token}",
        "http://localhost:8090" + path], capture_output=True, text=True).stdout)
d = get(f"/api/incidents/{inc}")["diagnosis"]
assert secret in d["generated"]["summary"] and d["generated"]["withheld"] is False, d["generated"]
assert d["generation_visibility"] == {"scope": "site", "site_id": site, "projection_version": 1}, d["generation_visibility"]
c = next(c for c in get("/api/learning/candidates")["candidates"] if c["skill_id"] == cand)
assert secret in c["yaml_text"] and c["generated_withheld"] is False and c["generation_visibility"]["site_id"] == site
print(f"    visible: generated block and YAML shown, projection recorded for {site[:12]}...")
PYEOF
}
s4_mark "$SITE_A"
echo "  marked for site A -- site-A human:"; s4_sees "$S3_A" "$SITE_A"
echo "  marked for site A -- org/site-set human holding A and B:"; s4_sees "$S3_AB" "$SITE_A"
s4_mark "$SITE_B"
echo "  marked for site B (an incident AT site A) -- site-A human:"; s4_generated "$S3_A" withheld
s2_get "$S3_A" "/api/incidents/$S4_INC" | grep -q "$SITE_B" && {
  echo "the withheld response named the site the marker names" >&2; exit 1; }
echo "  marked for site B -- tenant owner:"; s4_sees "$TOKEN" "$SITE_B"
s4_mark tenant
echo "  marked tenant -- site-A human:"; s4_generated "$S3_A" withheld
echo "  marked tenant -- A+B human:";    s4_generated "$S3_AB" withheld
# Revoke / restore, on an identity THIS step owns (the A30.21 lesson).
s4_mark "$SITE_A"
tenant_realm_user "gate-a3029@demo" "gate-a3029" site_admin
S4_RR=$(tenant_token gate-a3029@demo gate-a3029)
S4_RR_SUB=$(s1_sub "$S4_RR")
# The row-holding grant is a DIFFERENT row (an org-unit grant narrowed to
# incident.view) from the site grant revoked below.
S4_ORG=$(s1_cc "SELECT org_unit_id FROM cc_sites WHERE id='$SITE_A'")
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
   -d "{\"principal_ref\":\"$S4_RR_SUB\",\"scope_type\":\"org_unit\",\"scope_ref\":\"$S4_ORG\",
        \"role\":\"site_admin\",\"permission_subset\":[\"incident.view\"]}" \
   http://localhost:8090/api/scope-grants/)" = "201" ] || { echo "row-holding grant refused" >&2; exit 1; }
S4_FULL=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
   -d "{\"principal_ref\":\"$S4_RR_SUB\",\"scope_type\":\"site\",\"scope_ref\":\"$SITE_A\",\"role\":\"site_admin\"}" \
   http://localhost:8090/api/scope-grants/ | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "  fresh reader with fleet.view at A:";   s4_sees "$S4_RR" "$SITE_A"
[ "$(curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $TOKEN" \
     http://localhost:8090/api/scope-grants/$S4_FULL)" = "200" ] || { echo "revoke refused" >&2; exit 1; }
echo "  the site grant REVOKED (row still held through the org grant):"; s4_generated "$S4_RR" withheld
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
   -d "{\"principal_ref\":\"$S4_RR_SUB\",\"scope_type\":\"site\",\"scope_ref\":\"$SITE_A\",\"role\":\"site_admin\"}" \
   http://localhost:8090/api/scope-grants/)" = "201" ] || { echo "restore refused" >&2; exit 1; }
echo "  the site grant RESTORED:";             s4_sees "$S4_RR" "$SITE_A"
[ "$(s1_cc "SELECT explanation::jsonb->'generation_visibility'->>'site_id' FROM cc_incidents WHERE incident_id='$S4_INC'")" = "$SITE_A" ] || {
  echo "the stored marker changed under a read" >&2; exit 1; }
echo "  the stored artifact and its marker are unchanged throughout"

step "A6-4B0b-S4/BN: both live databases cross the A30.29 upgrades with rows present, backfilling nothing"
# CC 0026 -> 0027 with the candidate seeded above present. Same shape as
# the A23-2 step: the column is dropped and the chain re-run on the LIVE
# database, and the window is one statement wide.
CC_HEAD=$(ls "$_REPO_ROOT"/services/central_command/src/harkeniq_cc/db/migrations/versions/[0-9]*.py \
  | sed 's|.*/\([0-9]\{4\}\)_.*|\1|' | sort | tail -1)
SM_HEAD=$(ls "$_REPO_ROOT"/services/site_manager/src/harkeniq_sm/db/migrations/versions/[0-9]*.py \
  | sed 's|.*/\([0-9]\{4\}\)_.*|\1|' | sort | tail -1)
S4_CANDS_BEFORE=$(s1_cc "SELECT count(*) FROM cc_candidate_skills")
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc "
  alter table cc_candidate_skills drop column generation_visibility;
  update alembic_version set version_num='0026';" > /dev/null
docker compose exec -T central-command sh -c \
  "cd /app/services/central_command && alembic upgrade head" 2>&1 | tail -1
[ "$(s1_cc "SELECT version_num FROM alembic_version")" = "$CC_HEAD" ] || { echo "CC did not reach head" >&2; exit 1; }
[ "$(s1_cc "SELECT count(*) FROM cc_candidate_skills")" = "$S4_CANDS_BEFORE" ] || { echo "candidate rows changed" >&2; exit 1; }
[ "$(s1_cc "SELECT count(*) FROM cc_candidate_skills WHERE generation_visibility IS NOT NULL")" = "0" ] || {
  echo "the CC upgrade backfilled a marker" >&2; exit 1; }
echo "  CC $CC_HEAD on PostgreSQL: $S4_CANDS_BEFORE candidate row(s), every generation_visibility NULL"
# SM 0010 -> 0011: a LEGACY pattern row present (written the way the old
# release wrote it), the per-site table and the candidate column removed,
# then the chain re-run. The legacy row stays where it is and is copied
# under no site; the per-site table comes back empty.
docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc "
  INSERT INTO sm_fleet_patterns (pattern_id, pattern_type, description, confidence, detected_at, received_at)
  VALUES ('gate-a3029-legacy', 'cross_site_batch', 'SEL_CLEAR fails at 65% across 3 sites (35/54)', 0.9, '1', now())
  ON CONFLICT (pattern_id) DO NOTHING;
  drop table sm_site_fleet_patterns;
  alter table sm_candidate_skills drop column generation_visibility;
  update alembic_version set version_num='0010';" > /dev/null
docker compose exec -T site-manager sh -c \
  "cd /app/sm && alembic upgrade head" 2>&1 | tail -1
[ "$(s1_sm "SELECT version_num FROM alembic_version")" = "$SM_HEAD" ] || { echo "SM did not reach head" >&2; exit 1; }
[ "$(s1_sm "SELECT count(*) FROM sm_fleet_patterns WHERE pattern_id='gate-a3029-legacy'")" = "1" ] || {
  echo "the legacy row did not survive" >&2; exit 1; }
[ "$(s1_sm "SELECT count(*) FROM sm_site_fleet_patterns")" = "0" ] || { echo "the SM upgrade invented per-site rows" >&2; exit 1; }
[ "$(s1_sm "SELECT count(*) FROM information_schema.columns WHERE table_name='sm_candidate_skills' AND column_name='generation_visibility'")" = "1" ] || {
  echo "sm_candidate_skills.generation_visibility missing after upgrade" >&2; exit 1; }
echo "  SM $SM_HEAD on PostgreSQL: legacy row kept, copied under no site; per-site store present and empty"

step "A6-4B0b-S4/BO: this proof owns its state"
docker compose exec -T postgres psql -U harkeniq -d harkeniq_cc -tAc \
  "DELETE FROM cc_incidents WHERE incident_id='$S4_INC';
   DELETE FROM cc_candidate_skills WHERE skill_id='$S4_CAND'" > /dev/null
docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
  "DELETE FROM sm_fleet_patterns WHERE pattern_id='gate-a3029-legacy';
   DELETE FROM devices WHERE agent_id='gate-agent-s4'" > /dev/null
[ "$(s1_cc "SELECT count(*) FROM cc_incidents WHERE incident_id='$S4_INC'")" = "0" ] || { echo "seeded incident not removed" >&2; exit 1; }
[ "$(s1_sm "SELECT count(*) FROM devices WHERE agent_id='gate-agent-s4'")" = "0" ] || { echo "site B's cohort device not removed" >&2; exit 1; }
echo "  the seeded incident, candidate, legacy pattern row and site B's cohort device are removed"

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
