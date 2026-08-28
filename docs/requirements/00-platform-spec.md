# 00 — HarkenIQ Platform Master Specification

**Dated: 2026-08-19 · Status: GOVERNING · Owner: Vinod**

---

## §0 Canon and change control

This document is the single governing specification for HarkenIQ engineering. It reconciles
the two previously competing document sets:

- **docs/requirements/01–13** — remain the *engineering canon* for detailed requirements
  (requirement IDs R-M*, R-S*, R-C*, R-X*, R-AGENT-*, etc. stay authoritative).
- **HarkenIQ_PRD.md + HarkenIQ-Platform-Design.md** — supply *product vision, personas,
  commercial tiers, and pricing philosophy*. Their layer names and phase plans are mapped
  here and do not govern build order.

**Change control.** This spec changes only by dated amendment appended to §9. A slice may
narrow its own scope during execution, but may never redefine another slice, rename a
layer, or move a capability between layers without an amendment here. Phases do not
re-litigate scope.

**Supersessions (as of 2026-08-19):**

| Superseded | By | Note |
|---|---|---|
| `TODOS.md` — "Release one is Harken Mesh" (2026-07-27) | This spec §7 | R1 shipped as Diagnostic Foundation (doc 04 R1). Mesh tier/quorum/claims design feeds R3. M1–M10 carried into §8. |
| Doc 02 §8 market phasing (Phases 1–6) | This spec §7 | Market lens only. NOT build order. |
| PRD §8 30/60/90 plan + Platform-Design 5-phase plan | This spec §7 | Business timeline only. NOT engineering slices. |
| `docs/design/harken-mesh-release-one.md` | This spec §7 | Design input to R3 mesh autonomy; not release one. |
| PRD/Platform-Design layer name "Cluster Manager" | "Central Command" | Name retired; see §1. |

---

## §1 Four-layer architecture

```
┌────────────────────────────────────────────────────────────────────┐
│ L4  HARKEN CONSOLE                       (vendor-operated, multi-  │
│     Tenant lifecycle · licensing · billing · support · platform    │
│     admin · fleet-of-fleets statistics            tenant SaaS)     │
└────────────────────────────────────────────────────────────────────┘
              ↑ usage & health aggregates      ↓ licenses & plans
┌────────────────────────────────────────────────────────────────────┐
│ L3  CENTRAL COMMAND                    (one per tenant; vendor-    │
│     Fleet intelligence · learning · authorization · approval UI ·  │
│     audit                    hosted OR on-prem/air-gapped)         │
└────────────────────────────────────────────────────────────────────┘
              ↑ conclusions & incidents        ↓ knowledge & policy
┌────────────────────────────────────────────────────────────────────┐
│ L2  SITE MANAGER                              (one per site)       │
│     Multi-device correlation · incident consolidation · site model │
│     · command brokering · poll-path for node-less devices          │
└────────────────────────────────────────────────────────────────────┘
              ↑ claims & evidence              ↓ commands
┌────────────────────────────────────────────────────────────────────┐
│ L1  HARKEN NODE (MESH)                        (one per device)     │
│     Local detection & diagnosis · baselines/trending · peer        │
│     witness · allow-listed actions                                 │
└────────────────────────────────────────────────────────────────────┘
```

Principle P1 (doc 01): **decide locally, learn globally.**

### L1 — Harken Node (Mesh)
Per-device agent. **Built (R1, shipped 2026-08-19).** Requirements: doc 01 §3 (R-M1–R-M27).
Detects all single-device faults: component failure, single-device trending, subsystem
degradation, intra-device correlation.

### L2 — Site Manager
Per-site correlation, consolidation, brokering. Requirements: doc 01 §4 (R-S1–R-S9).

**Correlation boundary rule (doc 01 §4.3.1, restated — fixed vocabulary):**
*If a fault can be diagnosed using only data available on one device's Redfish API, the
node detects it. If diagnosis requires comparing data from multiple devices, the Site
Manager correlates it.* Site Manager fault classes: shared power event, rack thermal
excursion, batch component failure, network-vs-device ambiguity (with peer quorum).

### L3 — Central Command
Per-tenant fleet intelligence: learning (R-C1), cross-site correlation (R-C2), human
approval interface (R-C3), authorization governance (R-C4), autonomy posture (R-C5),
audit (R-C6), integrations (R-C7), inventory (R-C8), safe degradation (R-C9).
Requirements: doc 01 §5. Equals the PRD's "Cluster Manager" (name retired).
Deployable **vendor-hosted or fully on-prem/air-gapped** — the sovereign shape is a
requirement, not an edge case (doc 01 §7).

### L4 — Harken Console (new layer, specified here)
Vendor-operated multi-tenant SaaS, HarkenIQ's own business plane. It is **not** part of
any tenant's diagnostic path and holds **no tenant hardware telemetry** beyond the
aggregates listed in R-H4.

| ID | Requirement |
|---|---|
| **R-H1** | Every Console record is tenant-scoped. No API or query path returns another tenant's data. Tenant isolation is enforced at the data layer, not only in application code. |
| **R-H2** | The platform-admin domain (super admin, support) is isolated from the tenant domain: separate authentication realm, separate session/token audience, separate route namespace. A tenant token can never reach admin APIs and vice versa. |
| **R-H3** | Every platform-admin action (tenant create/suspend, plan change, credit note, role grant, support state change, impersonation if ever added) is written to an append-only audit log with actor, subject tenant, and timestamp. |
| **R-H4** | The only tenant data held at L4: identity/contact data, subscription and ledger data, support tickets, and aggregated usage/health metrics (node counts, site counts, agent versions, uptime aggregates). Never verdicts, sensor data, or device inventory detail. |
| **R-H5** | Console unavailability must not degrade any tenant's diagnosis, approvals, or actions. L4 is management-only; L1–L3 operate fully without it. |
| **R-H6** | Sovereign/air-gapped tenants interact with L4 by signed offline artifacts (license files in, signed usage reports out); connectivity is never a prerequisite for a paid deployment. |
| **R-H7** | Non-payment enforcement acts only on L4/L3-hosted surfaces (console access, plan tier). **On-prem agents and Site Managers are never remotely disabled.** |

---

## §2 Commercial tiers ↔ engineering releases

The PRD's Observe / Approve / Autonomy ladder is the *commercial* axis; R-releases are
the *engineering* axis. Mapping is fixed:

| Commercial tier | Meaning | Enabled by |
|---|---|---|
| **Observe** (free) | Read-only: discovery, normalization, verdicts, trending, TUI, demo | R1 (shipped) |
| **Approve** (paid, per-node) | Human-in-the-loop actions with evidence, approval in seconds, audit; site correlation; consoles | R2a + R2b |
| **Autonomy** (earned) | Proven action classes run unattended within budgets; one-command stop; drop-back to Approve when outcomes degrade | R3 |

Autonomy budgets, error-budget drop-back, and the fleet-wide stop switch (Platform-Design
"Learning Loop") are hereby adopted as R3 requirements alongside doc 04's R2/R3 rows and
the mesh design's tier gating (R-MD15).

---

## §3 Tenancy model

- **Tenant = organization** (a customer). A tenant owns: sites, users, role assignments,
  subscriptions, invoices, support tickets, and exactly **one Central Command**.
- **Identity:** one Keycloak realm per tenant (§4). Vendor staff live in a separate
  platform realm (R-H2).
- **Console data:** row-scoped by `tenant_id` in PostgreSQL; admin domain in separate
  schema. All queries pass through a tenant-scoping layer (R-H1).
- **Site** belongs to exactly one tenant and hosts one Site Manager.
- **Single-tenant guarantee downstream:** L1–L3 remain single-tenant software; tenancy
  exists only at L4. This preserves the sovereign deployment shape unchanged.
- **Onboarding flow (Console v1):** super admin or self-service signup creates tenant →
  tenant owner invited → owner registers sites → Console issues a **license key** (signed
  artifact binding tenant id, plan, node commit, expiry) → key is installed into Central
  Command/Site Manager at deploy time → connected CCs phone home usage (R-H4); sovereign
  CCs export signed usage reports (R-H6).

---

## §4 Identity and RBAC

**Provider:** self-hosted **Keycloak** (OIDC). One realm per tenant + one platform realm.
Customer SSO federation (LDAP/AD/Okta/SAML) attaches at the tenant realm later without
application changes. The same Keycloak deployment pattern runs air-gapped beside an
on-prem Central Command.

**Fixed roles (7):**

| # | Role | Domain | Grants |
|---|---|---|---|
| 1 | **Platform Super Admin** | Vendor | Everything at L4: tenant lifecycle, plans/pricing, billing config, credit notes, role grants, support administration |
| 2 | **Platform Support** | Vendor | Read tenant registry + health aggregates; work support queue. No billing mutation, no tenant lifecycle |
| 3 | **Tenant Owner** (system admin) | Tenant | Org settings, users/roles, sites, subscription + billing view, support tickets; everything Site Admin can |
| 4 | **Site Admin** | Tenant | One or more assigned sites: fleet config, skill deployment, site policies, approval policy config |
| 5 | **Operator / Approver** | Tenant | Day-to-day: view fleet, approve/deny proposed actions (named in audit), acknowledge incidents |
| 6 | **Auditor** | Tenant | Read-only everything + audit/compliance export. No approvals, no mutation |
| 7 | **Viewer** | Tenant | Dashboards and reports only |

**Custom roles:** tenants may define permission bundles (named sets of the same atomic
permissions the fixed roles are built from) and assign them like fixed roles. Fixed roles
are non-editable; custom roles cannot exceed Tenant Owner's ceiling.

**Rules:**
- Action-approval rights are held by Operator and above and are **per action class**
  (R-C4); approvals are attributable, revocable, recorded (R-C3).
- Every admin action at L4 is audited (R-H3); every approval at L3 is audited (R-C6).
- Permission checks are enforced server-side per request; the UI only reflects them.

**Capability × role matrix (summary — authoritative atomic permission list lives with the
Console implementation):**

| Capability | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|
| Tenant lifecycle (create/suspend) | ✓ | – | – | – | – | – | – |
| Plans, pricing, credit notes | ✓ | – | – | – | – | – | – |
| Work support queue (vendor side) | ✓ | ✓ | – | – | – | – | – |
| Org settings, users, roles | – | – | ✓ | – | – | – | – |
| Subscription/billing view, raise tickets | – | – | ✓ | – | – | – | – |
| Site config, skills, policies | – | – | ✓ | ✓ | – | – | – |
| Approve/deny actions | – | – | ✓ | ✓ | ✓ | – | – |
| View fleet dashboards | – | – | ✓ | ✓ | ✓ | ✓ | ✓ |
| Audit/compliance export | ✓ | – | ✓ | – | – | ✓ | – |

---

## §5 Billing strategy

**Engine: in-house, ledger-first.** (Lago's model is the design reference; it is not a
dependency.) Payment gateways sit behind a `PaymentProvider` adapter interface —
provider-neutral core, swappable collection.

### 5.1 Plans and price book

| Plan | Price | Includes |
|---|---|---|
| **Observe** | Free | Open-source agent, standalone operation |
| **Approve** | per-node/month, annual commit | Site Manager + Central Command + Console access, actions with approval |
| **Enterprise** | Approve + platform fee | Sovereign/air-gapped licensing, compliance reporting, priority SLA |

Multi-currency **price book**: per-plan per-currency rates (INR, USD, EUR at launch),
versioned; a subscription pins the price-book version it was sold under. The per-node
number itself is a business input (open question OQ-17), configurable — not hardcoded.

### 5.2 Metering pipeline

1. **Source:** Central Command snapshots **nodes-under-management** per site daily and
   reports `{tenant, site, date, node_count, agent_versions}` to Console (R-H4).
   Air-gapped: CC exports a **signed usage report** (Ed25519, license-key-bound) monthly;
   tenant admin uploads it to Console (R-H6). Missing reports >60 days → flag for
   Platform Support follow-up; never auto-disable (R-H7).
2. **usage_events** table: append-only raw snapshots.
3. **Rating:** monthly billable quantity = **high-water mark** (maximum daily node count
   across the tenant during the billing month). Simple, auditable, spike-visible.
4. **Ledger:** immutable `invoices` + `invoice_lines` + `credit_notes`; corrections only
   by credit note, never mutation. Every line traces to usage_events or a commit.

### 5.3 Charge model — annual commit + monthly true-up

- Tenant commits **N nodes** for 12 months → commit invoice issued up front (annual or
  quarterly per contract).
- Each month, metering runs; **overage = max(0, high-water − N)** billed monthly in
  arrears at the per-node rate.
- Under-use does not credit back (commit is the floor); commit raises mid-term are
  prorated to term end.

**Worked example.** Commit 200 nodes on Approve at $30/node/month → commit invoice
200 × 30 × 12 = **$72,000** up front. March snapshots peak at 227 nodes → high-water 227,
overage 27 × $30 = **$810** invoiced April 1, net-30. April peak 195 → no overage invoice.

### 5.4 PaymentProvider adapter contract

```
PaymentProvider:
  ensure_customer(tenant) -> provider_customer_id
  create_payment(invoice) -> payment_intent {url | instructions}
  handle_webhook(raw) -> normalized event   # idempotent by provider event id
  refund(payment, amount) -> refund_ref
  reconcile(date_range) -> [discrepancies]  # daily ledger-vs-provider job
```

- **Adapters:** `RazorpayAdapter` (INR, GST invoice fields, GSTIN), `StripeAdapter`
  (USD/EUR, VAT fields). Both certified in Console v1.
- **Routing:** tenant billing country selects the adapter (India → Razorpay; US/EU/rest →
  Stripe). Exactly one active adapter per tenant; super-admin override (R-H3-audited).
- **Rigor rules (standing):** payment behavior is implemented only after reading current
  Razorpay/Stripe documentation — never from assumption. Webhooks are verified
  (signature) and idempotent. **No card data is ever stored** — gateway-hosted checkout
  only. Ledger is source of truth; providers are collection channels.
- Manual payments (bank transfer — the common enterprise path) are recorded against the
  same ledger; the gateway is optional per invoice.

### 5.5 Delinquency state machine

```
CURRENT ──invoice past due──▶ OVERDUE ──day 14──▶ RESTRICTED ──super admin──▶ SUSPENDED
   ▲            (banners + email d0/d7/d13)  (console = billing pages only;   (console locked;
   │                                          tier drops to Observe: CC       usage sync paused)
   └────────────── payment received ───────── authorizes no new Approve-tier ──────┘
                   (auto-restore ≤1h)         actions)
```

**Never** at any state: remote disabling of on-prem agents or Site Managers (R-H7).
Diagnosis keeps running; a safety product does not brick monitoring over an invoice.

### 5.6 Statistics surfaces

- **Tenant (Owner/Auditor):** subscription state, commit vs actual node usage over time,
  per-site node breakdown, invoice/credit-note history with drill-down to usage days,
  upcoming true-up estimate.
- **Super admin:** tenant directory with plan/ARR/usage, MRR/ARR roll-ups, overage trends,
  delinquency dashboard, reconciliation exceptions.

---

## §6 Support model

- **Tenant side (Console):** raise ticket (subject, severity, site/device context
  optional, attachments), view thread, reply, close.
- **Vendor side:** queue with states `open → acknowledged → in_progress → waiting_on_tenant → closed`,
  severity (S1–S4), assignment to Platform Support users, SLA-due field per plan
  (Enterprise = priority SLA per PRD §9).
- Email notifications on state change both directions. All transitions audited (R-H3).
- Scope guard: support is product-defect response (PRD §9) — "we are a product company,
  not an outsourced ops desk."

---

## §7 Development slices (frozen build order)

| Slice | Contents | Exit gate |
|---|---|---|
| **R1 — Diagnostic Foundation** ✅ DONE 2026-08-19 (`16cfca8`, `5128fbd`) | Node agent: Redfish Dell/HPE polling + normalization, mock simulator, skill YAML engine, Welford baselines / OLS trending / debounce, 7-state machine, SQLite checkpoint, peer heartbeat + witness, action queue + CLI approval + audit, gRPC reporter stub, TUI, `harken demo` | 498 tests green; demo exits clean <10 s |
| **R2a — Site Manager** | FastAPI service + gRPC server implementing `AgentService` (receiver for existing proto); PostgreSQL/TimescaleDB persistence; site model (topology, racks, power/cooling fault domains — R-S3, CMDB import per OQ-1); multi-device correlation per §1 boundary table (R-S4); parent/child incident consolidation (R-S5); action approval brokering (R-S6); agent↔SM auth (mTLS or token — closes doc 10 insecure-channel gap, R-X14); SM web dashboard v1 (React); coverage map (M9); `harken peers list` + `harken bmc test` stubs closed | Two mock simulators + two agents: injected shared-fault (e.g. PDU) consolidated into ONE parent incident with children, approval brokered through SM — end-to-end test |
| **R2b — Central Command skeleton + Harken Console v1** | CC: SM registration, tenant fleet view, approval UI, audit store, usage reporter. Console: Keycloak (realms, platform/tenant split), tenancy + RBAC (7 fixed + custom roles, §4 matrix), tenant registry + onboarding + license keys (§3), support tickets (§6), billing core + Razorpay & Stripe adapters + statistics UI (§5) | Tenant onboards self-service → license key → CC registers → usage metered → invoice generated with correct commit/true-up math → paid via routed gateway sandbox; RBAC matrix enforced server-side and audited |
| **R3a — Safe Autonomy + Outcome Loop** | Distributed autonomy budget enforcement (CC sets, SM enforces, agent enforces locally); expanded action allow-list (SEL clear, BMC reset, power cycle, power cap adjust) with consistent action model (A2.1); risk-degraded authorization leases (A2.2); tier gating T1/T2/T3 from existing peer count; stop switch (fleet-wide halt); blast radius caps per fault domain; correlated-conclusion suppression (A2.6); post-action verification + outcome tracking; error-budget drop-back; SM knowledge base (past incidents, resolutions, skill execution history); basic OS signals (syslog/dmesg for hardware errors, hardware-to-OS device mapping); per-agent Ed25519 identity with signed leases and outcomes (A2.4); tiered resource profiles with defense-in-depth enforcement (A2.5); `harken diagnose` CLI; 7 architectural contracts (A2.7) | Agent autonomously executes a low-risk action (SEL clear) within budget on a healthy-baseline device → outcome tracked → success rate visible. Agent loses SM contact → medium-risk actions drop to propose-only within lease window → observe-only after expiry. Correlated event (3+ devices same fault domain) → autonomy suppressed → human resolves parent → autonomy resumes. Full test suite green. |
| **R3b — Intelligence + Full Mesh** | LLM integration at SM tier (reasoning engine, LLM Explain); LLM-assisted skill generation (candidate skills from novel resolutions); skill distribution pipeline (CC→SM→agent, versioned, canary deployment); skill validation pipeline; full mesh protocol (quorum disambiguation, claim/lease, signed claims, partition fencing); full hardware-to-application mapping (drive→filesystem→process→service); multi-step playbooks; SM credential brokering (JIT from Vault/CyberArk); fleet-wide pattern learning at CC (R-C1 full) | Specced by amendment when R3a ships |
| **R4 — Fleet Intelligence + Breadth** | Cross-site correlation (R-C2); config compliance auto-remediation; firmware update orchestration; network device monitoring (gNMI/NETCONF/SSH); multi-device-class credential rotation; compliance-grade audit (cryptographic chaining); warranty/lifecycle via vendor APIs; community skill marketplace; vendor reliability comparison; full air-gapped LLM; predictive maintenance | Specced by amendment |

Rules: full test suite green at every slice landing; milestone commits only on explicit
user approval; slices execute in order (R2a → R2b → R3 → R4) unless amended in §9.

---

## §8 Open questions register

Every known open question now has an owning slice. Questions are answered (and recorded
here) no later than the start of their owning slice.

| ID | Question | Source | Owning slice |
|---|---|---|---|
| OQ-1 | CMDB/topology import for the site model — design partner's source of truth? | doc 04 §6 Q4 | R2a — **answered, A1.1** |
| OQ-2 | Workload scope: containers / VMs / bare metal for OS-level signals? | doc 04 §6 Q6 | R3 |
| OQ-3 | Hardware-action approval workflow: native only, or ServiceNow/Jira/ticketing integration? | doc 04 §6 Q7 | R2b (native) / R3 (integrations) |
| OQ-4 | Dual-tier fault injection on real hardware without outage (mesh success criterion 1) | TODOS M1 | R3b-2 — **answered, A3.1** |
| OQ-5 | Release action allow-list for autonomy (beyond R1's LED/diagnostics/fan-reset) | TODOS M2 | R3a — **answered, A2.1** |
| OQ-6 | Authorization lease duration vs partition detection time | TODOS M3 | R3a — **answered, A2.2** |
| OQ-7 | Baseline confidence refusal threshold (propose-vs-act cutoff) | TODOS M4 | R3a — **answered, A2.3** |
| OQ-8 | Node identity, key issuance, rotation, revocation (signed claims/actions) | TODOS M5 | R3a — **answered, A2.4** (rotation deferred to R3b) |
| OQ-9 | Peer-set assignment where topology discovery is absent | TODOS M6 | R2a — **answered, A1.1** |
| OQ-10 | Node resource ceilings, enforced + observable | TODOS M7 | R3a — **answered, A2.5** |
| OQ-11 | Correlated-conclusion suppression (shared upstream cause) | TODOS M8 | R3a — **answered, A2.6** (parent incidents in R2a, suppression in R3a) |
| OQ-12 | Coverage-map presentation: silent device = unobserved, not healthy | TODOS M9 | R2a |
| OQ-13 | Two-device correlation probe: R3 or R4? | TODOS M10 | R3b-2 — **answered, A3.2**: implemented as CorrelationProbe (both-sides error counters, 4-way fault location) |
| OQ-14 | Credential model: SM credential broker vs local encrypted config; rotation (doc 03) | Platform-Design; TODOS C1–C16 | R3b-3 — **answered, A4.1**: CredentialProvider interface (Local+Vault+Mock), blue-green rotation |
| OQ-15 | Application-layer symptom source for cross-layer correlation (Prometheus scrape? logs?) | Platform-Design | R3a (basic: syslog/dmesg hardware-to-OS mapping) / R3b (full: process→service mapping) |
| OQ-16 | Non-Redfish device coverage (Cisco NX gRPC, OneFS REST, SNMP/IPMI fallback) | Platform-Design; telemetry matrix | **CLOSED, A10.2** — R4-1 (IPMI) + R6 (network: gNMI on SONiC, full loop shipped; NETCONF dropped per D13 — absent on community SONiC, partner-site gate). Vendor NOS breadth (Arista/Cisco) is follow-on scope, not part of this OQ |
| OQ-17 | Per-node price point per currency | PRD §9 | R2b (config), business decision |
| OQ-18 | Air-gapped LLM (model, GPU floor) | Platform-Design | R3b (LLM interface at SM) / R4 (full air-gapped serving) |
| OQ-19 | Agent language long-term (Python vs Go rewrite) | Platform-Design | Re-evaluate after R2b; Python governs until amended |

---

## §9 Amendments

### A1 — 2026-08-19 — R2a pre-slice decisions (decided: Vinod)

1. **OQ-1 + OQ-9 answered — site model strategy:** Site Manager auto-discovers all that
   is discoverable (devices via Redfish, BMC location fields, peer adjacency from
   heartbeat mesh) and pre-fills the site model; correlation-based inference proposes
   candidate power/cooling groupings over time as *suggestions*. Operator confirms/edits
   fault domains (dashboard or YAML). Confirmed domains drive correlation at full
   confidence; inferred-but-unconfirmed domains may produce parent incidents only when
   clearly labeled as inferred, at reduced confidence. Rationale: PDU/cooling mapping is
   physically unreadable from server APIs; pure inference has a cold-start problem.
2. **Agent↔SM authentication:** R2a ships gRPC over TLS with a per-site bearer token
   (later derived from the license key). mTLS with per-agent certificates lands in R2b
   together with the OQ-8 PKI/license work. Closes the doc 10 insecure-channel gap
   (R-X14) in two deliberate steps.
3. **OQ-14 answered (scheduling):** agents keep encrypted local BMC credentials through
   R2a/R2b — and permanently as a supported mode, since standalone Observe has no Site
   Manager. SM credential brokering (JIT fetch, R-X12) + rotation (doc 03) move to R3.
4. **Deployment shape:** every new service ships Docker Compose (service + PostgreSQL/
   TimescaleDB; Keycloak joins in R2b). Helm deferred until a Kubernetes customer exists.
5. Development readiness confirmed 2026-08-19; R2a may begin. No further scope questions
   are open for R2a.

### A2 — 2026-08-23 — R3 re-slicing and R3a architecture baseline (decided: Vinod)

R2b shipped 2026-08-22. Per §7, R3 is re-sliced into R3a (Safe Autonomy + Outcome
Loop), R3b (Intelligence + Full Mesh), and the existing R4 is expanded to include
deferred breadth capabilities. Six gating open questions answered below.

**Architectural principle (restated for R3a):** The HarkenIQ Agent remains the local
intelligence and execution layer close to the hardware. The Site Manager adds
site-level context, correlation, policy, and safety controls; it must not replace the
agent's local intelligence. Central Command sets fleet-wide policy; it does not make
per-device decisions. Each layer degrades independently (P3). The Observe → Reason →
Act → Learn loop must close at minimum viable fidelity in R3a.

#### A2.1 — OQ-5 answered: Action authorization model and allow-list

Every autonomous action follows a consistent pipeline:

```
Action → Risk Level → Preconditions → Required Corroboration →
Authority Level → Blast Radius → Verification → Outcome →
Drop-back/Escalation
```

**R3a allow-list expansion (4 new actions):**

| Action | Risk | Preconditions | Corroboration | Authority | Blast Radius | Verification | Drop-back |
|---|---|---|---|---|---|---|---|
| SEL clear | Low | Events forwarded to SM; SEL >80% full | None | SM-authorized (budget) | Unlimited | SEL accessible + empty within 30s | Log and continue |
| BMC reset | Low | BMC unresponsive 3 polls; no in-flight firmware update | None | SM-authorized (budget) | 1 per fault domain per 15min | BMC responds within 120s | Escalate to human; max 1 retry per 4h |
| Power cycle | Medium | T1 tier (≥2 peers confirm unresponsive); OS heartbeat absent >5min | T1 required; T2 = propose only | SM-authorized (budget) + device in eligible list | 1 per fault domain per 30min; max 2 concurrent site-wide | Agent re-registers within 300s | Escalate immediately; no auto-retry |
| Power cap adjust | Medium | Active thermal/power event; target within policy range | None for in-range; T1 for sub-minimum | SM-authorized (budget) | 3 per fault domain per 15min | Power draw changes within 30s | Revert to previous; max 3/device/hour |

R1 actions (LED blink, collect diagnostics, fan reset) remain locally authorized.

**Model-level invariants:**
- Every action is idempotent or explicitly marked non-idempotent (non-idempotent = no auto-retry)
- Every action has an expiry (authorization lease window)
- Every action records pre-state (even if rollback impossible)
- UNKNOWN outcomes always escalate to human, never silently retry
- Refused actions recorded with equal weight (R-M11)
- Risk level determines default autonomy budget tier

#### A2.2 — OQ-6 answered: Risk-degraded authorization lease model

| Parameter | Default | Min | Max | Scope |
|---|---|---|---|---|
| Authorization lease duration | 300s (5min) | 120s (2min) | 900s (15min) | Per-site, configurable |
| Lease grace period | 60s | 30s | 120s | Per-site, configurable |

Lease renewed on every successful heartbeat (30s interval).

**Risk-degraded behavior on SM disconnect:**

| SM State | None/Low Risk | Medium Risk |
|---|---|---|
| Connected + valid lease | Execute within budget | Execute within budget |
| Disconnected + valid lease | Execute within budget | **Propose only** (queue for reconnection) |
| Disconnected + expired lease | Propose only (grace period) | Propose only |
| Disconnected + grace expired | Observe only | Observe only |

**Non-configurable safety invariant:** medium/high-risk actions do not begin once SM
connectivity is lost, even with a valid lease. The agent lacks the altitude to detect
correlated fleet events (R-MD24) or verify blast radius across the fault domain without SM.

**Clarification (approved 2026-08-23):** This refines Platform-Design's original offline
execution model ("agent continues executing locally-cached skills within its autonomy
budget") for partition safety. The agent remains locally intelligent and resilient
during SM disconnection, but autonomy becomes risk-aware when broader site-level
context is unavailable. Previously authorized low-risk local actions continue within
the valid lease; medium-risk or disruptive actions must not newly start autonomously.
The agent continues observing, diagnosing, recording evidence, and proposing the
action for later execution or approval. In-flight actions follow their defined
completion/abort safety semantics and always record the outcome.

In-flight authorized actions (authorized before SM loss) may complete. Lease
configuration changes are audited policy changes, not runtime tweaks.

#### A2.3 — OQ-7 answered: Independent, extensible confidence dimensions

Two independent confidence dimensions gate autonomous action:

| Baseline Confidence | Skill Match | Behavior |
|---|---|---|
| ≥ 0.8 | 100% conditions matched | Eligible to act within autonomy budget |
| 0.5–0.8 | 60–99% matched | Propose only (SM/human review) |
| < 0.5 | < 60% matched | Skip, observe only |

Baseline confidence = `min(1.0, sample_count / min_samples)`. Existing critical-pause
mechanism (5 samples after fault clears before resuming baseline learning) prevents
learning during degradation.

**Extensibility requirement:** confidence dimensions are independent and composable,
not collapsed into a single score. Future dimensions (correlation confidence, outcome
history, peer agreement) plug in alongside without replacing these two. R3a ships
with baseline + skill match only; no additional dimensions unless the architecture
demonstrates a concrete need.

#### A2.4 — OQ-8 answered: Per-agent Ed25519 identity with signed leases and outcomes

**Identity model:**
- Agent generates Ed25519 keypair on first start; private key encrypted at rest
- `agent_id` = `SHA-256(public_key)[:16]` (identity follows the key, not the machine)
- SM stores agent public key during `RegisterAgent` RPC; issues SM-signed agent certificate
  (`agent_id + public_key + tenant + site + expiry`, signed with SM's Ed25519 key)
- SM issues signed authorization leases; agent verifies SM signature
- Agent signs outcome reports; SM verifies agent signature (authentic outcome data)
- Reuses R2b Ed25519 infrastructure (`license_keypairs` table, `cryptography` library)

**Bootstrap trust:** Agent receives SM public key during registration over TLS
(R2a CA-validated channel). Provisioning token authenticates the first registration
(one-time-use). Agent pins SM public key after registration; rejects key changes
without explicit re-enrollment.

**Key loss = re-enrollment:** If agent loses its private key (reinstall, disk
corruption), it must re-register with a new provisioning token as a new identity.
Old identity remains in SM records as "inactive." No silent identity regeneration.

**Identity vs instance:** Hardware rebuild = new identity. Cloned agent = new keypair
on first boot (keypair generation at first boot, not install time). Backup restore =
reconnects if old identity still active at SM, otherwise re-enrolls. Two active agents
cannot share an identity.

**Revocation:** SM stops renewing lease → agent drops to observe-only after expiry.
SM can issue explicit revoke → agent persists `revoked` marker to local checkpoint.
Marker cleared only by successful re-registration with valid provisioning token.
Disconnect/reconnect cannot restore authority without valid reauthorization. No CRL
infrastructure needed (leases are short-lived).

**Key rotation:** deferred to R3b. Abstraction (`AgentIdentity` class with `sign()`,
`verify_sm_authorization()`, `is_valid()`) designed now so rotation adds a `rotate()`
method without interface change.

#### A2.5 — OQ-10 answered: Tiered resource profiles with defense-in-depth enforcement

**Three deployment profiles:**

| Profile | Memory (target/soft/hard) | CPU (target/soft/hard) | Use Case |
|---|---|---|---|
| constrained | 30MB / 40MB / 50MB | 2% / 3% / 5% | GPU servers, dense compute, edge |
| standard (default) | 50MB / 75MB / 100MB | 5% / 7% / 10% | General-purpose servers |
| performance | 100MB / 150MB / 200MB | 10% / 15% / 20% | Management nodes, lightly loaded infra |

Profiles configurable per deployment. Agent cannot be configured for unlimited
consumption (hard limits enforced).

**Defense-in-depth enforcement:**
1. **Outer layer (external):** systemd `MemoryMax` / `CPUQuota` at hard limit.
   Agent ships a systemd drop-in file matching the selected profile. Container
   deployments get equivalent resource limits. Optional but recommended.
2. **Inner layer (self-monitoring):** Agent monitors its own resource usage, throttles
   at soft threshold, degrades at hard limit.

**Capability-aware degradation sequence** (preserves safety-critical functions longest):

```
1. Reduce non-essential telemetry polling frequency
2. Defer expensive analysis (trending regression, baseline recalculation)
3. Summarize telemetry (aggregate instead of per-reading)
4. Preserve: heartbeat, authorization state, action verification, audit
5. Last resort: observe-only (buffer everything)
```

In-flight authorized actions (verification pending, outcome recording) are never
interrupted by resource degradation. Audit, authorization state, action records,
and revocation state are never shed. Resource usage reported in every heartbeat
(observable per R-M23).

#### A2.6 — OQ-11 answered: Fault-domain-aware correlated-conclusion suppression

**Two trigger paths:**

```
Event arrives at SM correlation engine
    ↓
Path 1: Direct shared-dependency evidence?
    (devices share fault domain AND event maps to domain's failure mode)
    YES → Immediate scoped suppression (2+ devices sufficient)
    ↓ NO
Path 2: Statistical correlation (fault domain + event family + window)?
    Threshold reached → Suppress
    ↓ NO
Normal autonomy evaluation
```

**Default suppression policies (configurable per site):**

| Event Family | Fault Domain | Direct Dep? | Threshold | Window |
|---|---|---|---|---|
| Power | PDU / power circuit | Yes | 2 devices | 30s |
| Thermal | Cooling zone / rack | Yes | 3 devices | 60s |
| Connectivity | Network segment / TOR | Yes | 3 devices | 15s |
| Component | Any | No (fallback) | 5 devices | 300s |

**Once triggered:**
- No new disruptive autonomous actions start in affected scope
- Pending actions move to propose (in-flight may complete)
- Parent incident records correlation evidence and suppression reason
- Autonomy does not auto-resume on time window expiry

**Recovery (two paths):**

| Condition | Path |
|---|---|
| Transient, resolved (all devices healthy ≥10min stability period) | Auto-recovery with 1-hour re-suppression hair-trigger |
| Unresolved, ambiguous, or S1/S2 severity | Explicit human re-enable (audited) |

#### A2.7 — Architectural contracts established in R3a

Seven interfaces/data models introduced in R3a for R3b/R4 extension without redesign:

1. **Outcome/Learning data model** — ActionOutcome: action_id, verification_ts,
   pre_state, post_state, outcome (SUCCESS/PARTIAL/FAILURE/UNKNOWN/ROLLBACK),
   fault_resolved, side_effects, operator_override, override_reason
2. **Diagnosis/Evidence model** — Diagnosis: device_id, component, evidence[],
   contradicting[], confidence (per-dimension), tier, trajectory, recommended_action,
   reasoning_path
3. **Skill lifecycle model** — SkillPackage: skill_id, version, vendor, device_types,
   tier, validation_state (draft/tested/canary/promoted/deprecated), test_cases[],
   deployment_history[], outcome_stats
4. **Distributed autonomy enforcement interface** — AutonomyPolicy set at CC, enforced
   at SM site-locally, enforced at agent device-locally when disconnected. Same
   interface at all three scopes.
5. **OS signal abstraction** — OSSignalCollector: pluggable register_source(type),
   collect()→OSEvent[], map_to_hardware(os_device)→RedfishComponent. R3a: syslog+dmesg.
   R3b/R4: journal, smartctl, /proc, application mapping.
6. **Reasoning provider abstraction at SM** — ReasoningProvider: analyze(context)→result.
   R3a: DeterministicReasoner (skill matching) + KnowledgeBaseReasoner (history lookup).
   R3b: LLMReasoner plugs in at SM tier without pipeline change.
7. **Peer/mesh abstraction** — PeerProtocol: get_reachable_peers()→int (tier gating,
   R3a), get_peer_state() (witness, exists). R3b extends with broadcast_claim(),
   receive_claims(), renew_lease(), exchange_suspicion().

#### A2.8 — OQ-2, OQ-13, OQ-14, OQ-15, OQ-18 rescheduled

| OQ | Decision |
|---|---|
| OQ-2 (workload scope) | R3a ships basic OS signal collection (syslog/dmesg) for bare metal and VMs. Container and application-aware scope deferred to R3b. |
| OQ-13 (two-device correlation probe) | R3a uses existing peer count for tier gating. Full two-device correlation probe deferred to R3b with full mesh protocol. |
| OQ-14 (credential brokering) | Agents keep local encrypted credentials through R3a. SM credential brokering (JIT) lands in R3b. |
| OQ-15 (application-layer symptom source) | R3a: hardware-to-OS device mapping via syslog/dmesg. R3b: full process→service mapping. |
| OQ-18 (air-gapped LLM) | R3b defines LLM provider abstraction at SM. R4 implements full air-gapped model serving. |

6. Development readiness for R3a confirmed 2026-08-23. No further scope questions are
   open for R3a.

### A3 — 2026-08-24 — R3b-2 Full Mesh Protocol (decided: Vinod)

1. **OQ-4 answered — fault injection test approach:** Simulated multi-agent test harness
   with in-process agents on loopback, compressed timing (0.2s beat / 0.6s timeout),
   fault injection by killing/pausing agents and dropping heartbeats. No real hardware
   required. The protocol logic is identical; transport is tested by the integration
   harness. Real hardware validation deferred to design partner sites.

2. **OQ-13 answered — two-device correlation probe:** Implemented as `CorrelationProbe`
   class. On LINK_DOWN quorum verdict, both sides report receive-side error counters
   (CRC errors, FCS errors, interface resets, RX errors). Four-way fault diagnosis:
   LOCAL_PORT (our errors, no remote), REMOTE_PORT (their errors, no ours), CABLE
   (both sides), INCONCLUSIVE (no errors detected).

3. **Peer key distribution — SM-brokered:** SM distributes peer public keys in
   `RegistrationAck.peer_keys` (map<agent_id, public_key_pem>), SM-signed with
   `peer_keys_signature`. Agent verifies bundle with pinned SM public key before
   trusting any peer key. No direct peer-to-peer key exchange.

4. **Claim transport — UDP with envelope:** Claims travel over the existing heartbeat
   UDP port using a 1-byte message type prefix: 0x01=heartbeat, 0x02=claim,
   0x03=claim_ack, 0x04=suspicion. Thin reliability layer: retransmit until all
   peers ack or max retries (5) exhausted.

5. **Claim ownership protocol (R-M15 through R-M18):**
   - First-claim wins; ties broken by lexicographically lower agent_id (deterministic,
     not timestamp-based — clocks drift under network impairment per R-M15).
   - Claim subject is always the device (R-M16); link vs device is a conclusion.
   - Claim lease duration: 120s (shorter than SM auth lease 300s).
   - Lapsed lease returns incident to claimable with inherited evidence (R-M17).
   - Isolated node (0 reachable peers) cannot claim (R-M19).

6. **Quorum disambiguation (§3.4, 4-way):**
   - DEVICE_DOWN: all neighbours lost device, reach each other.
   - LINK_DOWN: one+ neighbours still reach it.
   - NODE_FAILED: link up, agent silent (check_node_failed refinement).
   - ISOLATED: lost every neighbour simultaneously (R-AGENT-6: self-report).
   - INCONCLUSIVE: insufficient observers (< 2 per R-M14).

7. **Suspicion exchange (R-M20 through R-M22):**
   - Per-component float scores from local observations and peers.
   - Time-based decay (configurable rate, default 0.01/s).
   - Threshold-triggered claims when cross-node evidence >= threshold AND >= 2 observers.
   - Greedy set-cover for smallest explaining set (R-M21).
   - Bundle coverage tracking for synthetic measurement (R-M22).

8. **Partition fencing (R-M19, R-AGENT-6, A2.2):**
   - All-peers-lost detection triggers ISOLATED state.
   - Isolated node is fenced: propose-only (T2), cannot execute actions.
   - ClaimManager respects both claim lease AND authorization lease.
   - Recovery: fence lifts when any peer returns.

#### A3.9 — Architectural summary

| Component | File | Purpose |
|---|---|---|
| PeerKeyRing | `autonomy/peer_keyring.py` | Store + verify peer Ed25519 public keys |
| Message envelope | `heartbeat/protocol.py` | 1-byte type prefix for UDP multiplexing |
| Claim / ClaimAck | `autonomy/claim.py` | Data model, wire format, Ed25519 signing |
| ClaimExchange | `autonomy/claim_exchange.py` | UDP broadcast + ack reliability layer |
| ClaimManager | `autonomy/claim_manager.py` | First-claim-wins, lease management |
| QuorumEngine | `autonomy/quorum.py` | Four-way disambiguation |
| SuspicionTracker | `autonomy/suspicion.py` | Continuous suspicion + threshold claims |
| CorrelationProbe | `autonomy/correlation_probe.py` | Two-device fault location |
| PartitionFence | `autonomy/partition_fence.py` | Isolation detection + fencing |
| PeerProtocol | `autonomy/peer_protocol.py` | Contract 7 facade (all 4 stubs implemented) |

### A4 — 2026-08-24 — R3b-3 Advanced Remediation + Fleet Learning (decided: Vinod)

1. **OQ-14 answered — credential provider interface:** `CredentialProvider` protocol
   with three implementations: `LocalCredentialProvider` (existing encrypted config,
   permanent fallback per R-H7), `VaultCredentialProvider` (HashiCorp Vault KV v2 via
   httpx, no SDK dependency), `MockCredentialProvider` (CI/testing). `CredentialProviderChain`
   tries Vault first, falls back to Local. Same httpx async pattern as LLMProvider.

2. **Credential rotation — blue-green pattern:** `CredentialRotator` implements create new
   account → verify new → disable old → update store. Rollback if verify fails (re-enable
   old, delete new). Audit trail for every rotation event. Uses Redfish AccountService API.

3. **Multi-step playbooks:** `Playbook` model with ordered `PlaybookStep` list. Each step:
   action_type, preconditions, verification_checks, rollback_action (optional), credential_required.
   `PlaybookExecutor` orchestrates sequential execution with per-step verification, rollback on
   failure, pause for human review on partial, resume capability. Three built-in playbooks:
   BMC_RECOVERY, THERMAL_MITIGATION, DISK_REPLACEMENT_PREP.

4. **Fleet outcome reporting:** `FleetOutcome` proto message added to `FleetSnapshot`. SM
   populates unreported outcomes from `sm_action_outcomes` table (watermark-based). CC ingests
   into `cc_outcome_history` table during existing fleet poll cycle. Zero new infrastructure.

5. **CC outcome aggregation:** `OutcomeAggregator` groups outcomes by (action_type, vendor,
   model), computes success_rate, failure_rate, resolution_rate. Snapshot-based trend detection
   for anomaly identification.

6. **Fleet pattern detection (R-C1):** `PatternDetector` detects three pattern types:
   batch_failure (action fails above threshold on specific model), anomaly (failure rate
   increase), reliability (model-specific rate worse than fleet average). Dedup by scope.
   Results stored in `cc_fleet_patterns` table.

7. **Knowledge distribution (R-C1 loop):** `KnowledgeDistributor` routes detected patterns
   to Site Managers whose fleet inventory matches the affected scope (vendor/model). Serializes
   patterns for existing PushPolicy RPC channel. No new RPC needed.

8. **Learning feedback tracker (R-C1 complete):** `LearningFeedbackTracker` tracks full cycle:
   outcome → pattern → skill → distribution → outcomes. Computes improvement percentage.
   Auto-promotion criteria: success_rate >= 95% across 50+ devices.

#### A4.9 — Architectural summary

| Component | File | Purpose |
|---|---|---|
| CredentialProvider | `security/credentials.py` | Local + Vault + Mock + Chain |
| CredentialRotator | `security/credential_rotation.py` | Blue-green BMC account rotation |
| Playbook / PlaybookStep | `actions/playbook.py` | Multi-step data model + built-ins |
| PlaybookExecutor | `actions/playbook_executor.py` | Step orchestration + verification |
| OutcomeAggregator | CC `outcome_aggregator.py` | Fleet-wide outcome metrics |
| PatternDetector | CC `pattern_detector.py` | Batch failure / anomaly / reliability |
| KnowledgeDistributor | CC `knowledge_distributor.py` | Pattern routing to SMs |
| LearningFeedbackTracker | CC `learning_feedback.py` | R-C1 complete loop tracking |
| CCOutcomeHistory | CC `db/models.py` | Outcome persistence for learning |
| CCFleetPattern | CC `db/models.py` | Detected patterns persistence |

### A5–A8 — 2026-08-24 — recorded in `docs/designs/r4-architecture-amendment.md`

A5 (R4-0 platform validation), A6 (R4-2 shipped), A7 (R4-3 shipped), and A8
(R5-2 scope + Network Intelligence deferral) were recorded in the R4
architecture amendment document rather than here; that document is part of the
amendment record.

### A9 — 2026-08-25 — R6 Network Intelligence scope (decided: Vinod)

1. OQ-16 remainder becomes slice **R6 — Network Intelligence**: full
   Observe→Reason→Act→Verify for network switches.
2. Anchor device: community SONiC (container). Protocols: gNMI (primary,
   streaming telemetry per R-M3) + NETCONF (config ops), both behind
   DeviceProtocol. NETCONF is simulator-validated only until a real
   NETCONF-capable device is available — explicit open gate.
3. Placement: N0 on-switch from day one (SONiC app container, constrained
   profile per A2.5); off-box operation is the inherent fallback.
4. Actions: LED locate, counter clear (low risk); interface reset and
   interface disable (high risk, T1 quorum + SM + CC approval, redundant-path
   preconditions, self-preservation invariant: never sever own management
   path or last redundant uplink).
5. Deliverables: NetworkDevice model, GNMIProtocol, NETCONFProtocol, switch
   simulator with fault injection, N0 packaging, port baselines + probe
   integration, SM/CC network surfaces. Exit gate per design doc
   `docs/designs/network-intelligence-milestone.md` §4.

### A10 — 2026-08-26 — R7 Demo Hardening campaign + autonomy semantics (decided: Vinod)

1. **R7 "Demo Hardening" is a slice** (proposed in
   `docs/designs/production-demo-readiness.md` §7, executed 2026-08-25/26).
   Scope: boot truth (compose gate), demo truth, wiring what was built
   (autonomy chain, OS signals), auth reality, and campaign fixes QA-001
   through QA-041 (`docs/qa/r7-bug-register.md` is the record). The exit
   gate is `scripts/e2e-compose-gate.sh`: boots the real stack, drives the
   ten-step scenario, asserts the persisted agent identity chain, and fails
   on any ERROR-level service log.
2. **A9 point 2 amended per D13 (R6-P0 finding):** NETCONF is absent on
   community SONiC, so NETCONFProtocol was dropped from R6 deliverables
   entirely — not simulator-validated. It returns only if a partner-site
   NETCONF device materializes (design doc §5 gate). gNMI carries both
   telemetry and config ops on the SONiC anchor. OQ-16 network half stands
   answered on this narrowed basis.
3. **Approval-vs-lease semantics ratified** (implemented in R7 QA-020):
   stop switch, failed preconditions, fully-expired lease, and blast-radius
   limits refuse even human-approved actions — approval does not make
   unsafe safe. Lease class-membership and budget "propose" verdicts ARE
   satisfied by a carried human approval while no T3 autonomous loop exists;
   revisit when T3 lands.
4. **CC budget→action-class mapping ratified** (documented in
   `harkeniq_cc/policy_push.py`): only `device_type="*"` budget rows map to
   lease grants; autonomy levels 0/1 grant nothing; level 2 grants
   SEL_CLEAR + BMC_RESET (low risk); level 3 adds POWER_CYCLE,
   POWER_CAP_ADJUST, CONFIG_RESTORE (medium). High-risk actions
   (FIRMWARE_*, INTERFACE_*) are NEVER budget-granted — they keep their
   dedicated per-action approval paths regardless of autonomy level.
5. **R3b-3 rotation claim made real (QA-034):** the blue-green credential
   rotation's four Redfish AccountService calls are implemented (create via
   POST with 405 fixed-slot fallback, verify by fresh session as the new
   account, disable via PATCH, rollback delete) and proven against the
   simulator's AccountService. OQ-14's rotation answer needs no de-claim.

### A11 — 2026-08-28 — Tenant plane separation + service placement registry (decided: Vinod)

Scope amendment for the Console work landing as PRs #9–#11 (with #7/#8 already on
main). Product decisions were made in-session by Vinod; this records them under
change control before merge.

1. **Tenant service placement registry (new architectural concept).** The vendor
   Console resolves each tenant's L1–L3 stack through an authoritative
   `tenant_services` registry (tenant → service kind → endpoint), not through a
   global configured URL. Resolution is **fail-closed**: a tenant with no active
   placement is refused (503), never handed a shared or default endpoint.
   `cc_url` survives only as a single-tenant startup seed that writes an explicit
   registry row. Rationale: §3 gives each tenant exactly one Central Command and
   L1–L3 stay single-tenant; the Console is therefore the component that must
   know, per tenant, which stack is whose — and must never guess.
2. **One tenant → one Central Command is an enforced invariant.** Registering an
   endpoint already active for another tenant is refused (409) with a DB unique
   backstop. CC has no per-tenant data filtering, so a shared endpoint would
   silently serve one tenant's data under another tenant's URL. If shared CC is
   ever wanted (scale/cost), it requires explicit tenant isolation inside CC and
   arrives only by a future amendment — never as a registration side effect
   (OQ-26).
3. **Tenant context lives in the URL** (`/t/{tenantId}/…`), not in a header or
   browser storage. A platform user is never placed inside a tenant
   automatically; entering is an explicit act from the tenant registry. The
   `current`-alias middleware, `X-Harken-Tenant` header, and client-side tenant
   selector are removed.
4. **Listing a tenant and entering one are different acts.** The registry is
   readable by platform staff holding `tenant.view`; because the atomic
   permissions are shared vocabulary (tenant roles hold some of the same names),
   platform-plane routes check *platform realm AND permission*
   (`require_platform_permission`). Entering a tenant is governed by
   `tenant_scope`: membership for tenant users; for `platform_support`, an
   approved, time-bound, requester-bound support-access grant
   (request → super-admin approval → TTL clock starts at approval; one approval
   admits exactly the engineer who requested it). `platform_super_admin` keeps an
   unconditional break-glass at L4 by design.
5. **Marketplace installs are tenant-explicit.** The install API accepts the
   target tenant, validated by the same tenant-scope gate; a tenant user may name
   only their own tenant. (Closes the silent no-op where a platform user's
   install recorded nothing and CC never delivered.)
6. **Deliberately NOT decided here:** cross-realm authentication for platform
   staff at per-tenant CCs (OQ-23), auditor scope (OQ-24), deny-then-re-request
   semantics (OQ-25). These are open questions with owners in §8; no
   implementation may assume their answers.
