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
| next | **R4-2 — Fleet Intelligence** (compliance audit chain, config drift remediation, firmware inventory + CVE, warranty/lifecycle APIs) |

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
