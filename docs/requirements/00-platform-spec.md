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
Console implementation; Auditor's canonical read scope is defined by A13: every atomic
`*.view` permission plus `audit.export`, and nothing else):**

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
| OQ-23 | Cross-realm auth for platform staff at L3 | 2026-08-28 review (adversarial pass, verified against `harkeniq_cc/auth.py`) | **answered, A12** — vendor staff never touch L3 live by default (spec-literal role 2); a CC-verified signed grant assertion is the only sanctioned future mechanism |
| OQ-24 | Auditor scope: prose vs implemented 5-permission set | 2026-08-28 review | **answered, A13** — prose is canonical: read-only everything + export; three read-gate follow-ups recorded |
| OQ-25 | Support-access denial semantics vs D16 | 2026-08-28 review (testing pass) | **answered, A14** — denial is non-final; re-request allowed; the approver sees the engineer's denial history at decision time; D16 stays hardware-specific |
| OQ-26 | Shared Central Command for scale/cost: registration now enforces one-tenant-one-CC (409). If sharing is ever wanted, it requires explicit tenant isolation INSIDE CC — a deliberate architecture decision, never a registration side effect (decided: Vinod, 2026-08-28) | 2026-08-28 review | future — reopen only by amendment |

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
### A12 — 2026-08-28 — OQ-23 answered: vendor staff at tenant Central Commands (decided: Vinod)

0. **Scope of this answer — the tenant architecture is unchanged.** Choosing the
   operating default below alters nothing about how HarkenIQ is structured for
   tenants. Tenant isolation, the tenant-specific CC/SM/agent topology (one CC
   per tenant, §3; L1–L3 single-tenant), explicit URL-scoped tenant context,
   permission-based RBAC, the subscription model, and tenant data boundaries
   (row-scoped Console data, fail-closed service placement) remain core
   architecture exactly as amended in A11. OQ-23 defines one thing only: the
   **trust boundary for HarkenIQ platform staff accessing customer
   infrastructure** — and the answer is that, by default, that boundary is
   closed. Point 2 ("B") remains the sole sanctioned, controlled live-support
   extension should a future slice need it.

1. **Operating default (effective now): spec §4 role 2 is literal.** Platform staff
   work vendor-side — tenant registry, health aggregates from phone-home usage
   (R-H4), the support queue, and Console-plane tenant data under an approved,
   requester-bound support-access grant. Live L3 access for vendor staff does not
   exist: a tenant's CC validates only its own realm, refuses platform-realm
   tokens, and that refusal is the intended behavior, not a defect. Deep
   diagnosis is customer-mediated (customer-granted account in *their* realm,
   screen-share, on-site). The single-realm demo is the only environment where
   platform staff see live L3, and it must be presented as such.
2. **The only sanctioned future mechanism ("B"), built when a real support case
   demands it and only by its own slice:** a connected tenant's CC may
   additionally accept the vendor platform realm **iff** the request carries a
   Console-signed grant assertion — requester-bound, tenant-bound, TTL'd,
   verified against the vendor Ed25519 trust CC already holds for licensing —
   mapping platform_support to read-only and logging the engineer in CC's own
   audit chain. It must be tenant-disableable (config, default off), and is
   structurally absent in customer-run-Keycloak and sovereign shapes.
   Token-exchange / Console-as-token-authority designs are rejected: they make
   vendor staff indistinguishable from tenant users at CC and concentrate
   every tenant realm's credentials at L4.
3. **Follow-up recorded, independent of this answer:** the Console SPA bakes one
   Keycloak realm at build time (`VITE_KEYCLOAK_REALM`), so multi-tenant login
   needs realm discovery (tenant slug → realm at the login page). Work item, not
   an open question.

### A13 — 2026-08-28 — OQ-24 answered: auditor scope is read-only everything (decided: Vinod)

1. **The §4 prose is canonical; the matrix was the stale artifact.** The Auditor
   (tenant-domain role 6) holds **every atomic `*.view` permission plus
   `audit.export`, and nothing else**: `fleet.view`, `incident.view`,
   `billing.view`, `audit.view`, `audit.export`, and — added by this amendment —
   `user.view`, `license.view`, `support.view`, `site.view`. Rationale: an
   auditor who cannot read users and role bundles cannot perform an access
   review, and the predictable consequence of a narrow auditor is compliance
   staff borrowing `tenant_owner` credentials — a strictly worse outcome than
   any read expansion. Three spec sources already describe the read-everything
   persona (§4 prose, §5's explicit Owner/Auditor billing reports, doc 03's
   R-CR3/R-CV4 auditor-report consumers).
2. **Hard boundaries, unchanged:** no write, administrative, support-elevation,
   infrastructure-action, or privilege-grant capability of any kind — no
   `*.manage`, no `action.approve`, no `incident.acknowledge`, no
   `skill.submit/install`, no API-key or user mutation. The Auditor remains a
   tenant-domain role: the platform plane stays vendor-only
   (`require_platform_permission`, A11.4), strict tenant scoping applies to
   every read (`tenant_scope` + `require_tenant_permission`), and the
   custom-role ceiling does not move (tenant_owner already holds every added
   permission). A12's settled boundaries are untouched.
3. **Three read-gate follow-ups are explicit future work (next Console slice),
   without which the grant is true in the table but not in practice:**
   (a) role-bundle *listing* readable to `user.view` holders (today gated
   `role.manage`, blocking access review of custom bundles); (b) a read path
   for CC approvals history (today CC gates the GETs on `action.approve`,
   which its coarse model grants only to wildcard admins — R-C3's evidence is
   unreadable by its intended reader); (c) a policy-read path (Console page and
   CC reads both require `site.manage`; read-only governance review is
   structurally impossible for any non-admin today). Each is read-only and
   lands by its own reviewed change, not silently.
4. **Sequencing (decided):** spec first, then the resulting permission matrix
   presented for review; code changes only after that review.

### A14 — 2026-08-28 — OQ-25 answered: support-access denial is non-final, history visible (decided: Vinod)

1. **A support-access denial does not permanently deny the person.** The same
   engineer may legitimately request again — context changes between asks
   ("not for this ticket", "not during the window"). No cooldowns, no
   permanent locks, no super-admin unlock machinery.
2. **But the history is never hidden from the next decision.** The approver's
   pending queue shows, per request, the engineer's prior denial history for
   that tenant (count, last denial time, last reason) at the point of
   decision. Read-only enrichment; the audit chain remains the durable record
   (`support_access.requested/approved/denied`), unchanged.
3. **D16 stays specific to hardware-action safety.** "Denied actions are
   final" constrains the MACHINE (the platform never re-proposes a denied
   action); it is not transplanted onto human support-access requests. What
   carries over is its spirit: a denial is never silently erased, and repeated
   asking is visible pressure, not invisible pressure.
4. **Scope of the sanctioned implementation:** the queue-payload enrichment,
   its UI display on the approver page, and regression tests pinning both
   halves (re-request allowed after deny; history present in the queue).
   Nothing else.

### A15 — 2026-08-30 — Approval policy is enforced at decision time (decided: Vinod)

**Trigger.** The pre-S6 architecture review found that
`cc_approval_policies` has carried `approval_mode`, `required_approvers`
and a group link since R2b, that the Console has full CRUD for it, and
that the S5 autonomy contract faithfully reports it — while **no code
path consulted it when a decision was made**. A tenant could configure
dual authorization and receive single authorization, silently. This
amendment writes down what §4 always intended, so the enforcement point
is named and cannot drift again.

**A15.1 — A decision is a set, not a field.** An approval or denial is
recorded per approver in `cc_approval_records`. `cc_approval_routes`
retains `decision` / `decided_by` / `decided_at` as a projection of that
set for compatibility; the ledger is the truth.

**A15.2 — The governing policy is the most specific active match** on
`(action_type, device_type, risk_level)`, with `*` as the wildcard on
each. Action type outweighs device type, which outweighs risk, so a rule
written for one action class always beats a broader rule that happens to
share its risk band. Ties break deterministically. **No policy configured
means one approver**, which is the behaviour every existing tenant has.

**A15.3 — An approver decides a subject once.** Enforced by
`unique(subject_type, subject_ref, approver_ref)` in the database, so it
cannot be lost to a later code path. A second decision from the same
person is refused with 409, never counted twice.

**A15.4 — A denial is terminal** (consistent with D16) and outranks any
number of approvals. An approver who objects cannot be outvoted by
colleagues deciding faster.

**A15.5 — Group membership, when a group is bound**, is matched on the
Keycloak subject first and falls back to the email address, so a rename
cannot silently lapse someone's approval authority.

**A15.6 — Each approval is audited individually**, not only the outcome.
Auditing only the outcome would make a two-approver decision
indistinguishable from a one-approver decision in the record that exists
to prove it. `GET /api/approvals/{id}/records` is the read.

**A15.7 — `approval_mode: "auto_approve"` is refused.** It is rejected on
write and coerced to `require_approval` on read. Reasoning: while
policies were unenforced the mode was inert; enforcing it as written
would make a single policy row a second, ungoverned path to unattended
execution — no evidence bar, no budget, no error-budget drop-back, and no
fence for the risk-`high` classes that `never_budget_grantable` refuses
at **every** autonomy level (A10.4, S5). **The tenant's autonomy contract
remains the one governed answer to "may this run without a human."**
Raising an action class's autonomy level is how it earns that, and only a
human can do it. The Console policy preset offering the mode is a
pre-S5 artifact and is retired.

**A15.8 — One contract, both origins.** Node-proposed actions and
Operational Agent proposals resolve the same policy, write the same
ledger and obey the same completion rule. There is no second approval
contract and no origin-specific exception.

**A15.9 — Approval still never overrides a safety gate** (unchanged,
A10.3). A fully approved action runs the unchanged node funnel and can
still be refused there.

**Approver scope** — an approval counting only within the approver's
authorized scope — is specified here as the intended end state and is
delivered by E1.2, which introduces scope grants. Until then every
approver's authority is tenant-wide, which is today's behaviour stated
explicitly rather than left implicit; the column and the check exist from
E0.1 so the later slice changes no approval code.

### A16 — 2026-08-30 — Site identity is authoritative; a Site Manager may serve many sites (decided: Vinod)

**Trigger.** The pre-S6 architecture review found that `RegisterSite`
received Central Command's `site_id` and discarded it, so CC's site id
and the Site Manager's own primary key were different id spaces that
never matched. Every site-scoped read then widened, silently, to the
whole Site Manager. Harmless while one Site Manager served one site;
a cross-site leak the moment that changed.

**A16.1 — Cardinality.** §3's "a Site belongs to exactly one tenant and
hosts one Site Manager" becomes: **a Site Manager serves one or more
sites, and a site is served by exactly one ACTIVE Site Manager.** §1's
L2 line "one per site" reads "one per site group". A site moves between
Site Managers by being retired at the first before it is bound at the
second.

**A16.2 — The binding is authoritative and is never overwritten.**
Central Command assigns the site id; the Site Manager persists it
(`sites.cc_site_id`, unique). A registration naming a site already bound
to a different identity is **refused**, and the refusal is audited.
Re-registration under the same identity is idempotent; a rename is a
label change and keeps the binding.

**A16.3 — No fallback may broaden scope.** An unresolved site returns an
explicit **empty** result with a stated reason, on both
`GetFleetSnapshot` and `GetUsageSnapshot`. Central Command must not
mistake that for "the site has no devices": its poller skips ingest
entirely, because ingesting an empty snapshot would clear the site's
fleet cache and, through D3 absence inference, resolve every one of its
incidents.

**A16.4 — Every site-scoped read is scoped.** Devices, incidents,
pending actions, action outcomes and candidate skills. The outcome and
candidate reads carry a `reported_to_cc` watermark, so an unscoped query
did not merely show another site's rows, it **consumed** them and that
site never received its own evidence. Rows without a device, and
therefore without a site, ride no snapshot at all.

**A16.5 — Correlation stays strictly per site.** Unchanged and
restated: every correlation rule takes a site id, and a Site Manager
serving several sites never correlates across them. Incident resolution
helpers that decide per device on that device's own state are
unaffected, because they compare nothing across sites.

**A16.6 — Error budgets are per site.** `sm_error_budgets` is keyed
`(site_id, action_type)`. The Site Manager remains the execution and
safety boundary; what is per-site is the **evidence** and the autonomy
withdrawal it justifies. A failure pattern at one site must not reduce
another site's autonomy, and recovery lifts one site's drop-back only.
The lease an agent receives is gated by that agent's own site.

**A16.7 — Metering is per site.** `GetUsageSnapshot` counts the
requested site's devices. It previously returned the whole Site
Manager's count labelled with one site id, which on a multi-site Site
Manager would have billed every site for the entire fleet. An unresolved
site meters zero.

**A16.8 — Break-glass rebind.** Recovery from a legitimately changed
Central Command identity (a restore from backup) is an explicit,
audited unbind at the Site Manager's site-token API, requiring the
site name as a typed confirmation and a stated reason. Registration
itself never overwrites. Unbinding clears only the tenant-plane
identity; devices, incidents, actions and outcomes stay exactly where
they are, and until the site is re-bound its snapshot is empty.

**A16.9 — The Site Manager's trust boundary is unchanged.** The site
token authorizes the Site Manager, which remains the execution and
safety boundary for every site it serves. Per-site authority for people
and agents is enforced at Central Command and arrives with E1.2. No
second authorization model is introduced here.

### A17 — 2026-08-31 — The Capability Registry: capability is a declared fact, not an assumption (decided: Vinod)

**Trigger.** The platform governs fourteen action classes and could
state, for any of them, its risk, its preconditions, its blast radius,
its approval policy and whether an autonomy budget grants it. It could
not state whether **any code existed to execute it**. Two classes turned
out to have none: `INTERFACE_RESET`, which no protocol has ever
implemented, and `CLEAR_COUNTERS`, which R6 correctly refused to fake
because SONiC exposes counter clearing only over CLI. Both were fully
governed, both were bindable to an Operational Agent, and the agent's
own condition table mapped an interface condition straight to
`CLEAR_COUNTERS` — so a proposal would be made, a human would approve
it, a directive would be dispatched, and the node would refuse it. Every
time, with nothing upstream able to say why.

**A17.1 — Capability is its own question.** Six questions govern an
action and none substitutes for another:

| Question | Answered by |
|---|---|
| **Can** this be executed at all? | the Capability Registry |
| Who may ask for it? | RBAC permissions |
| Where may they ask for it? | scope grants (E1.2) |
| May it run unattended? | the autonomy contract (S5) |
| Must a human decide? | approval policy (E0.1) |
| May it happen right now? | the execution gates and the node's allow list |

The Registry answers the first only, and confers nothing.

**A17.2 — The node is the only authoritative source.** A device's
capability is declared by the agent that would execute the action, from
its protocol's own implementation reach and its own configured allow
list. The Site Manager stores that declaration, Central Command caches
and composes it, the Console and the Operational Agent read it. **No
layer above the node may declare a capability**, and no surface may
carry a capability contract of its own.

**A17.3 — Reach and policy are reported separately.** Three sets travel
together: what the protocol implements, what the node permits, and their
intersection. "There is no code for it" and "this node does not permit
it" are different problems with different fixes, and collapsing them
into one list would leave an operator unable to tell which they have.

**A17.4 — Unknown is a real answer and is never zero.** A device that
has not declared reads `unknown` — never capable, never incapable. No
migration backfills an empty declaration, and unknown reach never
refuses a binding or a proposal; only provable zero reach does. Without
this rule a fleet that upgraded Central Command before its agents would
lose every bound action class at once.

**A17.5 — `reversibility` joins `ACTION_RISK`.** One new platform-level
declaration, on the action class, beside risk: `none` /
`self_reverting` / `reversible` (naming the inverse class) /
`irreversible`. It is a genuinely different axis — `SEL_CLEAR` is risk
`low` and permanently destroys the event log — and in this amendment it
is **reported only**: no grant, approval requirement or execution gate
reads it.

**A17.6 — An unimplemented class keeps every governed semantic.**
`INTERFACE_RESET` and `CLEAR_COUNTERS` retain their risk level,
preconditions, blast-radius and verification semantics and stay in the
`ActionType` vocabulary. Deleting a governed class to make the Registry
pass is explicitly refused: this is a capability truth problem, not a
reason to drop governance. Implementing either is a separate governed
capability slice with its own transport, safety, validation and
live-proof boundary.

**A17.7 — Consumers refuse on CAPABILITY, never on POLICY.** An
Operational Agent may not be bound to a class no executor implements,
nor to one no device in its own scope has the code for, and may not
propose a class its target device's protocol cannot perform. Refusals
name the capability reason, so an operator is sent to the right fix.

A node's `allow_list` is **not** such a ground. It is operator policy,
changeable at any time, and §A17.1 already assigns "may it happen right
now" to the node, which enforces it as the final execution authority. A
class the nodes implement but do not currently permit therefore **binds
and proposes normally**, and the node's refusal becomes attributed
evidence in the error budget — which is the ratified A0+A1 behaviour and
the mechanism by which an operator discovers the policy is wrong.
Refusing it at Central Command would promote a mutable node setting into
a hard configuration constraint and make it impossible to configure an
agent ahead of a config rollout. The state "bound, capable, permitted
nowhere" is instead REPORTED, by name, on the agent view.

**A17.8 — Deferred, named, not abandoned.**

- **Capability execution gate.** `execution_permitted()` reserves
  `capability` as its sixth decision input and nothing supplies it. This
  amendment deliberately does **not** refactor the production execution
  chain to fill it; `Agent._authorize_execution` and the node allow list
  are untouched. A later slice connects Registry truth into the runtime
  authorization path **without creating a second execution engine or
  authorization model**, and must land before any capability-dependent
  autonomous expansion relies on a runtime capability gate.
- **Skill recommendation validation.** `ActionRecommendation` is a fifth
  capability declaration site: a skill may recommend any action string.
  A later Registry-consumer slice validates skill-recommended actions
  against executor reach.

### A18 — 2026-08-31 — S6: governed capability orchestration across an estate (decided: Vinod)

**Trigger.** Every governance mechanism the platform has — org tree,
scoped RBAC, Capability Registry, autonomy contract, approval ledger,
directive transport, outcome and learning paths — existed, and nothing
could express the enterprise intent they were built for: *"run this
capability across every site in Region West, but only where the executor
actually supports it, respecting autonomy and approval."* Campaigns
existed only at the Site Manager, single-site, firmware-specific, gated
by a site token, with their own private approval field.

**A18.1 — A campaign is generic capability orchestration.** One governed
`ActionType`, one scoped estate, all fourteen classes through the same
machinery. Explicitly **not** firmware campaigns moved to Central
Command: risk, autonomy, approval and execution differentiation all come
from the existing contracts, never from a special case.

**A18.2 — The tier split is fixed.** Central Command owns campaign
lifecycle, tenant/org/site targeting, RBAC and scope, capability
preflight, governance, approval workflow, site ordering and concurrency,
and campaign state. The Site Manager owns the site execution boundary,
fault-domain knowledge, device-wave planning and execution. **Central
Command must never invent or approximate fault-domain or blast-radius
information.**

**A18.3 — The planning contract is read-only.** `PlanCampaignWaves`
(CC → SM) returns exact device membership per wave, a domain **count**,
and a deterministic plan hash. It writes nothing, dispatches nothing and
authorizes nothing. Fault-domain identities never leave the Site
Manager, and `plan_waves()` is never duplicated into Central Command:
Central Command reflecting the site's topology would make it a second
representation of something only that tier owns.

**A18.4 — Approval is per site-wave, universally.** Every action
requiring a human is approved per site-wave on the existing
`/api/approvals` surface, under `action.approve`, recorded in
`cc_approval_records` with `subject_type = campaign_wave`. There is no
campaign-level approval model and no campaign-specific approver storage.
Batch review is a Console affordance; the records stay individually
attributable and auditable. An autonomous class raises no approval
subject at all. All site-wave subjects for a campaign version are raised
at submit, so the set of decisions is deterministic before execution.

**A18.5 — Approval binds to a plan.** The subject is a digest over
campaign, version, site, wave index, the wave's **exact device set** and
the plan hash. Plans are immutable; a changed plan is a new row and the
old is superseded. Binding is therefore structural: a stale approval
cannot address a new subject even if nobody remembers to check.

**A18.6 — APPROVED ≠ EXECUTABLE ≠ EXECUTED.** Approval authorizes; it
never guarantees execution. Immediately before each site-wave dispatch,
capability and policy are revalidated and the plan is re-requested:

- a changed plan **refuses** the wave and requires new approval;
- capability/policy may only **narrow** the approved set;
- a newly capable device is **never added** after approval;
- changed fault-domain membership can never silently widen a blast radius.

**A18.7 — Warned targets need a named human.** `effective = false`
continues to mean implemented-but-not-currently-permitted. Such targets
are shown in preflight, never silently excluded, and a named person must
exclude or acknowledge them before approval. The acknowledgement is
version-bound and audited; editing a campaign invalidates it.

**A18.8 — Sites are isolated; partial success is first-class.** Within a
site, halt-on-first-failure stands. Across sites nothing propagates: a
halted site is not a halted campaign. A halted site **voids** its own
later approved-but-unstarted waves, explicitly and audited, because
their predecessor assumption has failed and stale authorization must
never be reused. Resuming is an explicit operation: re-plan, new plan
version where material, new approval where required.

**A18.9 — One execution path, and idempotence.** Dispatch is
`DispatchAction` onto the existing directive transport and the unchanged
node funnel. The Central Command reconciliation loop decides only which
wave is next; it is not an execution engine. A restart, a duplicate
trigger or a repeated `POST /advance` cannot double-execute, because the
dispatch ledger keys on campaign, version, site, device, wave **and
plan hash**.

**A18.10 — Nothing new was introduced.** No new permission, no second
approval model, no second authorization model, no second execution
engine, no campaign capability catalogue, no second wave-planning
algorithm. `execution_permitted()` and `Agent._authorize_execution`
remain untouched (A17.8 stands). The Site Manager's `firmware_campaigns`
are untouched; superseding them is a later decision.

### A19 — 2026-08-31 — A2: the Operational Agent becomes a complete governed product (decided: Vinod)

A0+A1 made the Operational Agent an object: identity, scope rows,
capability bindings, a policy that can only tighten the tenant's own, and
labelled proposals into the one approval queue. It could be created and
switched on. It could not be **configured, examined before it acted,
budgeted, or explained afterwards** — and its skill binding was accepted
and inert (E0.3 refused the kind rather than leave it so).

A2 closes that gap. It introduces **no new permission, no second approval
model, no second budget system, no second capability model and no second
execution path**. Every judgement below is a composition over governance
that already exists.

**A19.1 — Activation is a governed transition, not a status write.** The
lifecycle is fixed: CREATE → CONFIGURE → PREFLIGHT → ACKNOWLEDGE →
APPROVAL (where required) → ACTIVATE → RUN → OBSERVE → OUTCOME →
LEARNING. Activation without a stored preflight for the exact
configuration version is refused. The two ad-hoc checks A0 performed at
the transition become dimensions of that one contract, so the Console and
the gate cannot disagree.

**A19.2 — The activation preflight is a contract, not a checklist.**
Twelve dimensions — identity, tenant, scope, capabilities, skills,
autonomy ceiling, approval policy, budget, safety, executor reach,
configuration version, activation state — each carrying one of four
verdicts: READY, BLOCKED, WARN, UNKNOWN. BLOCKED dominates and refuses
activation. WARN and UNKNOWN require a named human's acknowledgement,
version-bound, exactly as A18.7 requires for a warned campaign target.
**UNKNOWN is first-class** (A17.4): a fleet mid-upgrade is unknown, not
incapable, and the two are never conflated. The result is assembled
server-side and stored immutably; a re-run supersedes, never updates.

**A19.3 — A READY preflight confers nothing.** It is a statement about
configuration. Every proposal the activated agent makes still passes the
S5 autonomy contract, the E0.1 approval ledger, the Site Manager's lease,
preconditions and blast radius, and the node's own allow list. Activation
grants no RBAC, no scope and no capability authority: it approves a
configuration the actor was **already permitted to build**.

**A19.4 — D1: activation approval is DERIVED, never ceremonial.**
Approval is required if and only if activation would confer real
unattended execution — that is, the agent's ceiling is above zero, it does
not require a human for every action, and at least one bound class is
`autonomous` under the tenant's own contract. An observe-, suggest- or
propose-only agent grants no new authority by being switched on and is
activated without a separate approval. Configuration saves are never
gated.

**A19.5 — Activation approval rides the one ledger, under the one
completion rule.** `SUBJECT_AGENT_ACTIVATION` is a fourth origin on the
E0.1 ledger — not a fourth approval model. Policy resolution, required
approver count, group membership, duplicate prevention, the terminality
of a denial (D16) and the completion rule are **the same functions a node
action calls**. A tenant configuring `required_approvers = 2` gets two
approvers for an activation, and one valid approval leaves the activation
**pending**. Any second implementation of that judgement is a defect by
definition, whatever it computes.

**A19.6 — The approval subject binds to the configuration.**
`activation_subject_ref` is a digest over the agent id, its configuration
version and the exact set of classes activation would let run unattended.
An edit that changes any of the three yields a different subject, so an
approval structurally cannot survive the configuration it was not given
for. Approving activation does not activate: a person still activates,
and the gate re-checks the preflight then.

**A19.7 — D2: the budget counts EXECUTIONS, and caps only unattended
work.** The per-agent budget counts actions actually executed under the
agent's attribution key, drawn from the existing outcome accounting — not
proposals, because intent is not consumption. Exhaustion means "this
agent has spent its delegated unattended allowance", never "this agent is
disabled". When exhausted, unattended execution is **refused at the
production dispatch path**; observation, analysis, proposal generation and
human-approved execution all continue unaffected. A human-approved
proposal is never refused for want of unattended budget. The tenant and
site budgets (S5, A10.4) are unchanged and still apply; this is a
narrowing, never a grant.

**A19.8 — D3: an approved proposal keeps its version and is not a
guaranteed execution.** A proposal authorized against configuration V3
remains attributable to V3 and is never silently reinterpreted as V4.
Equally, it is never silently executed because somebody once approved it:
Central Command re-evaluates its own hard gates at dispatch — agent
identity, activation state, tenant scope, stop switch, agent pause — and
an unevaluated gate is a refusal, not a pass. These are Central Command's
gates only; the Site Manager's lease, preconditions and blast radius, and
the node's allow list, run afterwards and independently and are never
substituted for. **Approved proposal version ≠ guaranteed execution.**

**A19.9 — Post-activation configuration versioning.** Activation records
the configuration version actually switched on, atomically with the status
change. `active AND activated_version == version` is the definition of no
drift; an agent freshly activated at V1 reports no drift. Editing an
active agent increments the version, which makes the running configuration
observably stale and invalidates the stored preflight, the acknowledgement
and any activation approval — each of which is version-bound.

**A19.10 — Skills are governed COMPOSITIONS, never permissions.** A skill
composes capabilities the agent already holds. Binding one may not expand
permission, scope, capability authority, autonomy ceiling or approval
authority. The one thing a skill can do is recommend an action, and that
is validated against the Capability Registry at preflight: a skill
recommending a class the platform does not implement is unusable and is
reported as such before activation, never discovered at dispatch. There is
no skill-specific capability model and there must never be one.

**A19.11 — Skill installation is per DEVICE, scoped and idempotent.**
Installation is triggered by activation and targets only devices within
the agent's own resolved scope that can actually run what the skill
recommends; a device that cannot is skipped **with a reason**, never
silently omitted. An undeclared device receives it, because unknown is not
incapable (A17.4) and the node's allow list remains final. Delivery is the
existing `InstallSkill` RPC onto the R5-1 directive transport, now
carrying explicit device targeting — installing onto a whole site from a
rack-scoped agent would be a scope escape dressed as a convenience. A
durable per-(agent, version, skill, device) ledger makes re-activation
non-duplicating, and the installation is audited.

**A19.12 — Runtime health is reported honestly or not at all.** The
runtime view reports only signals the platform actually produces:
activation state, configuration version and drift, last evaluation, device
freshness split into recently-seen / stale / **never-reported**, budget
consumption, proposal volume, skill installation state and preflight
currency. A device the site has never reported is counted as neither
healthy nor unhealthy. Inventing a plausible value is worse than admitting
ignorance, because an operator acts on this.

**A19.13 — Read/write split, unchanged vocabulary.** Preflight,
acknowledge and the lifecycle transitions are `site.manage` and
object-gated through the E1.2 delegation ceiling; the preflight and
runtime reads are `fleet.view` and scope-filtered, so an out-of-scope
agent is 404 and never 403. No permission is invented, and every route is
declared in the executable route contract. Activation approval is decided
on `/api/approvals` under `action.approve`, because there is one approval
system.

**A19.14 — What A2 did not do.** `execution_permitted()` and
`Agent._authorize_execution` are untouched; the node remains the final
execution authority (A17.8, D2 of A16's lineage). The Site Manager grows
no second authorization model. `INTERFACE_RESET` and `CLEAR_COUNTERS`
remain governed vocabulary with zero executor reach and are not deleted
(A17.6).

### A20 — 2026-08-31 — A3: machine identity is authentication, and only authentication (decided: Vinod)

An Operational Agent has never held a credential. It is a row evaluated by
a Central Command-resident loop that calls the governance composers
in-process, and its "identity" is an attribution string. A3 gives it a
durable machine identity so it can authenticate to HarkenIQ — and does
that **without introducing a second authorization or execution model**.

The credential answers exactly one question: *who is this runtime?* It is
a narrow authentication boundary, and it is not the product's long-term
ceiling on what an agent may do.

**A20.1 — One Keycloak client-credentials service account per LOGICAL
Operational Agent**, in the tenant's own realm, bound 1:1 to a
`cc_operational_agents.id`. Keycloak is reused, not replaced: no second
identity provider, no token service, no bespoke agent authentication
scheme. The `api_keys` table — a complete credential lifecycle whose
verifier `get_by_hash` has no production caller and which therefore
authenticates nothing — is retired rather than adopted, because adopting
it would mean building the second token service this amendment forbids.

**A20.2 — A machine identity confers NOTHING.** It grants no permission,
no scope, no capability authority, no autonomy, no approval authority and
no execution authority. Those come from where they already come from: the
fixed permission vocabulary, `cc_scope_grants`, A0 capability bindings,
the S5 autonomy contract, the E0.1 approval ledger, and the node's own
funnel.

**A20.3 — The machine principal ceiling is a HARD, INDEPENDENT
CONSTANT.**

    effective = A0 agent read bindings  ∩  MACHINE_PRINCIPAL_CEILING
    MACHINE_PRINCIPAL_CEILING = { fleet.view, incident.view }

The ceiling is deliberately *not* "whatever today's A0 bindings imply". It
is its own constant and the effective set is the INTERSECTION, so no
future binding — however written or mapped — can widen machine-principal
authority. This is E1.4's rule applied to a second subject: a custom role
bundle could once OR its permissions into a role and widen it; bundles now
intersect, and so does this. A machine principal must be **structurally
unable** to hold `action.approve`, `site.manage`, `role.manage`,
`tenant.manage`, `audit.export`, or any other mutation or administrative
permission — asserted over the whole vocabulary, not by convention.

The reason this matters: resolved as agents are today
(`role_permissions=["*"]`, safe only because nothing authenticates), an
authenticated agent would satisfy every route guard in the platform,
including approving its own proposals.

**A20.4 — One governance model, end to end.** Authentication → the
existing `UserContext` → the existing RBAC resolver → the existing scope
resolver → the existing Capability Registry, autonomy contract and
approval ledger → the existing execution funnel → the node. No second
RBAC, scope resolver, capability model, approval system, execution
engine, identity provider or token service. An agent still causes
operational work only by proposing: observe → reason → propose →
approval → execution → the node executes. HTTP authentication grants no
direct mutation authority.

**A20.5 — Lifecycle: CREATE → ISSUE → BIND → ROTATE → REVOKE → RETIRE.**
Client secrets are **never stored at Central Command** — Keycloak holds
them and CC shows one exactly once, the discipline E1.3's enrollment
tokens already use. **CC's identity status is authoritative on every
request**, so revocation beats an otherwise-valid JWT immediately rather
than waiting out a token lifetime. Rotation regenerates the secret with
no execution gap and never yields two identities: one client, one
subject, one row, one secret at a time.

**A20.6 — One identity per logical agent.** Per-runtime-instance
identities are not invented: the repository has no runtime instance
concept, and two runtimes of one agent share one bundle, one scope and one
budget, so they share one identity. Instances are made *observable*
(`last_seen_at`, `last_seen_source`) without being separately
*authorized*. Distinct per-instance authorization is a future ratified
decision only if the product later requires it.

**A20.7 — Identity binds to the AGENT, not to a configuration version.**
Editing configuration does not require re-credentialing. A2's version
semantics are untouched: an edit still bumps the version and invalidates
the preflight, the acknowledgement and any activation approval (A19.9). A
paused agent keeps a valid identity and keeps observing; a **retired**
agent has its identity revoked.

**A20.8 — D3 survives, unchanged.** An in-flight proposal retains the
configuration version it was made under. The current hard security and
safety gates remain authoritative at dispatch, and a revoked identity
refuses the proposal and audits the refusal — through the `agent_identity`
slot that already exists in the dispatch gate, not a new mechanism.
**Approved proposal version ≠ guaranteed execution.**

**A20.9 — Platform Operations sees aggregates, and A12.1 is not
amended.** Platform and vendor staff receive **no live tenant-plane
identity access**. A3 may expose aggregate operational signals only —
identity count, active/revoked/retired counts, high-level health and
freshness — and never per-agent identity detail through the tenant plane.
Those aggregates travel the **existing internal CC→Console channel** but
on a **distinct operational endpoint**, never the usage-events payload,
because that payload feeds metering and mixing a non-billing signal into
a billing ingest would corrupt invoicing. Full Platform Support, governed
support workflows and customer-authorized break-glass remain a separate
future Platform Operations capability built on this same governance model.
**Platform Support is never solved by weakening A12.1.**

**A20.10 — No proposal write path in A3.** `POST /proposals` is not
added: the Central Command-resident agent does not need it, and external
proposal submission belongs with its first real consumer at MCP/A5 unless
new evidence proves an earlier dependency.

**A20.11 — The ceiling is not the product's ambition.** A20.3 bounds the
CREDENTIAL, not the Operational Agent. A4, A5, A6 and MCP may make an
agent substantially more capable — through the governed Capability
Registry, RBAC, scope, autonomy, approval and execution architecture that
already exists. What stays narrow is authentication.

### A21 — 2026-09-01 — A4: governed capability expansion is about ADDRESSABILITY, not autonomy (decided: Vinod)

The platform implements 12 of its 14 action types. An Operational Agent
could propose 6, and one of those six had no executor at all. Seven
implemented, governed, node-executable capabilities were invisible to
every agent — not forbidden, not fenced, not denied; **unreachable**,
because nothing mapped a condition to them.

A4 makes implemented capabilities addressable by a governed agent and
makes Capability Registry truth load-bearing at runtime. It adds no
permission, no authority and no autonomy.

**A21.1 — The condition→capability mapping becomes governed data.**
`REMEDIATION_CANDIDATES` was a module constant: an agent could propose
only what a hardcoded dict named, and an operator could neither see it
nor change it. It becomes `cc_capability_catalogue` — tenant-scoped,
readable, auditable, seeded so that no tenant's behaviour changes on
upgrade. Each entry keeps `subsystem`, `action_type`, `because`,
`provenance` and `enabled`.

**A21.2 — The catalogue is not a second capability-authority model.**
It answers one question: *which capability is a candidate for which
observed condition.* The Capability Registry remains the only authority
on whether an executor can perform an action, and the catalogue can
never contradict it: an entry naming a class no executor implements is
refused on write and inert on read. Being in the catalogue is not being
permitted, in scope, autonomous, approved, or executable.

**A21.3 — Capability ≠ authority, and A4 collapses nothing.** Capability
asks whether the executor can perform the action; permission whether the
actor may address it; scope where; autonomy whether it may proceed
unattended; approval whether a human must decide; execution whether this
concrete action may happen now. Six questions, six answers, unchanged.

**A21.4 — The interface subsystem was dead and is repaired.** It mapped
only to `CLEAR_COUNTERS`, which no executor implements — and A17's
zero-reach rule then refused the binding. A switch-scoped agent could
observe an interface incident and never act on it, though R6 shipped
`INTERFACE_ENABLE` and `INTERFACE_DISABLE` on gNMI. The catalogue maps
the subsystem to those implemented actions instead. This is a mapping
correction, not a new node capability.

**A21.5 — Newly addressable classes enter as `not_budget_mapped`, which
means A NAMED HUMAN IS REQUIRED.** A4 does not modify the autonomy
ladder. No class is mapped into it, `COLLECT_DIAGNOSTICS` and
`IDENTIFY_LED` included. Each newly addressable class keeps its existing
risk, budget mapping, approval requirement, executor availability and
safety semantics.

**Evidence of effectiveness is not authority to execute unattended.**
`COLLECT_DIAGNOSTICS` at 8/8 SUCCESS is an argument for a future
decision, not a reason to widen a boundary inside a slice about
addressability. Any autonomy promotion is a separate explicit product
decision and its own amendment. This is deliberate sequencing: **A4 asks
what a governed agent may address; S5 and its successors ask what it may
execute unattended.**

**A21.6 — `execution_permitted()` becomes a real production gate.**
E1.3 shipped a ten-input fail-closed execution model and A17.8 recorded
that its `capability` slot was unsupplied. The broader truth this slice
found: the function had **no production caller at all** — the runtime
used hand-written sequential checks at the Site Manager alongside it.
A4 routes the Site Manager's existing dispatch checks *through* that one
function and supplies `capability` from the Registry. This is
consolidation of two parallel statements of one model, **not a second
execution engine**: no check is added, none is removed, and an
unevaluated input still refuses.

**A21.7 — The node remains the final execution authority.** The node
funnel is untouched: allow list, preconditions, stop switch, lease and
blast radius are unchanged, and the node's refusal remains final and
still becomes attributed evidence.

**A21.8 — Skills gain one rule and no authority.** A skill may recommend
only actions the tenant's catalogue names AND the Registry validates. It
still cannot expand permission, scope, capability authority, autonomy or
approval authority (A19.10, unchanged).

**A21.9 — `CLEAR_COUNTERS` and `INTERFACE_RESET` stay unimplemented, and
stay honest.** A4 changes their VISIBILITY, not their status. An
operator can see that the class exists in the governed vocabulary, that
no executor implements it, that it therefore cannot execute, and what
would have to change. Faking the capability and deleting the class are
both refused (A17.6).

**A21.10 — Firmware classes are campaign work, not incident
remediation.** `FIRMWARE_UPDATE` and `FIRMWARE_ROLLBACK` are implemented
and remain reachable only through S6 campaigns. They are deliberately
absent from the condition catalogue: an agent does not propose a
firmware update in response to a fault, and inventing a condition for
them would be inventing a remediation model nobody asked for.

**A21.11 — Nothing new was introduced.** No new permission (catalogue
reads are `fleet.view`, writes `site.manage`, both E1.2-scoped), no
second RBAC, scope resolver, approval system, execution engine,
capability-authority model or identity path. No MCP. No natural-language
builder. UNKNOWN remains neither capable nor incapable; unimplemented
remains never executable; policy-disabled remains distinct from
unimplemented.

### A22 — 2026-09-01 — A5: the canonical governed agent interaction contract (decided: Vinod)

**Context.** A0–A1 made the Operational Agent an object, A2 made it a
product, A3 gave it an identity and A4 made seven stranded capabilities
addressable. What no slice has produced is the *contract* through which a
governed agent interacts with those capabilities: what a capability
actually requires in order to run, and how anyone — an operator, the
agent itself, or a later external consumer — asks the platform what the
agent *would* do without anything happening. A5 establishes that
contract. It is deliberately neither MCP nor externalization; both are
later slices that adapt to what A5 defines.

**A22.1 — A5 is CC-resident, and that is a decision, not a limitation.**
The evaluator keeps its in-process residency because it is the only
caller that can obtain the complete consistent decision state — twelve
inputs, of which `open_dedupe_keys` is structurally unavailable to an
HTTP caller. An external decider built today would re-propose
permanently-refused work on every pass. A5 therefore *enables*
externalization by defining the canonical semantics; it does not perform
it. A6 may later expose ingress; MCP later becomes a thin protocol
adapter over the proven contract. Neither may own RBAC, scope, the
capability registry, autonomy, approval or execution authority.

**A22.2 — The action-parameter contract is ONE platform declaration.**
Every governed ActionType declares what it requires to execute: parameter
names, types, whether each is required or optional, constraints and
defaults where they exist, and where a truthful value may come from. The
declaration lives beside `ACTION_RISK` and `ACTION_REVERSIBILITY` in the
shared module, so Central Command, the Console, skills, Operational
Agents, the node and any future MCP consumer derive from the same file.
Independent parameter schemas in CC, Console, Skills, Node or MCP are
refused. A skill's own `action.params` block becomes a CONSUMER validated
against this declaration, not a peer of it — it was a fifth undeclared
schema site and is now bound to the platform's answer.

**A22.3 — Parameters are validated before a proposal exists.** The
evaluator may not emit a generic `params={"reason": ...}` for every
class. A proposal whose parameters do not satisfy the declaration is not
created. This is the fix for the defect A4 introduced: `IDENTIFY_LED`,
`CONFIG_RESTORE`, `POWER_CAP_ADJUST`, `INTERFACE_ENABLE` and
`INTERFACE_DISABLE` were made addressable while the evaluator could
supply none of their required parameters, so each would be proposed,
approved by a human, dispatched, and refused at the node every time.
Governance held — the node refused and the refusal became attributed
evidence — but the platform was making promises it could not keep.

**A22.4 — Component identity travels, and unknown stays unknown.** A
verdict's `sensor_id` is `"<subsystem>:<component>"`; the Site Manager
has always parsed off the subsystem and discarded the remainder, so
Central Command held no component identity for any device — no drive
bay, no port name. The affected components now ride the fleet snapshot on
an additive, nullable field, computed from the Site Manager's own verdict
stream. There is NO backfill: an absent field means the Site Manager has
not reported, which is unknown, never "no components". A class whose
required parameter cannot be resolved from reported evidence is not
proposed, and the reason is reported by name. The platform never guesses
a component.

**A22.5 — Addressable is not executable.** A capability that is in the
tenant's catalogue, implemented by an executor and permitted by policy,
but whose parameter contract cannot be satisfied for a given device, is
reported as exactly that. It is not hidden, not silently dropped, and not
presented as executable. `POWER_CAP_ADJUST` and `CONFIG_RESTORE` remain
in this state after A5 and are truthfully described: no policy input
exists for a target wattage, and drift detail is agent-side. Naming the
missing input is the deliverable; inventing one is refused.

**A22.6 — One governed verdict function.** The per-proposal decision is
lifted out of the evaluator's loop into a single named function that
takes governed context and returns a verdict. Its first and only
production consumer in A5 is the CC-resident evaluator. Dry-run and
normal evaluation call the same function — a preview that reasoned
differently from the runtime would be worse than no preview.

**A22.7 — Dry-run is a first-class contract, not a debug endpoint.** On
demand, scoped to one agent, it returns what the agent WOULD propose
against current governed context, machine-readable, with the same
dispositions, blocking conditions, evidence and parameters a real pass
would produce. It writes nothing, dispatches nothing, creates no
execution state, consumes no budget, and bypasses no scope, RBAC,
capability, autonomy or approval rule. "Writes nothing" is proven by
table snapshot, not asserted.

**A22.8 — Dry-run authority: humans and the agent's own identity.** A
governed Operational Agent may invoke its own dry-run. This requires NO
change to A20's `MACHINE_PRINCIPAL_CEILING`: the route is guarded at
`fleet.view`, which the ceiling already carries, and reasoning about what
one would propose is a read. The agent's restriction is an object-level
gate — an identity may dry-run its own agent and no other — and its
results are bounded by its own tenant, scope and identity exactly as a
real pass is. Dry-run is not execution authority and confers none.

**A22.9 — `/api/attention` is scoped.** It is declared READ_SCOPED and
applied no scope filter, so a site-scoped principal read every site's
attention state. It is also the one read every Operational Agent is
required to hold. E1.2's resolver now filters it like every other
site-anchored read.

**A22.10 — No grant means no operational scope, for every principal.**
The synthesized tenant-wide grant a grantless principal receives under
`legacy_open` inverts A0's own rule ("no scope rows = no devices") at the
A3 seam. The final invariant is unconditional and applies to humans and
agents alike: no grant → no operational scope → no operational data → no
proposal target. Because `legacy_open` is the default posture and
existing tenants may hold no grant rows, enforcement is staged: Central
Command first REPORTS which principals would lose access, with enough
detail to remediate, and enforcement follows in a later slice once
tenants can act on it. This is a migration strategy and explicitly not a
weaker final security model; the reporting surface is a deliverable, not
a log line.

**A22.11 — One attention composer.** The HTTP read and the in-process
composer were near-verbatim duplicates whose `band` filter reordered
`rank`, so an agent and an operator could see different priorities for
identical state. There is one composer; `band` is a pure filter applied
after ranking, which is what the endpoint's own contract already claimed.

**A22.12 — Dispatch re-checks current lifecycle and identity.**
Autonomous dispatch bypassed the CC-side gates, so a paused, retired or
revoked agent still dispatched. A19's D3 semantics are unchanged and now
enforced on both paths: an approved proposal retains its version and is
never a guarantee of execution. Current identity, lifecycle, tenant,
scope and hard safety gates remain authoritative at dispatch, and the
node remains the final execution authority.

**A22.13 — Effective permission is per grant and can never be `*`.** The
agent scope loader resolved with `role_permissions=["*"]`, so the
resulting scope answered `permits("action.approve")` with True. It was
latent only because every call site read `.site_ids`. The wildcard is
removed at the source rather than patched at call sites: a scope resolved
for scope-expansion purposes carries no permissions and cannot be asked a
permission question. The established model — route authorization →
repository scope filter → object-level mutation gate — is preserved, and
no route decorator is added.

**A22.14 — Discovery, decision and execution stay three questions.**
*What capabilities exist, what do they require, is the executor
implemented* is discovery. *What would this agent propose right now* is
decision. *May this concrete proposal execute now* is execution.
Capability discovery is never execution permission, and no A5 surface may
collapse them. The canonical path is unchanged: Operational Agent →
machine identity → RBAC/scope → capability catalogue → Capability
Registry → current context → A5 interaction contract → autonomy/approval
→ existing proposal/action path → existing execution funnel → Site
Manager → node.

**A22.15 — What A5 does not build.** No MCP. No external proposal
ingress. No `POST /proposals`. No `/api/v1`, which is not a ratified
deliverable. No external runtime or per-instance identity. No new
permission. No autonomy change: no action class is mapped into the
autonomy ladder by A5, and A21's rule stands — evidence of effectiveness
is not authority to execute unattended. No Site Manager surface is
exposed to any A5 consumer, directly or by proxy.

### A23 — 2026-09-02 — D2/A23: enterprise authorization integrity (decided: Vinod)

**Context.** An independent repository verification, reconstructed and
re-executed on `main` at `f8a340e`, found that authorization integrity has
two failure classes, not one. **(A) Synthesis escalation:** the
`legacy_open` fallback in the scope resolver fires on an EMPTY grant list
after lifecycle filtering, so a principal whose only grant has expired,
been revoked, or points at a deleted org unit resolves tenant-wide with
their full role permissions. Proven by executing `resolve()`: a
site-narrowed principal becomes a tenant-wide holder of `action.approve`
and `role.manage` the moment their grant expires. **(B) Declared but not
enforced scope:** the route contract lives in a test file, no runtime code
consumes a treatment, the generated persona sweep asserts only 403 versus
not-403, and several handlers inject the caller's scope and never read it.
Strict mode cannot help a handler that never consumes the scope. Two
further findings: the campaign preflight UNIONS the caller's reach into the
campaign's target set (a one-site campaign preflighted by a tenant owner
targets the whole estate, proven by execution), and Central Command boots
against the PLATFORM realm when `keycloak_realm` is unset in secure mode.
Ratified decisions D1–D4 and the three follow-on decisions below are
recorded here before any code.

**A23.1 — Sequencing is B → identity → C → B′ → A, as five slices.**
A23-1 enforcement, A23-2 identity, A23-3 recovery + delegation, A23-4
synthesis, A23-5 strict birth. Each is one PR from verified main, merged
and main-verified before the next starts. Synthesis is not removed first,
and strict is not made the default, until enforcement, identity and
recovery are proven. Modification to the ratified D1: campaign
intersection and realm refusal are ENFORCEMENT and land in A23-1; the
identity slice precedes B′ because the migration census B′ depends on
compares email-recorded actors to subject-keyed grants and is wrong today.

**A23.2 — A declared scope treatment is enforced at runtime, and proven
by narrowing.** The route contract (`READ_SCOPED`, `OBJECT_GATED`,
`TENANT_GATED`, `UNSCOPED`) becomes a runtime module. Three things must
hold together for every route: the declared treatment, runtime
consumption of the resolved scope by the handler, and a behavioural test.
The test harness detects a handler that declares scope and never consumes
it, and the persona matrix asserts actual row narrowing (a scoped
principal never receives an out-of-scope site, device, incident, campaign,
agent, grant or proposal identifier) and actual mutation protection (a
scoped principal cannot reach an out-of-scope target with any mutation).
A read is never 403 on an out-of-scope object; it is absent.

**A23.3 — The campaign target invariant.** Actual target set = declared
campaign target ∩ caller effective scope. Caller authority may constrain
and may never enlarge. The persisted target set must equal the governed
set that will execute. The campaign preflight no longer resolves the
caller a second time with `role_permissions=["*"]` and `realm=""`; that
shape was removed for agents by A22.13 and is removed for humans here.
Campaign lifecycle mutations (acknowledge, submit, cancel, advance) are
object-gated on every scope rule the campaign names, the same ceiling
creation and preflight already apply.

**A23.4 — Secure mode requires an explicit tenant realm.** With
`insecure=false` and `keycloak_realm` unset, configuration validation
fails and Central Command does not boot. The silent fallback to the
platform realm is deleted. `platform_super_admin` is not removed by A23.

**A23.5 — Metering is scope-free by design.** The usage reporter, the
fleet poller, the evaluator, the campaign runner and the governance
loaders' defaults call repositories with `scope=None`, meaning "no user is
asking". That contract is pinned by a test before general scope hardening
and is not an omission to be fixed: a strict tenant with zero grants still
reports its full node count, and no user-authorization filter may ever
touch the billing path.

**A23.6 — Self-grant is refused outright.** Grantor principal == target
principal refuses the grant with an explicit reason and audits the
refusal, tenant-wide grantors included. Delegation is a transfer of
bounded authority, never a way for a principal to modify its own.
Delegated authority is bounded by the grantor's effective permissions AND
the grantor's effective reachable scope, checked per grant on the exact
target: a narrowed administrator cannot grant themselves withheld
permissions, broaden their own subset, or broaden their own scope. (A23-3.)

**A23.7 — Actor identity is `actor_ref`, outside the hash chain.**
`cc_audit_log.actor_ref` (CC migration 0020): nullable for historical
rows, indexed, the canonical stable `principal_ref` for every new write,
DELIBERATELY outside `_chain_payload` (the `site_id` precedent from
E1.2). `actor` is retained for compatibility and display; `detail` may
carry a mutable display snapshot. One helper, `actor_of(user)`, is the
only way a new audit row names its actor. No historical row is backfilled
and no chained payload is rewritten. Readers understand `actor_ref` when
present and legacy `actor` forms when it is NULL; the enforcement-impact
census uses `actor_ref` where available. Canonical model: `principal_ref`
is stable identity; email and display name are mutable snapshots. (A23-2.)

**A23.8 — The last tenant `role.manage` authority cannot be configured
away.** Revoking, setting an expiry on, or transitioning to strict past
the last active tenant-scope grant carrying `role.manage` is refused with
an explicit reason and audited, through ONE counting function shared by
all three checks. Recorded limit: Keycloak-side user deactivation is
outside Central Command's visibility; this protects grant configuration,
not the identity provider's lifecycle. No platform-plane bypass is
created; A12.1 stands. (A23-3.)

**A23.9 — A vanished target never widens authority.** An org unit cannot
be deleted while active grants reference it; grants are removed or
reassigned first. A grant whose target no longer exists is retained as
INERT: target missing, reach none, explicit reason, and evidence that the
principal was previously administered. An inert grant never produces an
empty grant list, so it can never trigger synthesis. A missing site
resolves to zero operational reach with a reason and no data. (A23-3.)

**A23.10 — Synthesis only for the never-granted, and never for agents.**
The resolver distinguishes NEVER GRANTED from PREVIOUSLY GRANTED BUT NOW
revoked, expired, orphaned or vanished. `legacy_open` synthesis is
allowed only for the former. Any previously administered principal with no
valid grant has zero operational reach. Operational Agents receive no
synthesis under any posture (A0: no scope rows = no devices). The final
invariant is unconditional: no grant → no operational scope. (A23-4.)

**A23.11 — Strict birth.** A23-5 first pins every existing tenant's
current posture explicitly by migration, then changes the default: a
missing `cc_tenant_settings` row means STRICT. Secure defaults never
depend on a Console provisioning signal. `missing row → legacy_open` is
retired as a platform security invariant, and the compose gate proves a
new tenant is strict, a pinned legacy tenant stays pinned, and
`legacy_open` cannot be synthesized by a missing row. (A23-5.)

**A23.14 — Strict birth: the two ratified implementation decisions**
(dated 2026-09-03, decided: Vinod). A23.11 recorded the dependency; it
did not say which tenant a Central Command migration is entitled to
speak for, nor what happens when a tenant is born without an
administrator. Both are settled here, and neither widens A23.

*Tenant identity at migration.* Central Command is single-tenant
software (doc 01 §7): every request resolves `config.tenant_id`, and CC
holds no tenant table — the authoritative tenant registry is the
Console's `tenants`, in another service and another database, which a CC
migration cannot and must not read. `HARKEN_CC_TENANT_ID` is therefore
NOT a tenant inventory; it is the authoritative tenant IDENTITY of the
deployment being migrated. Migration 0021 pins that one tenant, plus any
tenant already carrying a `cc_tenant_settings` row, and enumerates no
operational table to infer that a tenant exists. A quiet tenant with no
operational data is covered because the deployment's configured identity
is itself authoritative.

*The pinned value is `legacy_open`.* That is the posture an existing
tenant already has — the missing-row default has answered `legacy_open`
since E1.2 — so pinning it changes nothing and states what was
previously implied. Pinning an existing tenant STRICT would be a silent
posture change that could lock out a working deployment, which is the
exact harm E1.2 seeded `legacy_open` to avoid. An explicit row of either
posture is preserved untouched.

*An active tenant has an administrator.* A new tenant is born STRICT, and
strict enforcement with no administrator is an unusable tenant, so an
authoritative owner SUBJECT becomes a precondition of tenant creation.
The Console's existing fail-closed creation path (E1.4) carries it: no
`admin_email`, or a Keycloak owner that cannot be minted, now rolls the
tenant back instead of returning an active tenant nobody administers. No
new tenant lifecycle state is introduced — `tenants.status` keeps its two
values, and billing, `/api/me` and admin listing keep filtering on
`active` unchanged.

*The first grant is a provisioning act, not a principal's act.* Central
Command seeds ONE tenant-scope `tenant_owner` grant for the Console-
recorded owner subject, pulled over the existing CC→Console internal
channel. It is written by a dedicated repository seam that refuses if the
tenant carries ANY grant row of any lifecycle state — never
`ScopeGrantRepo.grant()`, which revives a revoked row and would let a
deliberately removed administrator return outside the grant lifecycle,
contradicting A23.10. It never routes through the human admission
sequence, adds no self-grant exception, creates no hidden administrator
and confers nothing beyond one ordinary grant that A23.8 then protects
like any other. Attribution is `system:tenant_birth`; the act is audited
once and is inert forever after.

*Migrated tenants are not newly born tenants.* Strict birth applies to
tenants created after A23-5. A historical tenant discovered without a
usable administrator keeps its pinned posture and is REPORTED through the
existing `locked_out` reading; no synthetic administrator is invented for
it. (A23-5.)

**A23.12 — Invariants A23 must leave standing.** No grant never means
tenant-wide authority. Revoked, expired or orphaned grants never mean
tenant-wide authority. A vanished target never widens. Caller scope never
enlarges campaign scope. Declared scope is runtime-enforced. Delegation
requires reach AND authority. Self-grant is forbidden. The last
`role.manage` authority cannot be configured away. Identity is stable via
`principal_ref`/`actor_ref`. The historical chain is never rewritten.
Secure CC requires an explicit realm. Metering is scope-free. Agents get
no synthesis. Platform staff get no tenant-plane bypass. No new
permission, no second RBAC, no second scope resolver, no second approval
system, no second execution engine. A23 is foundational enterprise
security for the full product — multi-region tenants, delegated
administrators, many Site Managers, Operational Agents, campaigns,
autonomous execution, external agents, MCP, event-driven operation and
natural-language agent creation — and reduces none of it.

**A23.13 — What A23 does not build.** No A6 ingress, no MCP, no S11, no
Dell identity, no machine-ceiling change, no node-authority change, no
platform break-glass, no scope-resolution cache (recorded as a scale
follow-up), no Keycloak-side user lifecycle, and no change to the Site
Manager's own site-token approve route (recorded by A22).

### A24 — 2026-09-05 — A6 external agent ingress (decided: Vinod)

**Context.** An external Operational Agent runtime has held a credential
since A3 and has been able to reason about itself since A5's dry-run, but
it has never been able to submit work. The pre-implementation
investigation on `main` at `d11840c` found that three of the four pieces a
first ingress slice would need already exist — the credential, the
token→identity→agent→tenant→scope binding, and dry-run, which is already
reachable by a machine principal at `fleet.view` with an object-level
self-gate. The single real gap is **submission**, and the machine
permission ceiling is read-only by construction.

It also found that a `POST /proposals` carrying an action cannot be built
without breaking four ratified properties: `govern_proposal()` is
condition-driven rather than action-driven, `resolve_action_params()`
derives parameters and never accepts them, `open_dedupe_keys` is
structurally unavailable to an HTTP caller (A22.1), and a body able to
carry `authorization_basis` would be a self-signed execution order
(A22.15).

**A24.1 — Propose-by-reference, not propose-by-construction.** The
external agent does not construct a proposal. It selects a candidate
Central Command has already governed and shown it through the existing
dry-run, and asks for it to be recorded. Authorship stays server-side;
the agent contributes which and when. HarkenIQ remains the sole authority
that derives an executable proposal.

**A24.2 — The transport contract is closed.** The body carries exactly
`candidate_ref`, `idempotency_key`, optional `observed_at` and an optional
bounded `note`. `agent_id`, `action_type`, `device`, `params`,
`disposition`, `authorization_basis`, `status`, `decided_by`, autonomy
level, approval and site are **unrepresentable** — rejected by the schema,
never merely ignored. The prohibition on caller-supplied `disposition`,
`authorization_basis` and `status` is PERMANENT and survives any future
extension of A6.

**A24.3 — `candidate_ref` is not an authority token.** It is opaque and
server-minted. On receipt Central Command re-loads current state and
re-runs the existing `govern_proposal()` before anything is persisted. A
candidate that no longer governs the same way is refused with the CURRENT
reason — never narrowed, never coerced, never honoured because it was
valid when issued.

**A24.4 — Effective machine authority is a binding INTERSECTED with the
ceiling.** `proposal.submit` enters the permission vocabulary and
`MACHINE_PRINCIPAL_CEILING`. The ceiling does not grant it. An agent
submits only where an explicit capability binding names that authority
AND the ceiling admits it:

    effective = explicit agent bindings  ∩  MACHINE_PRINCIPAL_CEILING

An Operational Agent without an explicit ingress binding cannot submit
merely because the ceiling permits the class. This preserves A20.3's
shape exactly: the binding table and the ceiling stay separate objects,
so adding a binding can never raise the ceiling.

**A24.5 — A machine agent acts as itself.** For external machine
submission the token-derived agent id MUST equal the route's agent id. No
body field can select another agent. Human access to the same surfaces
remains governed by ordinary scope rules. A6-2 normalizes the same self
semantics across the remaining machine-facing proposal, runtime and
preflight reads, which today are asymmetric: `dry-run` restricts an
identity to its own agent and the adjacent reads do not.

**A24.6 — One proposal-admission path.** Ingress and the CC-resident
evaluator admit proposals through the SAME function. Two concurrent
submissions for the same governed logical candidate must not create two
open proposals, and must not be able to do so by presenting different
idempotency keys — logical duplication is governed by the dedupe key the
verdict function already computes, not by the transport's replay key.
Idempotency and logical duplication are two different guarantees and both
are required.

**A24.7 — Submission does not consume execution budget.** `execution_budget`
counts actions actually executed (A19 D2) and is asked at dispatch. A
submission or a retry consumes neither it nor any autonomy grant. The
existing per-agent daily proposal cap remains the back-pressure on
creation.

**A24.8 — Ingress abuse controls are part of the first slice.** A6 admits
the platform's first programmatic, retrying, external writer. A bounded
body, a closed schema and a durable per-identity rate control ship with
it. The rate mechanism must be correct under the deployed runtime model —
Central Command runs multi-replica, so a per-process counter would be
decorative — and must not introduce new infrastructure.

**A24.9 — What A6-1 does not build.** No MCP. No streaming, webhooks or
event fan-out. No agent-supplied evidence or telemetry. No autonomy
change: no action class enters the ladder. No Site Manager or node
authority change. No `/api/v1`. No second RBAC, scope resolver,
capability registry, agent identity, approval system, execution engine,
audit universe or proposal governance function.

**A24.10 — A6-1 is a floor, not a ceiling.** Propose-by-reference is the
first SAFE external ingress contract, not the final limit of external
agent reasoning. A future governed slice may extend A6 toward capability
intent or evidence ingestion — which would need its own evidence-trust
model — while preserving A24.1: HarkenIQ remains the sole authority that
derives an executable proposal.

### A24 addendum — 2026-09-05 — pre-merge red-team findings (decided: Vinod)

An independent read-only review of PR #34 found one blocker and four high
findings at the new external machine-write trust boundary. Every claim was
independently reproduced against the code before any was implemented; all
five were correct, and a sixth was found while reproducing them. The A6
architecture is unchanged — propose-by-reference, server-derived identity,
binding ∩ ceiling, `candidate_ref` as a lookup, one `govern_proposal()`,
one `admit_proposal()`, unchanged CC→SM→Node authority.

**A24.11 — Idempotency must be serialized, not merely constrained.** The
unique constraint makes a duplicate impossible; it does not make a
concurrent duplicate *handled*. Reproduced on PostgreSQL: when two
requests carrying one key both complete the replay lookup before either
inserts, the loser raises an unhandled integrity error. Ingress therefore
takes a transaction-scoped advisory lock covering the whole
lookup→process→persist→commit sequence for that principal, in the
established `pg_advisory_chain_lock` pattern. Exception handling is a
backstop, never the concurrency architecture.

**A24.12 — Attribution identity is not operational identity.** The
proposal dedupe key begins with the proposing agent, so two agents could
hold two simultaneously active proposals for the SAME mutually exclusive
physical operation. Agent identity remains provenance. Operational
collision identity is server-owned and independent of the proposer, and is
DERIVED from the canonical action-parameter contract rather than
hard-coded: an operation is identified by its device, its action class,
and those parameters the contract marks as addressing the affected
component (`source == component`). Parameters the contract states no
executor reads are excluded by that same rule. Two DIFFERENT operations on
one target remain legitimately concurrent. Enforced for OPEN proposals
only — a settled proposal must not fence a device forever.

**A24.13 — Ingress rate is an ATTEMPT rate, enforced atomically.** Count,
compare and admit must be one atomic decision for the deployed
multi-replica runtime; a non-atomic count is advisory. Every external
attempt counts — first submission, replay, idempotency conflict, rejected
candidate, and authenticated authorization refusal — because a replay that
skipped the counter is an unmetered channel. A replay stays functionally
idempotent; it is not free. Attempt accounting is bounded by its own
limit: once over, a request is refused without adding to the record it
would otherwise grow.

**A24.14 — A body must be bounded before it is parsed.** Schema field
limits reject a payload the server has already read and allocated.
Ingress therefore enforces a byte ceiling at the transport layer, counting
bytes actually received, correct for both declared and chunked bodies, and
never trusting a declared length.

**A24.15 — Current authority is revalidated at execution time.**
Proposal-time authorization is not permanent execution authority. Before
dispatch, in addition to the existing agent status, pause, retirement and
credential checks, the platform revalidates that the agent's CURRENT scope
still reaches the target and its CURRENT capability binding still permits
the class. Withdrawn authority fails closed. Historical provenance on the
proposal is unchanged, and the node remains the final authority.

**A24.16 — Attempt telemetry is not the audit chain.** The audit chain is
authoritative governance history and is hash-chained per entry; appending
to it on every hostile or malformed request is an amplification channel
against the platform's own integrity store. Governed outcomes — a
submission that produced a proposal, an authenticated identity refused —
remain audited. High-volume attempt outcomes are counted, not chained.
