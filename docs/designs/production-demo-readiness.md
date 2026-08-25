# Production-Demo Readiness — Complete Gap Analysis

Date: 2026-08-25. Method: three exhaustive spec-vs-code sweeps (docs 00–04
requirement coverage; docs 05–13 implementation fidelity; PRD/Platform-Design
deployment audit) plus LIVE verification — full-stack `docker compose up` on
this host, service health probes, `harken demo` runs, e2e suite. Nothing
below is inferred from the ledger; every finding was checked against code or
observed running. Companion milestone plan: §7 of this document.

## 1. Executive verdict

**The demo that works today is the R1 story** — `harken demo` runs clean,
the agent detects/trends/proposes against real Redfish/IPMI/gNMI, the R2a
site-correlation e2e is genuinely end-to-end, and R6's network path was
proven against real SONiC. **The four-tier platform demo does not work
today**: `docker compose up` yields a Central Command and Console with **no
database schema** (verified live: `relation "cc_sites" does not exist`), a
Console UI that **cannot be logged into as built**, no agent in the default
stack, no README, no seed data — and the product's single most
differentiating output, the **LLM incident explanation, is computed and
stored but never rendered in any UI**.

Underneath sit three systemic failure classes, each with one root cause:

1. **Built-but-unwired.** Large, well-tested subsystems are never
   instantiated by the running product: the ENTIRE agent autonomy chain
   (preconditions, budgets, lease gating, blast radius, verification), the
   ENTIRE mesh protocol (claims, quorum, suspicion, partition fence —
   `agent.py` sets them to `None` and never assigns), OS signals, the
   resource monitor, credential chain, SM suppression engine, SM autonomy
   enforcer, CC knowledge distribution (its proto field doesn't even
   exist), structured logging. Tests exercise the libraries; the agent
   never calls them.
2. **Deploy rot.** Entrypoints referenced by nothing, migrations that only
   ran on sqlite, port collisions (host 5432; two services on host 50051 in
   the network-sim profile), dead env vars, no restart policies, no real
   healthchecks at CC/Console.
3. **Security theater.** Secure mode raises HTTP 501 (`JWT validation not
   yet implemented`); insecure mode grants every caller
   `platform_super_admin`. Approver identity is a self-asserted text input.
   Checked-in `admin/admin`. Peer HMAC secret defaults to `""` and empty is
   accepted. License keys are issued but never verified anywhere.

**Root cause of all three: exit gates that never booted the artifact.**
`test_r4_0_exit_gate.py` asserts files exist; `test_docker_compose.py` says
in its own docstring it validates structure "without actually running
Docker"; `test_r3a_exit_gate.py` hand-assembles modules with step 6 as a
comment (`# 6. Execute (simulated)`). Green gates, unbootable product —
found live twice today (broken mock-simulator CMD since R4-0; my own R6
migration crash-looping SM on a fresh DB, fixed in `5fec5f7`).

## 2. Verified-solid foundation (what the demo CAN lean on)

- `harken demo` (R1 story): exits 0, all severity classes, peer-witness
  evidence retention, pending actions. e2e suite 17/17.
- Skills engine byte-faithful to doc 07; baselines/trending faithful to
  doc 13 core; normalization data model faithful to doc 08 §3–§9;
  state machine exact to doc 06 §11; heartbeat HMAC + witness buffer;
  checkpoint schema exact; action queue/allow-list/audit (with the caveats
  below); TUI complete with hotkeys.
- R2a exit gate: real two-agent + two-simulator → one parent incident →
  brokered approval. The only genuinely end-to-end release gate in CI.
- SM service: real health checks (db + LLM model probes), audit chain +
  verify endpoints, correlation rules incl. R6 tor_connectivity, firmware
  orchestrator with waves/rollback.
- Console: billing ledger/engine/adapters with verified+idempotent
  webhooks; 27 finished UI screens; SM dashboard 5 screens.
- R6 network path end-to-end against REAL SONiC (32 ports streaming).
- LLM Explain back end: enrichment fires on WARNING/CRITICAL, explanation
  persisted and served by the API — only the last inch (UI) is missing.
- Degrades safely without the LLM (deterministic + KB reasoners always on).

## 3. BLOCKERS for a first production demo (all verified)

| # | Finding | Evidence |
|---|---|---|
| B1 | CC + Console boot with no schema on Postgres; every DB call 500s. Their runtimes `create_all` only on sqlite; the "entrypoint runs alembic" docstring refers to entrypoint scripts nothing references (and whose paths are wrong for the images) | live: `relation "cc_sites" does not exist`; `deploy/full-stack/entrypoint-*.sh` referenced by nothing |
| B2 | Console UI unloginable: no `VITE_KEYCLOAK_URL/REALM` build args (realm default `harkeniq` vs shipped `harkeniq-platform`), auth URL built against empty origin; all 27 screens behind the broken redirect | `console-ui/src/useAuth.ts`, `deploy/r2b/Dockerfile.console` |
| B3 | Default stack has NO agent (service commented out; uncommenting exits code 3 — TLS config validation requires `tls_ca` while SM runs insecure) | compose `:161-177`; `config.py:231-238` |
| B4 | Host-port collisions: postgres publishes 5432 (dies on any host with local postgres — verified live); `network-sim` profile double-binds 50051 (SM + switch-sim) | live boot failure |
| B5 | Zero install/demo docs: README is 22 bytes; no quickstart, runbook, seed data, install.sh. PRD day-30 gate is "a stranger installs Observe in minutes" | repo-wide find |
| B6 | LLM explanation never rendered: `Incident` TS type has no `explanation` field; no UI greps for it. The differentiator is invisible in every screen | `services/site_manager/ui/src/types.ts` |
| B7 | `harken diagnose` CRASHES (TypeError: `_TARGET_COLLECTIONS` values are strings called as functions; `engine.evaluate` called with wrong signature). Zero test coverage; doc 06 §6.3 Nagios exit codes also absent | `cli.py:239-241` |
| B8 | Demo's flagship line prints nonsense: `declining at -5,402,706 RPM/hr … in 0 hours` — trending slope uses wall-clock hours while demo compresses to 0.2s samples. The doc 09 headline ("caught it 46 hours before failure") is unproducible at any speed | live run output |
| B9 | Doc 09 acceptance criteria failing: cross-subsystem correlation scene ABSENT (no code), thermal cascade silent (drifts Exhaust +2°C vs 75°C threshold), summary dashboard is a text blob, `--scenario` mode absent, PSU action produced by an undocumented masking step; fixtures 4 fans/1 disk vs spec'd 8/4 (3 of 4 drive routes 404) | doc-09 sweep + live run |
| B10 | No working auth anywhere: Console/CC secure mode = HTTP 501; insecure default grants `platform_super_admin` to any caller; approver identity is free-text from the request body (PRD "a named human approves" unmet); SM site token doubles as agent transport AND operator/approval credential | `auth.py` both services; `api/actions.py` |
| B11 | R2b/R3a exit-gate claims don't hold at the product level: license keys never verified (CC `license_key_path` has zero readers); agent autonomy pipeline (preconditions→budget→lease→blast radius→verification) never invoked by `agent.py`; the four R3a actions have no Redfish executor branch (fall to "Unknown action type"); CC stop switch is an in-process dict that reaches no lease (`stop_switch` hardcoded `False`) | coverage sweep, file refs in §5 |
| B12 | Credential rotation is a hollow stub (`_create/_verify/_disable/_delete` all `return True`, comment admits it); doc 03's lead capability (credential VALIDATION, R-CV1–5) absent entirely — the ledger records both as shipped | `security/credential_rotation.py:142-169` |

## 4. MAJOR (selected; full inventories in the three sweep reports)

**Unwired subsystems** (exist + tested, never constructed by the product):
mesh claims/quorum/suspicion/fence + PeerProtocol (`agent.py:139-140` =
`None` forever); `os_signals/` (~900 lines, zero production imports);
`autonomy/resources.py` (and `HARKENIQ_RESOURCES_PROFILE` env is silently
discarded — no `resources` config section, so the D12 decision is
unenforced); `blast_radius.py` + `network_safety.py` not consulted by the
executor; SM `SuppressionEngine` + `SMAutonomyEnforcer` never instantiated
(leases carry defaults: R1 actions, unlimited budget, `stop_switch=False`);
CC `KnowledgeDistributor`/`LearningFeedbackTracker` outside every loop —
R-C1 is open-loop; `CredentialProviderChain` unused (agent reads plaintext
YAML password — R-AGENT-26/doc 03/doc 06 §3.3 all violated); structured
JSON logging + request-id middleware imported only by tests; agent log-poll
loop absent (SEL/IML never reaches a verdict; `log_cursors` table dead).

**Security/majors:** CC→SM gRPC unconditionally plaintext (no TLS option);
agent silently downgrades to plaintext when `tls:true` but no CA; license
signing key auto-generated INTO the Console DB (config path never read);
checked-in `admin/admin` + `dev-token-sm` + postgres creds; no mTLS
(R-X14/A1.2 promised R2b); heartbeat HMAC optional-empty (R-X15);
`RedfishProtocol.execute_action` self-grants the FULL ActionType allow-list
("unrestricted by design" — any caller reaching the protocol bypasses
R-X6); agent audit never forwarded to SM (R-AGENT-30); actions carry no
signature/expiry/replay-id (R-X5, R-AGENT-8); no read-only deployment mode
(R-X11); no self-destructive-action guard for servers (R-AGENT-11).

**Operability:** no restart policies anywhere; CC/Console `/healthz`
hardcoded `{"ok"}` (no DB probe — would report healthy in the B1 state); no
`.dockerignore` (374MB build contexts; host `node_modules` overwrite the
image's → darwin binaries in linux images when built on a Mac); agent
identity/baselines unpersisted in compose (re-enrolls every restart);
airgap-llm profile mounts a nonexistent `deploy/models/`; CI never builds a
container or the UI; no performance tests (doc 12 §5: 0 of 8); coverage
gate not enforced (`--cov-fail-under` absent).

**Product-vision majors:** the SPA mixes L3 and L4 surfaces behind one auth
(spec §1 boundary) and splits its API calls across two backends with no
reverse proxy (half the screens 404 in every shipped config); the rich
`Diagnosis` object (doc 02's "operator acts without re-deriving" — "the
single most important test in the plan") reaches humans ONLY via the broken
CLI, never the SM/Console UI; R-S1 SM poll path for node-less devices
absent (coverage = installation); R-M6 peer consultation before concluding
absent; predictive is a deterministic heuristic that will not survive a
buyer demo framed as "prediction".

## 5. Spec contradictions to resolve by amendment (not silently)

1. D16 ("denied actions are final", CLAUDE.md locked) vs doc 01 R-C3
   ("approvals revocable") — never reconciled in §9.
2. A9 still lists NETCONFProtocol as a deliverable; D13 dropped it —
   needs a one-line A9 amendment note.
3. `agent.py:835` docstring claims directives pass preconditions; the
   executor has no precondition path — directives ARE currently a policy
   bypass relative to A2.1.
4. Doc 07 target enum lacks `interface`; doc 08 §10.1 public API shape
   diverges from the per-vendor modules that exist; doc 10 is missing the
   entire R2a+ service surface; doc 03 header still says "Release: R2".
5. **Ledger corrections owed** (no-assumptions culture): R2b "RBAC enforced
   server-side", R3a "agent autonomously executes with preconditions +
   blast radius + verification", R3a/R3b "OS signals wired", R3b-2
   "Contract 7 complete" (implemented, never constructed), R3b-3
   "blue-green rotation" (stub), R3b-3 "knowledge distribution" (no proto
   field). Each shipped as a library, not as product behavior.

## 6. What the first production demo should BE (vision-aligned)

Doc 01 §8 defines the claims to prove; doc 02 §8 names the test that
matters: **"an operator acts on a diagnosis without re-deriving it."** The
PRD day-30/60 gates: a stranger installs Observe in minutes; partners run
on real fleets. Therefore the demo is:

> `docker compose up` → seeded tenant → operator logs into the Console →
> fleet is green → a fault is injected (server fan decline AND a switch
> optic decay) → TRENDING with a sane projection → incident opens at SM
> with the **LLM explanation and Diagnosis rendered** → a NAMED operator
> approves the proposed action → agent executes with preconditions +
> read-back verify → outcome and audit chain visible → stop switch
> demonstrably halts autonomy.

Everything in §7 exists to make that sentence true.

## 7. Next steps — proposed milestone R7 "Demo Hardening" (needs A10 approval)

Ordered by dependency; each phase lands suite-green with a REAL gate.

**R7-P0 — Boot truth (the compose gate).** Fix B1/B2/B3/B4: alembic-running
entrypoints (assets COPY'd), Console UI build args (realm
`harkeniq-platform`, dev-mode build for demo), agent service in the default
stack with `HARKENIQ_SITE_MANAGER_TLS:"false"`, port fixes (drop host 5432
publish; switch-sim to 50052), `.dockerignore`, restart policies, real
CC/Console healthchecks + `service_healthy` deps, agent volume. THEN the
new exit gate: a CI job that `docker compose up`s the stack, injects a
fault, approves via API, asserts the outcome — the amendment-§8 ten-step
scenario, executable. This gate is the fix for the root cause in §1.

**R7-P1 — Demo truth.** Fix B7 (diagnose crash + Nagios exit codes + the 5
missing CLI tests), B8 (demo synthetic clock so projections read "46
hours"), B9 (cross-subsystem correlation scene, thermal scene thresholds,
summary dashboard, `--scenario`, fixtures to 8 fans/4 disks), B6 (render
`explanation` + Diagnosis in the SM incident card — cheapest high-value fix
in the list), disk-wear trend (5σ-reset exemption when the jump is toward
failure — doc 13 §5.2 amendment).

**R7-P2 — Wire what's built (agent).** The autonomy chain into
`Agent.poll_and_evaluate`/executor: preconditions → lease.allows_action →
budget → blast radius (incl. network_safety) → execute → verification →
typed outcome (UNKNOWN producible). Resource monitor + `resources` config
section (makes D12 real). OS-signals loop + log-poll loop. Structured
logging + request-id middleware in all three services. UNKNOWN-on-restart
for in-flight actions. Remove `RedfishProtocol`'s self-granted allow-list.

**R7-P3 — Wire what's built (SM/CC).** SuppressionEngine + SMAutonomyEnforcer
into SM runtime (leases carry real budgets + stop_switch); persist CC stop
switch and thread it CC→SM→lease (R-C5 real); license verify at CC startup
+ site registration; `learned_patterns_json` proto field + KnowledgeDistributor
into the CC loop (close R-C1); either wire the mesh protocol into the agent
runtime behind a config flag OR record by amendment that mesh is
library-complete/product-deferred — no third option; same choice for
credential rotation (finish the Redfish AccountService calls or de-claim).

**R7-P4 — Partner-network security minimum.** Keycloak JWKS validation in
Console+CC auth (delete the 501 path); approver identity from the
authenticated principal, never the body; secrets to `.env` with required
substitution; HMAC secret required non-empty; fail-closed agent TLS; CC→SM
TLS option; separate operator credential from agent token. (If the first
demo is laptop-local screen-share only, P4 can follow the demo — stated
out loud, not silently.)

**R7-P5 — Docs + runbook + spec hygiene.** README, quickstart,
`deploy/full-stack/DEMO.md` (click-by-click with URLs/credentials/fault
curl), seed script (`scripts/seed-demo.sh`: tenant → license → site →
registered SM). Amendment A10 records this milestone; §5 contradictions
resolved; ledger corrected.

Estimated shape: P0+P1 are days and make the demo REAL; P2+P3 are the weeks
that make the demo HONEST (the product doing what the ledger already
claims); P4 gates the partner-network variant; P5 is continuous.

## 8. Recommended immediate actions

1. Approve A10 (this §7 plan) — amendment before code, per §0.
2. Run **`/code-review ultra`** now that R6 + this analysis are on main —
   the cloud multi-agent review is the right second opinion on exactly the
   wired-vs-built distinction this audit surfaced.
3. First code lands: R7-P0 (nothing else matters until `up` works) and
   B6/B7/B8 from P1 (three small fixes, outsized demo impact).
