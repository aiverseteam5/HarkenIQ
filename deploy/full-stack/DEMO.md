# HarkenIQ — Production Demo Runbook

Click-by-click script for the four-tier live demo (QA-014). Total time:
~15 minutes. Everything runs locally; authentication is real.

## 0. Preflight (before the audience arrives)

```bash
cd deploy/full-stack
docker compose up -d
watch docker compose ps        # wait until every service reads (healthy)
../../scripts/seed-demo.sh     # tenant + site registration (idempotent)
```

Healthy means healthy: CC and Console health checks probe the database;
a green stack is a working stack.

Optional wow-factor prep — LLM Explain (pick ONE):

- **Cloud:** uncomment the five `HARKEN_SM_LLM_*` lines in
  `docker-compose.yml`, set `HARKEN_SM_LLM_API_URL` to an OpenAI-compatible
  `/v1` endpoint + `HARKEN_SM_LLM_API_KEY`, `docker compose up -d site-manager`.
- **Air-gapped:** place a `.gguf` in `deploy/models/` as `model.gguf`,
  set `HARKEN_SM_LLM_MODEL_PATH: /models/model.gguf` and
  `HARKEN_SM_LLM_MODEL_SHA256: $(sha256sum deploy/models/model.gguf)`,
  then `docker compose --profile airgap-llm up -d`.

Without an LLM the demo still works — deterministic + knowledge-base
reasoners always run; the explanation panel simply doesn't appear.

## 1. The 60-second story (terminal, R1 heritage)

```bash
harken demo --speed 10
```

Narrate: "One agent, one server, zero cloud. It caught the fan bearing
failure **44 hours before the fan died** — from RPM trend against a
learned per-device baseline, not a threshold."

## 2. Observe (SM dashboard)

Open **http://localhost:8080** → token `dev-token-sm`.

- Devices: the containerized agent, `observed / OBSERVING`, PowerEdge R750.
- Point out: "silent means unobserved, never healthy" (coverage states).

## 3. A fault happens

```bash
curl -sk -X POST https://localhost:9000/test/inject-fault \
  -H 'Content-Type: application/json' \
  -d '{"fault_type":"fan","target":"Fan1A","params":{"health":"Critical","speed_rpm":0}}'
```

Within one poll (~15s): SM **Incidents** shows the fan incident. With LLM
configured, the **DIAGNOSIS panel** renders: summary, confidence,
suggested action, expandable reasoning.

## 4. Approve (the human-in-the-loop moment)

SM dashboard → **Approvals**: the agent proposed `COLLECT_DIAGNOSTICS` /
`IDENTIFY_LED`. Approve one. Narrate: "approval in seconds, named in the
audit trail, and a denial is FINAL — the platform never nags."

Verify the audit chain live:

```bash
curl -s -H 'Authorization: Bearer dev-token-sm' http://localhost:8080/api/audit/verify
```

## 5. Console (L4 — the business layer)

Open **http://localhost:8100** → real Keycloak login
(`admin@harkeniq.com` / `admin`).

Walk: Fleet Overview (device_class column — servers and switches),
Approval Policies (groups + autonomy budgets), Vendor Reliability,
Audit Logs, Billing. All 27 screens are live against real APIs — the
L3 surfaces proxy through to Central Command with the same token.

## 6. Network Intelligence (R6 encore)

```bash
docker compose --profile network-sim up -d
```

A second agent speaks **gNMI** to a SONiC-shaped switch simulator —
same skills engine, same approval chain, per-port baselines. Narrate:
"one brain, servers AND switches; the gNMI path was validated against
real SONiC."

## 7. If asked about security

- OIDC RS256 against Keycloak JWKS; no token, no API (try
  `curl http://localhost:8090/api/fleet/` → 401).
- Every store carries a SHA-256 hash-chained audit (step 4's verify).
- The tenant plane is authorized by permission, not membership: a `viewer`
  calling `POST /api/tenants/{id}/api-keys/` directly gets 403, not a key.
- Platform staff do not get customer tenants for free, and cannot let
  themselves in. `platform_support` *requests* access
  (`POST /api/admin/support-access/{tenant_id}/request`); only a
  `platform_super_admin` can approve it, for 24h, revocable, audited at
  every step. A grant admits support, it does not make it root: still no
  `user.manage`, so still no minting API keys. `platform_super_admin`
  keeps an unconditional break-glass on purpose — gating that on the grant
  mechanism would lock everyone out if the mechanism itself failed.
- Tenant context is in the URL (`/t/{tenant}/...`), not a header or browser
  storage, and a platform admin is never placed in a tenant automatically:
  they pick one from the registry, and the pick is an explicit act.
  Each tenant's Central Command is resolved from a service-placement
  registry, fail-closed — an unregistered tenant is refused, never handed
  another tenant's stack.
- The demo stack still runs lab defaults (admin/admin, dev-token-sm) —
  say so if asked; production hardening is config, not code.

## Reset between demos

```bash
docker compose down -v && docker compose up -d && ../../scripts/seed-demo.sh
```

After a fresh reset, give the agent ~2 minutes before injecting faults:
baselines must re-learn (5 polls at the demo stack's compressed learning
window) before actions are proposed — the A2.3 confidence gate refuses to
act on a baseline it hasn't earned, which is itself a talking point.

## Upgrading an existing stack: the `basic` client scope

Keycloak persists realms in postgres, so `--import-realm` is a no-op once
the realm exists — editing `deploy/r2b/keycloak-realm-platform.json` does
not reach a stack that has already booted. A `docker compose down -v`
picks it up; anything else needs the scope attached by hand.

Without it, Keycloak 24+ mints access tokens with **no `sub` claim** (that
mapper lives in the built-in `basic` scope), so `UserContext.user_id` is
empty: custom-role grants never resolve, and Console audit rows land with
a null `actor_id` (`actor_email` still carries the actor, so the trail
stays attributable).

```bash
KC=http://localhost:8180
AT=$(curl -s -X POST "$KC/realms/master/protocol/openid-connect/token" \
  -d client_id=admin-cli -d grant_type=password \
  -d username=admin -d password=admin | jq -r .access_token)
CID=$(curl -s -H "Authorization: Bearer $AT" \
  "$KC/admin/realms/harkeniq-platform/clients?clientId=harkeniq-console" | jq -r '.[0].id')
SID=$(curl -s -H "Authorization: Bearer $AT" \
  "$KC/admin/realms/harkeniq-platform/client-scopes" | jq -r '.[] | select(.name=="basic") | .id')
curl -s -X PUT -H "Authorization: Bearer $AT" \
  "$KC/admin/realms/harkeniq-platform/clients/$CID/default-client-scopes/$SID"
```

Verify: a fresh token should decode with a `sub`, and `GET /api/me`
should return a non-empty `user_id`.
