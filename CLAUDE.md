# HarkenIQ — Project Constitution

HarkenIQ is the vendor-neutral AI SRE for data center hardware: an embedded brain for
every server regardless of vendor ("they bolt on, we build in"). Hardware becomes an
agentic, intelligent device through three earned tiers — **Observe** (read-only
diagnosis) → **Approve** (human-approved actions in seconds) → **Autonomy** (proven
actions run unattended within budgets, with a stop switch).

## Governing spec — read this first

**`docs/requirements/00-platform-spec.md` (dated 2026-08-19) governs all engineering.**

- Detailed requirements: `docs/requirements/01–13` (R-M*/R-S*/R-C*/R-X* IDs authoritative).
- Product vision/personas/pricing: `HarkenIQ_PRD.md` + `HarkenIQ-Platform-Design.md`.
- **NOT build order:** doc 02 §8 market phasing, PRD 30/60/90 plan, Platform-Design
  5-phase plan, and TODOS.md's "release one is Harken Mesh" — all superseded per spec §0.
- **Change control:** scope changes only by dated amendment to spec §9. A slice may
  narrow itself; it may never redefine another slice or rename a layer. Do not
  re-litigate scope at phase boundaries.

## Fixed vocabulary — four layers (spec §1)

| Layer | Name | One line |
|---|---|---|
| L1 | **Harken Node (Mesh)** | Per-device agent; detects all single-device faults |
| L2 | **Site Manager** | Per-site multi-device correlation, incident consolidation, command brokering |
| L3 | **Central Command** | Per-tenant fleet intelligence, learning, authorization, approvals (= old "Cluster Manager", name retired) |
| L4 | **Harken Console** | Vendor multi-tenant SaaS: tenants, onboarding, RBAC, billing, support, super admin |

Correlation boundary: single-device fault → node; multi-device comparison → Site Manager
(spec §1, doc 01 §4.3.1).

## Status ledger (update at every milestone commit)

| Date | Milestone |
|---|---|
| 2026-08-19 | **R1 Diagnostic Foundation shipped** — commits `16cfca8` (Phase 2) + `5128fbd` (Phase 3) on origin/main; 498 tests; Dell/HPE Redfish, skills, baselines/trending, state machine, checkpoint, peer heartbeat/witness, action queue + CLI approval, TUI, gRPC reporter stub, `harken demo` |
| 2026-08-19 | Master spec `00-platform-spec.md` + this constitution adopted |
| 2026-08-19 | **R2a Site Manager shipped** — `harkeniq_sm` service (gRPC ingest TLS+token, TimescaleDB-ready schema, site model + domain inference, correlation engine with 4 rules, incident consolidation, approval brokering, FastAPI + React dashboard v1, Docker Compose); agent: registration/action-sync/decision-poll RPCs, secure channel; CLI `peers list` + `bmc test`; frozen exit gate green; 631 tests, 87% coverage |
| 2026-08-22 | **R2b Central Command + Console v1 shipped** — 7 phases (`1e4b097`..`82ea9f9`); CC service (fleet poller, usage reporter, approval routing, autonomy budgets); Console service (Keycloak OIDC, tenant/user/RBAC, Ed25519 licensing, billing engine with commit+overage invoicing, Razorpay+Stripe adapters, delinquency state machine, metering+air-gapped upload, support ticketing+24h mode, audit logs+export, feature toggles, release mgmt, API keys, impersonation log); 22 production React screens; 25 Console DB tables; 1188 tests, exit gate green |
| 2026-08-23 | **R3 re-sliced** — Amendment A2 to spec §9; R3 split into R3a/R3b/R4; 6 gating OQs answered; 7 architectural contracts defined |
| 2026-08-23 | **R3a Safe Autonomy + Outcome Loop shipped** — 4 commits (`a969d3f`..P8); agent Ed25519 identity + SM-signed leases + tier gating; risk-degraded partition behavior; resource profiles (constrained/standard/performance); 4 new actions (SEL clear, BMC reset, power cycle, power cap adjust) with preconditions + blast radius + verification; autonomy budgets (distributed CC/SM/agent) + stop switch; outcome tracking + error-budget drop-back; correlated-conclusion suppression (2 trigger paths + auto-recovery); OS signals (syslog/dmesg) + hardware-to-OS device mapper; Diagnosis model; `harken diagnose` CLI; 7 A2.7 contracts verified; 1331 tests, exit gate green |
| 2026-08-23 | **R3b-1 Intelligence + Skills shipped** — 3 commits (`943ceef`..R3b-1); LLM Explain at SM (ReasoningPipeline + LLMReasoner, backend-agnostic httpx provider); candidate skill generation; OS signal expansion (journal + smartctl); full hardware-to-application mapping (drive->block->mount->process->service); SkillPackage<->loader integration; skill validation pipeline (static analysis + dry-run); skill distribution (PushSkill RPC + agent SkillReceiver); KB persistence tables; 1397 tests, exit gate green |
| 2026-08-24 | **R3b-2 Full Mesh Protocol shipped** — 8 phases; peer key distribution (SM-brokered PeerKeyRing + signed bundles); message type envelope (4-type UDP multiplexing); claim data model (Ed25519-signed, canonical JSON wire format); claim broadcast + ack (UDP reliability layer with retry); first-claim-wins ownership (deterministic tiebreak on agent_id, not timestamp); claim lease management (120s lease, lapse inheritance per R-M17); quorum disambiguation (4-way: DEVICE_DOWN/LINK_DOWN/NODE_FAILED/ISOLATED); suspicion exchange (per-component scores, threshold-triggered claims, greedy set-cover R-M21, bundle coverage R-M22); two-device correlation probe (OQ-13: LOCAL_PORT/REMOTE_PORT/CABLE/INCONCLUSIVE); partition fencing (isolation detection, fenced mode, recovery); all 4 PeerProtocol stubs implemented (Contract 7 complete); OQ-4 + OQ-13 answered; Amendment A3; 1523+ tests, exit gate green |
| 2026-08-24 | **R3b-3 Advanced Remediation + Fleet Learning shipped** — 8 phases; CredentialProvider interface (Local+Vault+Mock+Chain, Vault via httpx KV v2, R-H7 fallback); credential rotation (blue-green: create→verify→disable, rollback on failure); multi-step playbooks (Playbook/PlaybookStep/PlaybookExecution, PlaybookExecutor with per-step verification+rollback+resume, 3 built-in playbooks); fleet outcome reporting (FleetOutcome proto in FleetSnapshot, SM watermark, CC cc_outcome_history); outcome aggregation (by action_type/vendor/model, success/failure/resolution rates, trend detection); fleet pattern detection (batch_failure/anomaly/reliability, cc_fleet_patterns table); knowledge distribution (CC→SM routing by scope match, PushPolicy channel); learning feedback loop (R-C1 complete: outcome→pattern→skill→distribute→outcome, auto-promote at 95% success); OQ-14 answered; Amendment A4; 1660+ tests, exit gate green |
| 2026-08-24 | **R4 Architecture Amendment approved** — R4 split into R4-0/R4-1/R4-2/R4-3; reference architecture diagram; DeviceProtocol abstraction; IPMI as first non-Redfish proof point (R4-1); air-gapped LLM workload analysis; OQ-16 partial (IPMI), OQ-18 deferred; design doc at docs/designs/r4-architecture-amendment.md |
| 2026-08-24 | **R4-0 Platform Validation shipped** — 7 phases; unified Docker Compose full-stack (Agent+SM+CC+Console+TimescaleDB+Keycloak+MockSimulator); Alembic migration chains for CC+Console (backfilled 0001_initial.py); structured JSON logging with request-id propagation (JSONFormatter + middleware); health checks (HealthChecker with pluggable probes) + Prometheus metrics (MetricsRegistry with text export); DeviceProtocol abstraction (interface + RedfishProtocol wrapper, 63% of codebase verified protocol-agnostic); E2E exit gate; Amendment A5; 1730+ tests, exit gate green |
| 2026-08-24 | **R4-1 Infrastructure Breadth shipped** — IPMIProtocol (pyghmi IPMI-over-LAN RMCP+ UDP 623, no root; FRU identity, sensor→NormalizedDevice normalization, SEL→log entries, IDENTIFY_LED + atomic SEL_CLEAR); agent wired through DeviceProtocol factory (`bmc.protocol: redfish\|ipmi`, Redfish default; RedfishDeviceProtocol wrapper fixed + made real); ActionExecutor protocol dispatch (allow-list/audit stay agent-side, vendor coupling Redfish-only); MockIPMIBMC in-process test double with fault injection; cross-site correlation R-C2 (site-aware OutcomeAggregator, cross_site_batch pattern ≥2 failing sites, IntelligenceEngine loop wired into CC runtime, OutcomeHistoryRepo+FleetPatternRepo, /api/outcomes/metrics + /api/outcomes/patterns); Console Vendor Reliability page (/reliability); LLM local endpoint validated (llama.cpp /v1 URLs, key-optional auth, opt-in live probe); OQ-16 answered (server BMC extensibility proven); exit gate green |
| 2026-08-24 | **R4-2 Fleet Intelligence shipped** — compliance audit hash chain across all 4 stores (shared `harkeniq/audit/chain.py`, SHA-256 + per-service seq, chained at write, verified on demand via repo methods + `/api/audit/verify` endpoints + agent `verify_audit_chain()`; OQ-20 answered); config drift detection + remediation (ConfigPolicy YAML + drift detector, `collect_config()` on DeviceProtocol, CONFIG_RESTORE action with Dell DellAttributes restore+verify, PlaybookExecutor FINISHED — `_run_action` stub wired to real ActionExecutor with allow-list/audit intact, rollback real, dry-run default per risk register, agent compliance loop + approval-gated playbook execution); firmware inventory (R-AGENT-17/18: cross-vendor version comparator, protocol `collect_firmware_inventory()`, `inventory_interval` loop brought alive, proto tags AgentRegistration.firmware_json + FleetDevice.service_tag/inventory_json, SM Device.firmware, CC fleet cache) + local CVE feed matching (`cc_cve_feed`, import/exposure API, air-gap safe, example bundle in deploy/); warranty/lifecycle (Dell TechDirect adapter per documented OAuth2+asset-entitlements v5, HPE has NO public server warranty API — manual import endpoint instead, `cc_warranty` TTL cache + refresh loop, warranty on fleet list + new `/api/fleet/{id}` detail endpoint fixing pre-existing Console 404, FleetOverview warranty+firmware panels); Amendment A6; 1828 -> 1933 tests, exit gate green |
| 2026-08-24 | **R4-3 Governance & Ecosystem shipped** — community skill marketplace at Console (OQ-22 answered: community→verified→core tiers, `SkillTier.VERIFIED`, submission w/ `parse_skill` schema validation as the untrusted-YAML safety boundary, human review queue, install + stats, promotion gate on RAW counts ≥95%/≥50 exec/≥50 devices, `skill.submit/review/install` permissions, audited on the R4-2 chain, Console UI page); air-gapped LLM serving (OQ-18 closed: llama.cpp compose service under `airgap-llm` profile, SM model-integrity gate `llm_model_path`+`llm_model_sha256` — corrupt/missing model disables LLM, R4-0 HealthChecker finally wired into SM `/healthz` w/ db+model probes and model metadata); firmware update orchestration (OQ-21 answered: SM `firmware_campaigns`+targets, blast-radius wave planner — never 2 devices of one fault domain in a wave, campaign-level human approval, strictly sequential waves, halt-on-first-failure with blue-green standby-bank rollback, DeviceUpdater seam that refuses to advance without a real transport; agent FIRMWARE_UPDATE/FIRMWARE_ROLLBACK actions — risk "high", health-gated preconditions, Redfish SimpleUpdate + task poll + version verify; simulator UpdateService w/ task state machine + failure injection); predictive maintenance infrastructure (CC `predictive.py`: recency-weighted device failure rate, cohort prior, health/warranty modifiers, explicit insufficient_data, `/api/predictive/risk` — deterministic scoring, ML seat reserved pending data accumulation per §3); Amendment A7; 1933 -> 2011 tests, exit gate green |
| 2026-08-24 | **R5-1 Directed-Directive Transport shipped** — the SM→agent transport both R4-3 seams waited on: `sm_directives` table + DirectiveService (queue → deliver-on-agent-poll → settle; audit-chained), new `PollDirectives`/`ReportDirectiveResult` RPCs, agent-side execution in background tasks through its OWN executor (allow list + preconditions + audit apply — delivery is not a policy bypass; kind=action incl. FIRMWARE_UPDATE via playbook-grade path, kind=skill_install via SkillReceiver + hot reload); `AgentDirectedUpdater` implements the firmware DeviceUpdater over directives and is wired into SM runtime — **campaign advance now drives real agents** (proven end-to-end: orchestrator → gRPC → agent → simulator BMC flash → settle → completed); SM `POST /api/skills/install` queues marketplace skills to agents (static-validated before queueing). Also: **console-ui build FIXED** (25 tsc errors → 0; PageHeader accepts action arrays, ConfirmDialog accepts children, FilterBar `date` type, dead imports removed; `npm run build` green for the first time since R2b). 2011 -> 2031 tests |
| 2026-08-24 | **R5-2 Completion + Enterprise Hardening shipped** (Amendment A8, scope approved by Vinod) — marketplace install automation Console→CC→SM→Agent (CC PULLS installs from a Console internal endpoint using the existing CC→Console credential pair — no new trust direction; `marketplace_installs` events at Console, `InstallSkill` RPC CC→SM queueing skill_install directives, `cc_skill_deliveries` durable dedup ledger with failed-push retry, sync loop in CC runtime; full chain proven over real ASGI Console + real SM gRPC); audit-chain multi-replica locking (`pg_advisory_chain_lock`: transaction-scoped PostgreSQL advisory lock held from tail-read through commit, wired into all three service appends, no-op on sqlite — removes the documented GA limitation); CC tenant scoping for `cc_fleet_patterns`/`cc_cve_feed`/`cc_warranty` (tenant columns, warranty composite PK (tenant, tag), all repos/APIs/loops threaded — same service tag can carry per-tenant records without leakage). **Deferred by A8 (not abandoned):** Network Intelligence milestone (OQ-16 remainder; gNMI-vs-NETCONF decision deferred WITH it), trained predictive models (data gate). 2031 -> 2049 tests, exit gate green |
| 2026-08-25 | **R6 Network Intelligence scoped — Amendment A9 approved** (decided: Vinod) — anchor: community SONiC container; protocols: gNMI primary (streaming per R-M3) + NETCONF config-ops, both behind DeviceProtocol, NETCONF simulator-gated until a real NETCONF device is available; placement: N0 on-switch from day one (constrained profile); actions: LED locate + counter clear (low) and interface reset/disable (high, T1 quorum + SM + CC approval, redundant-path preconditions, self-preservation invariant). Design doc `docs/designs/network-intelligence-milestone.md`; A9 in spec §9 (with A5–A8 pointer row) |
| next | **R6 Network Intelligence implementation** (per A9: NetworkDevice model, GNMIProtocol, NETCONFProtocol, switch simulator w/ fault injection, N0 packaging, port baselines + probe integration, SM/CC network surfaces, exit gate design-doc §4) — or trained predictive models once outcome data accumulates |

## Locked engineering decisions

- **Stack:** Python/FastAPI + SQLAlchemy + PostgreSQL (TimescaleDB for telemetry) for all
  server-side services; React + TypeScript consoles; existing compiled gRPC proto reused.
- **Identity:** self-hosted Keycloak — one realm per tenant + a platform realm; same
  pattern runs air-gapped. 7 fixed roles + tenant custom roles (spec §4).
- **Billing:** in-house ledger-first core; `PaymentProvider` adapter interface with
  Razorpay (India/INR) + Stripe (US/EU) routed by tenant region; annual node commit +
  monthly high-water true-up; delinquency = grace → console restriction → manual suspend
  (spec §5).
- **Never remotely disable on-prem agents or Site Managers** — not for non-payment, not
  for anything (R-H7). Diagnosis is safety infrastructure.
- **Payment-code rigor:** zero compromise; implement gateway behavior only after reading
  current Razorpay/Stripe docs; idempotent verified webhooks; no card data stored.
- **Denied actions are final** (D16); approvals per action class, always audited.
- Sovereign/air-gapped deployment is a requirement, not an edge case (doc 01 §7):
  L1–L3 stay single-tenant software; tenancy lives only at L4.
- Open questions live in spec §8 with an owning slice — answer there, don't re-ask ad hoc.

## Dev conventions

- Tests: `cd /home/vinod/HarkenIQ && source .venv/bin/activate && PYTHONPATH= python -m pytest tests/ -q`
  (bare `python` is not on PATH; always use the venv).
- Code layout: agent under `src/harkeniq/`; new services land as `services/site_manager/`,
  `services/central_command/`, `services/console/` (backend) + `console-ui/` (React).
- Full suite green before any slice lands. Milestone commits/pushes only on explicit
  user approval; commit style follows `git log` (short imperative subject).
- Suggestions are Claude's; **product decisions are Vinod's** — surface options, ask
  before assuming.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec
