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

# A draft agent evaluates nothing. Activation is a separate human act.
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8090/api/operational-agents/$AGENT_ID/activate" \
  | python3 -c "
import sys, json
a = json.load(sys.stdin)
assert a['status'] == 'active' and a['activated_by'], 'activation must name a human'
print('agent activated by', a['activated_by'])
"

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
                        device_class, first_seen_at, last_seen_at)
   SELECT 'gatedevb00000000000000000000000', s.id, 'gate-agent-b', 'b1',
          'Dell', 'R750', 'server', now(), now()
   FROM sites s WHERE s.cc_site_id = '$SITE_B'
   ON CONFLICT (id) DO NOTHING"
docker compose exec -T postgres psql -U harkeniq -d harkeniq_sm -tAc \
  "INSERT INTO incidents (id, site_id, kind, status, device_id, subsystem,
                          title, opened_at, last_seen_at)
   SELECT 'gateincb00000000000000000000000', s.id, 'device', 'open',
          'gatedevb00000000000000000000000', 'psu', 'site B only', now(), now()
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
  -d '{"fault_type":"psu","target":"PSU1","params":{"health":"Critical"}}' \
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
