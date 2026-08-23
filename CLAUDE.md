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
| 2026-08-23 | **R3 re-sliced** — Amendment A2 to spec §9; R3 split into R3a (Safe Autonomy + Outcome Loop), R3b (Intelligence + Full Mesh), R4 expanded; 6 gating OQs answered (OQ-5/6/7/8/10/11); 7 architectural contracts defined |
| next | **R3a — Safe Autonomy + Outcome Loop** |

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
