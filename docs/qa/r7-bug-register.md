# R7 QA Bug Register — CC · SM · Agent · UIs · APIs

QA campaign 2026-08-25 (branch `r7-demo-hardening`). Mode: fix-as-found
(D16); real Keycloak JWT in scope (D17). Every bug: ID, layer, severity
(BLOCKER/MAJOR/MINOR), status (OPEN / FIXED@commit / WONTFIX-reason /
UNTESTABLE-reason). Source `audit` = found in the 2026-08-25 readiness
audit (docs/designs/production-demo-readiness.md); source `qa` = found by
this campaign's live testing.

## Register

| ID | Layer | Severity | Bug | Status |
|---|---|---|---|---|
| QA-001 | Deploy/CC+Console | BLOCKER | CC & Console boot with no DB schema on Postgres (runtime create_all is sqlite-only; alembic entrypoints referenced by nothing, wrong paths, alembic assets not COPY'd into images) | FIXED@pending — entrypoints run `alembic upgrade head` (fail loudly), alembic.ini COPY'd into both images; verified live: fresh boot, CC/Console healthy with REAL DB probes |
| QA-002 | Console UI | BLOCKER | UI unloginable: no VITE_KEYCLOAK_URL/REALM build args; realm default `harkeniq` vs shipped `harkeniq-platform`; auth URL built against empty origin | FIXED@pending — VITE_KEYCLOAK_URL/REALM/CLIENT/DEV_MODE build args (realm harkeniq-platform); browser click-through pending UI QA phase |
| QA-003 | Deploy | BLOCKER | Default stack has no agent service (commented out); uncommented block exits 3 on TLS validation (no `HARKENIQ_SITE_MANAGER_TLS:"false"`) | FIXED@pending — agent in default stack w/ SITE_MANAGER_TLS false; verified live: registered + OBSERVING at SM |
| QA-004 | Deploy | BLOCKER | Host-port collisions: postgres publishes 5432 (dies on hosts with local postgres — verified); `network-sim` profile double-binds 50051 | FIXED@pending — postgres host publish removed (opt-in comment); switch-sim on host 50052 |
| QA-005 | Console+CC backend | BLOCKER | No real auth: secure mode raises HTTP 501; insecure mode grants `platform_super_admin` to every caller | FIXED@pending — real JWKS validation (harkeniq/security/oidc.py) in Console (dual-realm) + CC (single-realm; configure_auth finally CALLED); compose runs INSECURE=false; verified live: real Keycloak token accepted at both, no/garbage token 401; 11 unit tests |
| QA-006 | SM UI/API | BLOCKER | Approver identity self-asserted (free-text input → `decided_by`); audit proves nothing | OPEN |
| QA-007 | Agent CLI | BLOCKER | `harken diagnose` crashes (TypeError: `_TARGET_COLLECTIONS` values called as functions; wrong `engine.evaluate` signature); no Nagios exit codes; zero test coverage | OPEN |
| QA-008 | Agent demo | BLOCKER | Trending slope uses wall-clock hours under compressed demo time → `-5,402,706 RPM/hr … 0 hours`; "46 hours before failure" unproducible | OPEN |
| QA-009 | SM UI | BLOCKER | LLM explanation computed/stored/served but rendered by no UI (Incident TS type lacks `explanation`) | OPEN |
| QA-010 | Deploy | MAJOR | No restart policies on any service; CC/Console healthz hardcoded ok (no DB probe); depends_on = service_started only | FIXED@pending — restart policies everywhere; CC/Console healthz now probe the DB (503 when degraded); service_healthy deps; two stale runtime tests updated |
| QA-011 | Deploy | MAJOR | No .dockerignore: 374MB contexts; host node_modules overwrite image installs (darwin-on-linux breakage) | FIXED@pending — root .dockerignore (contexts ~374MB -> small; node_modules overwrite eliminated) |
| QA-012 | Deploy | MAJOR | Agent identity/baselines unpersisted in compose (re-enrolls each restart) | FIXED@pending — named volumes for agent + network-agent /var/lib/harkeniq |
| QA-013 | Deploy | MAJOR | Compose omits Console↔CC linkage env (tenant id, console URL/API key) — marketplace sync + usage reporting silently disabled | FIXED@pending — CC tenant/console/keycloak env linkage restored in compose |
| QA-014 | Docs | BLOCKER | No README/quickstart/DEMO runbook/seed data (README = 22 bytes UTF-16) | OPEN |
| QA-015 | Security | MAJOR | Checked-in creds: Keycloak admin/admin (temporary:false), postgres harkeniq/harkeniq, SM token dev-token-sm | OPEN |
| QA-016 | Agent | MAJOR | Peer HMAC secret defaults ""; empty accepted → forgeable heartbeats | OPEN |
| QA-017 | Agent | MAJOR | Agent silently downgrades to plaintext when tls:true but tls_ca empty | OPEN |
| QA-018 | CC | MAJOR | CC→SM gRPC unconditionally plaintext (no TLS option in SMClient) | OPEN |
| QA-019 | Console | MAJOR | License signing key auto-generated into DB; `license_signing_key_path` never read; CC never verifies any license | OPEN |
| QA-020 | Agent | MAJOR | Autonomy chain unwired: preconditions/lease-gate/budget/blast-radius/verification never called by Agent or executor; four R3a actions have no Redfish executor branch | OPEN |
| QA-021 | SM | MAJOR | SuppressionEngine + SMAutonomyEnforcer never instantiated; leases carry defaults (unlimited budget, stop_switch=False) | OPEN |
| QA-022 | CC | MAJOR | Stop switch = in-process dict; never persisted, never reaches SM/lease | OPEN |
| QA-023 | Agent | MAJOR | RedfishProtocol.execute_action self-grants full ActionType allow-list (bypasses R-X6 for any direct caller) | OPEN |
| QA-024 | Agent | MAJOR | OS signals package (~900 lines) + log-poll loop unwired; SEL/IML never reaches a verdict; log_cursors dead | OPEN |
| QA-025 | Agent | MAJOR | resources monitor unwired; `HARKENIQ_RESOURCES_PROFILE` env silently discarded (no `resources` config section) — D12 unenforced | OPEN |
| QA-026 | Services | MAJOR | Structured JSON logging + request-id middleware imported only by tests; all services use basicConfig text | OPEN |
| QA-027 | Demo/fixtures | MAJOR | Fixtures diverge from doc 09/11: 4 fans vs 8, 1 disk vs 4 (drive_1..3 routes 404), CPU2 sensor absent | OPEN |
| QA-028 | Demo | MAJOR | Doc 09 scenes missing: cross-subsystem correlation, thermal scene inert (Exhaust +2°C vs 75°C threshold), summary dashboard is text blob, `--scenario` absent, PSU action masked by undocumented fan-seize | OPEN |
| QA-029 | Console UI | MAJOR | SPA splits /api across two backends (CC 8090 + Console 8100) with no reverse proxy — half the screens 404 in every shipped config; L3/L4 boundary blurred | OPEN |
| QA-030 | CI | MAJOR | CI never builds containers or UI; coverage gate not enforced; no compose-boot gate | OPEN |
| QA-031 | Agent | MINOR | Playbook executions in-memory only (crash loses resume state) | OPEN |
| QA-032 | Agent | MINOR | ECC counters baselined as raw values not rates (doc 13 §5.6) | OPEN |
| QA-033 | SM/CC | MAJOR | Knowledge distribution: proto field `learned_patterns_json` doesn't exist; distributor/feedback outside every loop; SM PushPolicy is log-and-ack stub | OPEN |
| QA-034 | Agent | MAJOR | Credential rotation stub (`return True` x4); credential validation (R-CV1-5) absent | OPEN |

| QA-035 | Console | MAJOR | `/api/internal` endpoints have NO auth (docstring: "No auth (internal network)") despite R5-2 ledger claiming a CC->Console credential pair | OPEN |

(Statuses updated in place as the campaign proceeds; new live findings appended.)

## Test log

| When | Area | Result |
|---|---|---|
| 2026-08-25 | Baseline | Full suite 2157 collected / 2155 pass, 2 expected skips; e2e 17/17; `harken demo` exit 0 |
| 2026-08-25 | Boot+Auth | FIRST fully healthy fresh boot: 7/7 services healthy; agent registered+OBSERVING at SM; CC+Console in SECURE mode accept a real Keycloak token (password grant, platform admin), reject absent/garbage tokens with 401 |
