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
one target remain legitimately concurrent.

*Corrected 2026-09-05, before the implementing code.* This clause first
read "enforced for OPEN proposals only". `OPEN_STATUSES` is a different
question's answer and rightly contains `denied`, because a denial is
final (D16) and the agent that was refused must not relitigate it.
Fencing a PHYSICAL operation on that status would let one human's refusal
block that operation for every agent forever. The rule is therefore
IN-FLIGHT proposals only — work that can still reach a device:
`proposed`, `awaiting_approval`, `approved`, `dispatched`. Not `denied`
(final), not `blocked` (never dispatches), and not `completed` or
`failed` (settled; re-running or retrying is legitimate work). The
per-agent dedupe rule is unchanged and still treats a denial as final.

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

### A25 — 2026-09-05 — A6-2 machine status and outcome correlation (decided: Vinod)

A6-1 answered how an external Operational Agent submits governed intent.
A6-2 answers how that same agent reliably follows what HarkenIQ did with
it. It builds the machine feedback loop into the existing governed
capability plane; it does not reduce it. External agents are first-class
actors, not reduced UI automation clients.

**A25.1 — Exact correlation, and the defect it closes.** Central Command
holds an exact execution key and discards it before settlement. The Site
Manager records every directed execution as
`action_id = "directive:<directive_id>"`, `FleetOutcome.action_id` carries
it, and `cc_outcome_history.action_id` stores it — but the projection the
settlement loop consumes omits the field, so proposals are settled by a
heuristic on device, action class, actor and a time window. Two dispatched
proposals for one device and one action class can therefore be settled by
each other's outcome. Settlement now joins on the exact key. The heuristic
survives ONLY for outcomes that can carry no key — a proposal dispatched
without a directive id — and that fallback is explicit, counted and
tested, so its eventual retirement is a measurement rather than a guess.
A proposal that holds a directive id waits for its own outcome and is
never settled by another's: unsettled and visible beats settled and wrong.

**A25.2 — Historical receipt, bounded (D1).** Operational reads stay
current-authority. No grant, revoked, expired or vanished scope never
restores estate visibility. One exception, and only one: the SAME logical
Operational Agent that submitted a governed request retains access to the
lifecycle RECEIPT for that exact submission after its scope narrows or is
revoked. This is historical transaction attribution, not operational
authority.

Where current authority is absent the receipt may carry only:
submission id, accepted/refused state, proposal id where one exists,
proposal lifecycle state, approval required/state/counts, execution and
terminal state, outcome classification, lifecycle timestamps, and a
bounded refusal or failure reason. It may NOT carry device or site
details, fleet state, raw evidence, executable parameters, authorization
basis internals, human approver identity, group membership, other agents,
or any current estate information.

**A submission id is not a bearer credential.** Receipt access requires
authentication as the same logical agent that created the submission.
Cross-agent and cross-tenant access fail closed. The exception covers an
agent's own submission and proposal lineage and generalizes to nothing
else; it is not an A23 scope bypass.

**A25.3 — Approver identity is never machine-visible (D2).** A machine
projection may report that governance occurred — required, state, granted
and required counts, and justified timestamps. It may not report who: no
`decided_by`, no approver email, no Keycloak subject, no group membership.
The canonical approval ledger is unchanged; safety is achieved by
projection, never by weakening the record.

**A25.4 — The layers stay separate.** No synthetic lifecycle status is
invented. The machine projection preserves submission, proposal, approval,
execution, outcome and terminality as distinct facts. Proposal status is
not approval state is not execution state is not outcome. `approved` does
not mean executed and is NOT terminal. `PARTIAL` and `ROLLBACK` never
silently become a generic external failure without the canonical outcome
classification beside them. The legal `approved → awaiting_approval`
transition, which the per-agent budget produces, stays representable.

**A25.5 — Machine-self is normalized (closes G5, per A24.5).** An
Operational Agent must not inspect another Operational Agent merely
because both occupy overlapping estate scope. The self-restriction that
`dry-run` and submission already carry is extended to the remaining
agent-addressed machine routes. Human administration continues under the
ordinary scoped RBAC model and is not converted into a machine-self route.

**A25.6 — Reads are metered separately from governed attempts.** Status
polling and proposal submission are different traffic with different
meaning, and must not share an accounting bucket. The A24.13 ledger
continues to count governed submission attempts; read traffic is counted
in its own bucket so that abuse detection, tenant and per-agent quotas,
capacity management and entitlements can later distinguish them. Normal
polling reads are never appended to the governance audit chain.

**A25.7 — Caching may not conceal a transition.** Strong caching is
permitted only where the underlying state can no longer change. `approved`
is not terminal, and no caching semantics may hide `approved →
awaiting_approval` or any later dispatch or outcome transition.

**A25.9 — The projection follows the authorization, everywhere (pre-merge
remediation).** When a route becomes readable by a machine principal, its
PAYLOAD becomes part of that decision. A25.3 and A25.5 apply to the whole
Operational Agent surface, not only to the receipt endpoints: the agent
detail, the agent listing, the preflight read, the runtime read and the
identity read are answered from allow-listed machine projections built by
NAMING the fields that may pass. Filtering a rich payload by removal is
forbidden — a subtractive filter leaks the next field added upstream. Two
withheld sets are distinguished. Operator identity (`decided_by`,
`approvers`, `created_by`, `activated_by`, `produced_by`, `acknowledged_by`,
`issued_by`, `rotated_by`, `revoked_by`, governing policy and group names)
never reaches a machine on ANY response FROM THIS SURFACE — see A25.13 for
the one place outside it where the rule is not yet true. Execution and
delivery internals
(`params`, `evidence`, `rationale`, `authorization_basis`, `directive_id`,
`dispatch_reason`) never reach a machine on a LIFECYCLE or STATUS response;
`dry-run` is the single deliberate exception, because A22.2 requires it to
return the agent's own resolved parameters for work it has not proposed.
The agent-binding catalogue is an operator surface and is refused to a
machine principal: it enumerates the tenant's bindable sites and devices,
and a machine has nothing to build and no self to narrow the answer to.

**A25.10 — Accounting identity is server-derived, and accounting precedes
the target decision (pre-merge remediation).** The read bucket is resolved
exclusively from the authenticated principal — the tenant and Operational
Agent the validated token names. It is never derived from a route
parameter, a request body, a query value, a proposal or submission
identifier, or any other caller-supplied identity; otherwise a caller
could spend another agent's allowance. The order is: authenticate, derive
the canonical machine identity, ACCOUNT against the caller, then decide
about the target and respond. Accounting is not authorization and may
never weaken a permission, scope or self check — but an authenticated
refusal that costs nothing is an unbounded channel, so a cross-agent,
not-found or otherwise refused machine read is charged to the caller that
made it. Every machine-readable route on this surface consumes the same
allowance, so no route can become the unmetered substitute for another.

**A25.11 — Durable read accounting owns its own transaction (pre-merge
remediation).** The read meter opens a short-lived session from the
canonical session infrastructure, writes and commits ONLY the counter, and
closes. It never receives, commits or rolls back the caller's business
transaction — the same rule the counter's own SAVEPOINT already applied
one level down. Charges are therefore durable independently of whatever
the request does afterwards, including a refusal.

**A25.12 — Approval completion is per subject (pre-merge remediation).**
Approval state is read from the E0.1 ledger by `subject_ref`, so it may
never be cached, shared or inferred across subjects. Policy RESOLUTION —
which depends only on action class, device type and risk — may be cached.
A projection that cached completion under policy coordinates lets two
proposals sharing an agent, a device and an action class report one
another's approval state, decided by list order.

**A25.13 — Named follow-up, found by the A25.9 sweep and NOT fixed here.**
Sweeping every route a machine principal can reach found one outside the
Operational Agent surface where A25.3 is not yet true: `GET
/api/policies/groups/{group_id}` is gated at `fleet.view` (E0.3's A13
read-split) and returns each member's email address and Keycloak subject,
so an authenticated Operational Agent can enumerate the tenant's
APPROVERS. `GET /api/policies/` and `GET /api/policies/groups` likewise
return policy and group names and `created_by`. This is PRE-EXISTING — it
has been reachable by a machine principal since A3 gave one `fleet.view`
(2026-08-31) — and A6-2 neither introduced nor widened it. It is recorded
rather than fixed because A6-2 is the Operational Agent surface and the
approval-policy router has human consumers this slice did not review. The
decision on whether to refuse a machine principal there, and on whether
approver membership should be readable at `fleet.view` at all rather than
at `action.approve`, is Vinod's.

**A25.8 — What A6-2 does not build.** No MCP, webhooks, event streaming or
SDKs. No Console UI. No new autonomy policy, approval model, RBAC, scope
resolver, execution state machine or outcome store. No `/api/v1`. No new
permission and no machine-ceiling change: `fleet.view` already suffices.
No change to what an external caller may supply — A24.2's unrepresentable
set is permanent. These remain on the roadmap; they are simply not this
slice.

### A26 — 2026-09-06 — A25.13 governance visibility boundary (decided: Vinod)

Closes the pre-existing HIGH that A6-2's sweep found and independent
review confirmed. Recorded BEFORE the code, per change control.

**A26.1 — The defect, stated exactly.** `GET /api/policies/groups/{group_id}`
is authorized with `fleet.view` and returns approval-group membership:
each member's email address, role, canonical `principal_ref` and
subject-binding status. `GET /api/policies/groups` is likewise
`fleet.view` and enumerates the tenant's approval groups, their external
escalation channels and their creator. `fleet.view` is held by every
tenant role down to `viewer`, and by every machine principal (A20.3), so
an authenticated Operational Agent — and any human viewer — can enumerate
the tenant's APPROVERS. Tenant isolation holds; the authorization
boundary does not. Reproduced on `5fb38e8` before any change was written,
for a machine principal, a `viewer` and an `auditor` alike.

**Infrastructure visibility is not governance-topology visibility.** The
two were conflated because both were reachable behind one permission.

**A26.2 — `governance.view` enters the fixed vocabulary (spec §4).** The
25th atomic permission, and the first added since A24's
`proposal.submit`. It answers exactly one question:

> *May this principal inspect governance configuration and governance
> topology within its effective tenant and scope?*

It is READ ONLY. It does not imply, and may never be read as implying,
`action.approve`, `site.manage`, `role.manage`, `tenant.manage`,
`audit.export`, delegation authority, governance mutation, autonomy
authority or execution authority. **Visibility is not authority.**

**A26.3 — The two directions are independent, and both are load-bearing.**

    action.approve  !=  governance.view
    governance.view !=  action.approve

An approver may decide an approval without authority to enumerate the
tenant's approval topology — which is why `operator` does NOT receive
`governance.view`, though it holds `action.approve`. Deciding a subject
needs `/api/approvals/{action_id}/records`, which is decision EVIDENCE
about one subject and is already correctly gated (`action.approve` OR
`audit.view`, E0.3). And a governance auditor may inspect topology
without being able to approve anything — which is why `auditor` DOES
receive it. Neither permission is ever inferred from the other.

**A26.4 — Role mapping.** Granted to `tenant_owner`, `site_admin` and
`auditor`. Withheld from `operator`, `viewer` and `platform_support`.
The two `site.manage` roles already reach this data through the Console
(whose Policies page is `site.manage`-gated), so nothing they hold is
taken away; the `auditor` is A13/OQ-24's read-only-everything persona and
already reads approver identity through the approvals evidence routes.
`viewer` and `operator` lose an exposure they never had a product path
to. `platform_super_admin` semantics are unchanged and no tenant-plane
platform bypass is introduced.

**A26.5 — Machine principals cannot hold it.**
`MACHINE_PRINCIPAL_CEILING` is NOT widened: it remains
`{fleet.view, incident.view, proposal.submit}`. No A0 read binding maps
to `governance.view`, and no implicit expansion may introduce it. The
effective-set intersection (A20.3) therefore makes it structurally
unreachable by any machine identity, whatever bindings it is given.

**A26.6 — What moves, and what deliberately does not.** Two routes move
to `governance.view`: the approval-group LIST and the approval-group
DETAIL. Enumerating the approval groups is itself governance topology, so
leaving the list at `fleet.view` would leave the structure readable with
only the names removed.

Three governance reads KEEP `fleet.view`, on ratified grounds rather than
convenience: `/api/policies/` (A13/E0.3 and S1 D2 — that an action needs
two approvers is POSTURE, readable by the people living under it),
`/api/policies/autonomy` (S1 D2 verbatim) and `/api/policies/stop-switch`
(safety posture; everyone must be able to see the estate is halted). No
permission is replaced mechanically.

Every governance MUTATION stays at `site.manage`. Every approval
decision and evidence route is untouched. The A6-1 ingress and A6-2
machine status surfaces are untouched.

**A26.7 — The policy list is projected, not filtered.** `created_by` on
an approval policy is governance identity metadata, and it was reaching
machine principals and viewers. Posture stays broadly readable and the
AUTHOR does not: a caller holding `fleet.view` without `governance.view`
receives an explicit operational projection built by NAMING the fields it
may have; a caller holding `governance.view` receives that projection
plus `created_by`. Constructed positively — the rich object is never
serialized and then stripped, because a subtractive filter leaks the next
field somebody adds upstream (A25.9's rule, applied here).

**A26.8 — Scope and lifecycle are unchanged and still fail closed.**
`governance.view` is subject to tenant identity, the E1.2 effective
scope, and the A23 grant lifecycle. It is NOT tenant-global merely
because it is a governance permission. In particular a narrowed
`permission_subset` that does not name `governance.view` does NOT acquire
it because the underlying role now carries it — deliberate fail-closed
behaviour, and tested as such. No migration and no backfill: existing
narrowed grants keep exactly the reach they were given.

**A26.9 — Named production follow-ups, recorded and NOT implemented.**
Neither is discarded; both are sequenced after this slice.

1. **Read-only Governance experience** for auditor, compliance and
   security-review personas. The Console Policies page stays
   `site.manage`-gated in A26, so an auditor gains the API read and no
   page. This is a product/UX capability, not a permission question.
2. **Governed sensitive-read audit / security observability.** Measured
   during this slice: ZERO of the platform's 97 declared routes audit a
   GET — sensitive-read auditing does not exist anywhere in HarkenIQ, so
   there was nothing to preserve or extend here. A real
   production-readiness capability; when it lands it must extend the
   canonical hash-chained audit architecture and must not introduce a
   second audit system.

**A26.11 — Eligibility is not authority (pre-merge remediation).** A26's
first implementation guarded the two topology reads with
`require_permission("governance.view")` alone and selected the policy
projection with `has_permission(user, "governance.view")`. Both ask
NOMINAL role membership. E1.2 states the rule they broke in its own
words: the route guard answers *"could this actor ever hold this
permission"*, and it cannot answer the other question, because
`permission_subset` is PER GRANT and the effective permission is
therefore object-dependent.

Reproduced against the running handlers under STRICT enforcement with
rows in `cc_scope_grants`: a SITE-scoped `site_admin`, an ORG-scoped
`site_admin`, and a tenant-wide grant whose subset was `["fleet.view"]`
each read every approver's email address and Keycloak subject, and each
received `created_by`. The platform's canonical chain —

    role permission ∩ effective grant permission ∩ effective scope
                    ∩ grant lifecycle ∩ tenant boundary

— was being cut after its first term.

Both topology reads and the policy projection now ask the canonical
E1.2/A23 resolver through ONE predicate,
`scope.permits("governance.view", tenant_object=True)`. `tenant_object`
because `cc_approval_groups` is keyed by tenant and has no site
dimension: the authority that reads tenant-wide approval topology is
authority over the TENANT. A site-, org-unit-, device- or
device-class-scoped grant reads nothing, however broad the principal's
ROLE — including `site_admin`, whose role holds `governance.view`. That
is deliberate and is not a capability reduction: site-specific
governance topology, if the product later wants it, is a correctly
scoped projection rather than tenant-wide leakage. Revoked, expired,
inert/orphaned, missing and narrowed grants all fail closed.

**The refusal shape is the canonical READ shape, not a new one.** The
platform's `test_no_read_is_object_gated` invariant holds that a GET must
narrow rather than 403, *because a 403 on a read confirms the object it
refuses* — and here the existence of a group id IS the topology being
protected. So a caller without effective authority receives an empty
group list and a 404 on the detail, the same answer a cross-tenant id
already gets. `forbid_out_of_scope` was considered and rejected for
exactly this reason; using it would have required weakening a
platform-wide read invariant for one route. A principal whose ROLE lacks
`governance.view` is still refused 403 by the route guard, which names no
object.

All three routes are re-declared **READ_SCOPED**, because their answer
now varies with the resolved scope and the A23 route contract must
describe runtime truth in both directions — declared-scoped/runtime-
unscoped and declared-unscoped/runtime-scoped are equally wrong. The
A23.2 consumption census enforces the first; a test naming these three
routes enforces the second.

No second RBAC, resolver or authorization mechanism was introduced: one
predicate delegating to `ResolvedScope.permits`, which already checks
coverage and permission on the SAME grant and skips inert rows.

**A26.10 — What A25.13 does not build.** No Console UI. No read
auditing. No new role. No second RBAC, scope resolver, approval system,
identity model, capability authority, execution engine or audit system.
No machine-ceiling change. No schema change. No change to A6-1 ingress or
A6-2 status. A6-3 is not started.

### A27 — 2026-09-06 — A6-3 external-agent provenance and ingress operability (decided: Vinod)

A6-1 gave an external runtime a governed way to WRITE; A6-2 gave it a
governed way to READ what happened. A6-3 completes the third side of the
same interaction: the **human's** ability to attribute and supervise it.
Recorded BEFORE the code, per change control.

**A27.1 — The gap, measured on `6748baf`.** `origin` (`evaluator` |
`ingress`) is passed to `admit_proposal()` and written **only into the
audit-entry detail JSON**. There is no column on `cc_agent_proposals` and
no API payload carries it. Meanwhile `/api/approvals/` already spends the
word `origin` on the queue LANE (`node` / `agent` / `agent_activation` /
`campaign_wave`), so an approver sees `"agent"` for a proposal HarkenIQ
reasoned itself AND for one an external runtime asked for. A24's own
comment claims "an approver is never left guessing"; today they are.
Separately `cc_agent_submissions`, `cc_agent_ingress_attempts` and
`cc_agent_read_windows` are written by the meter and the submit route and
read by **nothing else** — no human can answer whether a runtime is
submitting, being refused, throttled or silent, though A25.6 collected
the data for exactly that purpose.

**A27.2 — Provenance is its own first-class concept (D2).** A dedicated
persisted discriminator `cc_agent_proposals.provenance_type`, NOT an
overload of the approvals lane `origin`. Closed vocabulary, canonical
values:

    evaluator         HarkenIQ's own CC-resident loop derived it
    external_ingress  an authenticated external runtime asked for it
    unknown           no authoritative provenance exists for this row

The value is `external_ingress`, deliberately not shortened to
`"ingress"`. The vocabulary is designed so a later ratified source
(skill, event, MCP, operator, campaign) is ADDED without reinterpreting
today's values; none of those is implemented here.

**A27.3 — One semantic fact, one canonical spelling going forward (D1).**
`ORIGIN_INGRESS` becomes the canonical `external_ingress` and is the
single source both the column and new audit entries read. Historical
audit entries keep `{"origin": "ingress"}` and are **never rewritten** —
the chain is immutable and a rewrite would be a worse lie than an old
spelling. The pre-A27 spelling is documented where it can be met.

**A27.4 — No backfill, ever (D3).** The column is additive and nullable
with no data migration. A pre-A6-3 proposal has no authoritative
provenance and projects as `unknown`; it is never manufactured into
`evaluator`. Unknown is not a default — it is the honest answer, the same
rule A17.4 and A19.9 already apply to capability reach and activation
provenance.

**A27.5 — Provenance is written transactionally with the proposal.** It
is set inside the existing `admit_proposal()` transaction, from the
`origin` that function already receives, so a proposal cannot exist
without the provenance of its own creation. No second write path, no
reconciliation job, no inference from the audit log.

**A27.6 — Provenance is projected where the DECISION is made.** The
approvals queue item, approvals history and the human proposal views
carry a structured block:

    provenance: { type, submission_id }

`submission_id` appears only for `external_ingress`, only because A6-1
already persisted it as authoritative, and only to let an operator
correlate a decision with the submission that asked for it. Nothing else
travels: no credential, no client id, no secret, no token, no realm
subject, no identity material beyond what the surface already carried.

**A27.7 — Machine projections are unchanged.** A machine already knows
it submitted — it holds the submission id A6-1 returned. Adding
provenance to the A25 machine projections would widen a payload to tell a
runtime something it supplied, so it is not added. A25.3 and A25.9 stand
untouched.

**A27.8 — Ingress health is agent-anchored and governed like every other
agent read (D4).** `GET /api/operational-agents/{agent_id}/ingress`, at
`fleet.view`, through the canonical `get_scope` resolver and
`_require_visible_agent`, declared READ_SCOPED because its answer varies
with the resolved scope. Ingress health is OPERATIONAL STATE, not
approval authority: `action.approve` is not required to see whether a
runtime is healthy. A machine principal reads its OWN and no other, via
the existing A25.5 mechanism — no new self rule is invented. There is no
tenant-wide roll-up in A6-3.

**A27.9 — Bounded by construction (D3).** Counts come from the existing
one-hour `cc_agent_ingress_attempts` window and the current
`cc_agent_read_windows` counter; recent refusals are the most recent 20
`cc_agent_submissions` rows by `created_at DESC` under a deterministic
`LIMIT`. No unbounded historical scan, no new retention, no second
telemetry or accounting subsystem. Refusal `code` and `reason` are
projected bounded and sanitized.

**A27.10 — Observed activity, never invented connectivity (D5).**
HarkenIQ holds no heartbeat, session or connection signal for an external
runtime, so A6-3 claims none. `AgentIdentityRepo.touch()` is called on
every authenticated machine request, which makes
`cc_agent_identities.last_seen_at` a real persisted observation; it is
projected as `last_authenticated_at`, meaning exactly *the last
authoritative persisted authentication observation* and never "connected".
`last_seen_source` is caller-supplied and is NOT exposed.

**A27.11 — `activity_state` has deterministic precedence, defined before
the code (D5).** A derived summary must never be ambiguous, and it is a
convenience over the raw observations, never an authority. The raw
counts and timestamps are authoritative and are always returned beside
it. Precedence, first match wins, evaluated in this order:

    1. never_authenticated   no last_authenticated_at, no attempt ever
    2. throttled             throttled_count > 0 in the current window
    3. repeatedly_refused    refused_count > 0 and accepted_count == 0
                             within the attempt window
    4. active_recently       any attempt or authentication within 1 hour
    5. idle                  last observation within 24 hours
    6. no_recent_activity    otherwise

**A27.12 — What A6-3 does not build.** No events, webhooks, streaming or
subscriptions. No MCP. No SDK. No capability or parameter discovery API.
No tenant-wide ingress roll-up. No heartbeat protocol or connectivity
subsystem. No evidence ingress or trust model. No cursor pagination and
no `/api/v1`. No machine-ceiling change, no new permission, and no
change to RBAC, scope semantics, autonomy, approval authority, capability
authority, execution, Site Manager coordination or Harken Node final
authority. The observation that a machine principal reaches 45 of 97
routes because `fleet.view` is one broad key is RECORDED here and belongs
to A6-4's deliberate External Agent API Plane; it is not solved in A6-3.

**A27.13 — Pre-merge remediation: a real 429 must be observable.**
Independent review found the ingress attempt vocabulary carries no
`throttled` outcome — A24.13 refuses an over-limit submission WITHOUT
writing, deliberately, so the traffic a rate limit exists to bound
cannot grow the table that bounds it. A27.8's projection nevertheless
read `throttled` out of that same ledger, so the field was zero for
every agent forever, A27.11's `throttled` state was unreachable by any
amount of real traffic, and an operator could not distinguish a silent
runtime from one being refused at the door.

Both facts stand. A rejection still never enters
`cc_agent_ingress_attempts` — a rejection counted as an attempt would
consume the allowance it was just refused for, and the limit would eat
itself. The refusal is instead counted in `cc_agent_throttle_windows`
(CC migration **0025**, additive, no backfill): one row per (tenant,
agent, aligned minute), incremented in place, so the bound is TIME and
not request count — a flood of a million requests in one minute writes
one row and increments it. Only an ACTUAL rejection marks it; the
request that merely consumes the last slot was SERVED and is not
throttling, because a state meaning "at the limit" is a different and
far commoner fact. Atomicity comes from the same single-statement UPDATE
plus unique-constraint-guarded insert that `cc_agent_read_windows`
stands on, inside a SAVEPOINT so a lost open race costs one statement
and never the caller's transaction (A25.12). `/ingress` reports the
count and `last_throttled_at` on the SAME window as the attempt counts,
so an operator reads one horizon rather than two that disagree.

Nothing else moves: the attempt vocabulary, `ATTEMPT_WINDOW_S`,
`ATTEMPT_MAX`, the 20-row refusal sample, machine-self behaviour,
`fleet.view` + `READ_SCOPED`, and `MACHINE_PRINCIPAL_CEILING` are all
unchanged.

**A27.14 — Pre-merge remediation: provenance is consumed by a human.**
A27.6 put provenance on both human payloads and the Console rendered
neither, so the fact existed and no operator could see it. The Console
now carries a typed, allow-listed presentation module applying the
SERVER's own reading rule — `evaluator` → "HarkenIQ evaluator",
`external_ingress` → "External agent runtime", and anything absent,
empty or unrecognised → "Unknown historical source", never inferred into
`evaluator` (A27.4). It is rendered on both intended surfaces: the
approval-queue agent-proposal card, where the decision is made, and the
Operational Agent proposal history. The queue's `origin` LANE is
untouched and provenance is never merged into it (A27.2); the submission
id is shown only for `external_ingress`, as bounded correlation detail
that confers nothing (A25.2). No authorization internal is exposed.

### A28 — 2026-09-07 — PX0: the HarkenIQ product-experience architecture (decided: Vinod)

Every amendment to this section so far has governed a platform
capability. This one governs the **surface through which a human reaches
every capability already built**, and it is recorded because that surface
is now the platform's weakest layer by a wide margin.

Ratified after an independent architecture review, with amendments. The
assessment was measured on `main` at
`3e8369e5bf31691e4e0a0982420dfe4cbe093270` (A6-3 merged, main verified).

**A28.1 — The finding, measured.** HarkenIQ can state, for any action on
any device at any moment, what the node can execute, what policy permits,
whose scope reaches it, whether autonomy grants it, how many humans must
approve, what the blast radius is, what happened, and what the fleet
learned. The Console renders that as **37 independent pages with nine
links between them**. Measured on the assessed commit: 519 inline
`style={{}}` sites and 310 locally declared `CSSProperties` constants,
with one concept redeclared up to fifteen times; **zero** uses of
`useSearchParams`, so no incident, proposal, approval, campaign wave or
agent has a URL; **zero** `@media` queries in either UI; 53 `<label>`
elements against 6 `htmlFor`; one `aria-live` region; no focus trap or
focus restore in the drawer or the dialog; 29 pages choosing a status
variant with no shared mapping; `GET /api/attention` rendered by a page
titled "Risk & Exposure"; and the string "External Agent" appearing
**zero** times, though A6-1, A6-2 and A6-3 shipped that actor class.

**A28.2 — It is the house pattern, at the product layer.**
`harkeniq_cc/receipts.py` already composes the operating narrative as
named blocks — `submission_block`, `proposal_block`, `approval_block`,
`execution_block`, `outcome_block`, `terminal_block`, assembled by
`build_receipt()`. It was built in A6-2 **for machines**, and no human
surface consumes it; the Console renders the flat `proposal_dict`
instead. Declared, written, and unreadable at the point where somebody
must act on it — the eleventh recorded instance, and the first outside
the backend. The external agent receives a governed lifecycle receipt;
the human operator receives a list.

**A28.3 — What PX is, and is not.** PX is the connective human operating
layer over the governed architecture that already exists. It is NOT a
cosmetic redesign, NOT a frontend rewrite, and NOT a second governance
system. It creates no second domain model, RBAC system, scope resolver,
capability authority, autonomy model, approval system, execution engine,
identity model or agent governance plane. Human UI, external agent
runtimes, future MCP clients and integrations all remain consumers of the
SAME governed capability plane, and the Harken Node remains final
execution authority.

**A28.4 — The operating narrative.** Nine stages, in order, each mapping
to contracts that already exist:

    Attention -> Evidence -> Agent Assessment -> Recommendation ->
    Capability -> Governance -> Execution -> Outcome -> Learning

`Capability` is a stage and not a footnote: A17 established three
distinguishable block reasons — not implemented anywhere, not permitted
by this node, not declared/unknown — and collapsing them into
"unavailable" would discard the distinction the platform pays to
maintain, making a fleet mid-upgrade indistinguishable from one that
cannot do the thing at all.

**A28.5 — Sequencing (amendment).** **A6-4 is the next production
implementation slice; PX implementation follows it.** A6-4 establishes
the External Agent API Plane boundary, and new human projections must not
widen machine reach while that boundary is unresolved — a machine
principal currently reaches a large share of the declared routes because
`fleet.view` is one broad key (A27.12). PX architecture is ratified by
this amendment; **no PX production implementation runs concurrently with
A6-4.**

**A28.6 — Ratified principles.** P1 the screen never decides — every
governance answer is server-composed and rendered verbatim, because a
page that reaches its own verdict can show an operator something the
enforcement path will not honour. P2 everything an operator can see, they
can link to. P3 UNKNOWN is a first-class rendered state, never a blank or
an optimistic default (A17.4, A19.9, A27.4 at the data layer; the same
rule at the presentation layer). P4 a refusal surfaces the server's own
reason and names the blocking condition. P5 plumbing is not product. P6
one case, many lenses. P7 freshness is content, and silence never renders
as health.

**A28.7 — Canonical terminology.** "Agent" is never a bare noun in
customer-facing text. The three actors are **Harken Node** (L1, the
per-device agent, final execution authority), **Operational Agent** (the
governed, scoped, budgeted actor a tenant configures) and **External
Agent Runtime** (a third-party runtime holding a machine identity,
submitting by reference). **Device** is the physical hardware; a Harken
Node is HarkenIQ running on it, and the two are not interchangeable. The
customer-facing planes are **HarkenIQ Central Command** and **HarkenIQ
Site Manager**. No code is renamed by this amendment; URLs move once,
with redirects, in PX2.

**A28.8 — Information architecture.** Six tenant groups: OPERATE
(Attention, Incidents, Approvals, Campaigns) · INFRASTRUCTURE (Sites &
Fleet, Harken Nodes, Capabilities) · AGENTS (Operational Agents, Agent
Activity) · INTELLIGENCE (Outcomes & Reliability, Learning, Predictive
Risk) · GOVERNANCE (Autonomy, Policies, Organization, Access, Audit) ·
ADMINISTRATION (Users, Subscription & Usage, Downloads, Support,
Marketplace). Autonomy sits in GOVERNANCE and not INTELLIGENCE, because
S5 built `/api/autonomy` as a decision boundary and filing it as an
insight would tell an operator it is a report. The platform plane keeps
its own IA and never adopts tenant group names. Six groups / five items
is a **strong design constraint, not a constitutional invariant**
(D-IA3); a proven product need may change it through normal ratification.

**A28.9 — Attention is the tenant operational home (amendment).**
`/t/{tenant}` opens Attention, not a generic Overview — it is the only
surface that answers "what needs me now", it is already ranked
server-side, and it is the read every actor in the platform holds. It is
NOT today's Risk & Exposure page renamed. It composes existing governed
lenses: operational posture, ranked attention, active incidents, pending
approvals, active campaign/execution state, stale infrastructure, recent
consequential outcomes. It creates **no second aggregation authority**;
every summary comes from a canonical server contract. There is no
separate Overview dashboard.

**A28.10 — D-IA4: every Attention lens must prove its contract.** The
same architectural rule A28.13 places on the case view. For each named
lens PX2 documents (1) the canonical backing contract, (2) its
authorization and scope treatment, (3) its freshness semantics, (4) its
empty and unknown semantics. **A lens with no canonical authoritative
contract is DEFERRED, not invented.** Specifically: "fleet health" and
"active automation" are not yet shown to resolve to a single canonical
read, and neither may be manufactured to fill a dashboard card. The UI
may COMPOSE separately authoritative lenses visually; it may NOT
independently derive a new governance or health conclusion.

**A28.11 — The persistent operational context bar.** The shell states, at
all times: tenant · effective scope · enforcement posture · safety state ·
freshness. Every one is an existing server contract (`ResolvedScope` via
`/api/me`, `cc_tenant_settings.enforcement`, `FleetSafetyState`, the stop
switch). This is not optional chrome: under strict enforcement a
site-scoped operator sees a genuinely different estate from a tenant
owner, and an operator who does not know their own reach will read an
empty list as "nothing is wrong". A tenant stop switch denies every
action class and must be visible from every screen, not only from the
Autonomy page.

**A28.12 — Design-system architecture.** Four layers: L0 accessible
primitives (headless behaviour only, third-party permitted) → L1
HarkenIQ primitives (already adopted at 28–35 of 37 pages) → L2 HarkenIQ
**domain** components (missing entirely, which is why the 519 inline
styles exist) → L3 product experiences (composition only). **No
third-party framework owns HarkenIQ's product language above L0.** The
token set is completed before anything is restyled. Styling is statically
extracted; no runtime CSS-in-JS. One semantic status module owns domain
status → severity for every platform enumeration, and Site Manager
imports the same module. `console-ui/src/proposalProvenance.ts` (A27.14)
is the reference shape for every L2 component: a closed vocabulary, the
server's own reading rule applied client-side so the two cannot disagree,
unrecognised values collapsing to a first-class unknown, and a projection
returning exactly what may pass.

**A28.13 — The Governed Operation view is a read projection, and its
endpoint is conditional.** D-AX2 is ratified: the case view is a
projection over canonical objects, never an authority, workflow engine,
state machine or persistence model; a stage with no data renders as *not
reached*, which is a real state. D-AX3 — a human projection endpoint —
is **CONDITIONAL** pending PX5A. The earlier characterisation that it
would cost "almost no backend" was wrong and is withdrawn:
`build_receipt()` supplies a lifecycle skeleton, not a canonical human
case identity. Before any endpoint is approved, PX5A must prove from the
repository: canonical operation identity; whether one stable identifier
spans incident, proposal, approval, execution and outcome; cross-object
correlation; incident-to-proposal linkage; campaign/device relationships;
tenant and scope authorization; bounded evidence; human-versus-machine
projection separation; and stable deep-link semantics. **No case table or
state machine is created merely because the UI would like one.**

**A28.14 — Governance is presented as eight distinct concepts.**
Capability ≠ Permission ≠ Scope ≠ Autonomy ≠ Approval ≠ Execution ≠
Outcome ≠ Learning, never merged into a single allowed/blocked indicator
anywhere in the product. When an action cannot proceed, the UI names
WHICH of the eight refused it. Governance reads as posture and changes as
ceremony (the A26/A13 read-write split, expressed visually). Every
governance object shows its own recovery path, because A23-3 already
built the recovery endpoints.

**A28.15 — Operational Agent experience.** The A19 activation ceremony —
CREATE → CONFIGURE → PREFLIGHT → ACKNOWLEDGE → APPROVAL → ACTIVATE → RUN
→ OBSERVE — is the server's, mirrored by the page and independently
refused by the server in its own words. An External Agent Runtime is a
distinct visual class: it holds a credential, submits by reference, is
metered and can be throttled, none of which is true of the CC-resident
evaluator. The 1,820-line agent page becomes six addressable views —
identity, reach, capability, governance, readiness, activity — over the
same contracts.

**A28.16 — Site Manager positioning.** It remains **HarkenIQ Site
Manager — Local Site Operations** and never becomes a second Central
Command: no tenant switching, no RBAC surface, no autonomy
configuration, no campaign orchestration, no cross-site anything. It
shares HarkenIQ tokens, semantic status vocabulary and language; it does
not necessarily share Central Command's layout, and its single-column
density correctly signals a smaller scope. It gains durable URL routing
(D-SM4). **Connectivity language is evidence-based (D-SM3, conditional):**
absent an authoritative connectivity or heartbeat contract it must not
claim *connected*, *online* or *offline*, and may state only factual
observations — last successful Central Command exchange, last
synchronization attempt, pending uploads, locally available actions, last
directive received. This is A27.10's standard applied to a second surface.

**A28.17 — Headless surfaces are a recorded decision, not an omission.**
Machine proposal submission, token exchange, machine receipts, ingress
accounting internals, outcome-correlation internals, SM→Node dispatch
plumbing and any future MCP transport stay headless; the human sees
decisions, exceptions, risk, progress and outcomes. Every capability with
no human surface is classified either *headless by design*, with its
reason, or *UI debt*, with an owning slice. `MACHINE_ONLY_ROUTES` is the
existing mechanism and its assertion in both directions is the model.

**A28.18 — Accessibility is a per-slice Definition of Done.** WCAG 2.2 AA
is the HarkenIQ product standard. Every new or migrated primitive, page
or surface satisfies its applicable accessibility requirements **when it
is introduced**. PX8 is the formal audit and VPAT-evidence stage; it is
**not** the first remediation stage. Non-negotiables: accessible names on
every control; labels associated with controls; focus visible, trapped in
overlays and restored on close; async results announced through a live
region; status encoded in shape or text as well as colour; 4.5:1 body and
3:1 large-text and UI-boundary contrast verified in both themes;
`prefers-reduced-motion` respected.

**A28.19 — Responsive scope, and mobile keeps full governance context.**
Three widths: ≥1280px the design target; 768–1279px fully usable; <768px
a deliberate subset — Attention, incident detail, approval and denial.
**D-AC3 (LOCK):** mobile approval retains provenance, action, target,
evidence, effective scope, policy, approval state, blast radius,
reversibility, consequence and required confirmation. **No
swipe-to-approve. No reduced governance path.** Mobile is a responsive
presentation over the same backend authority and the same ceremony as
desktop.

**A28.20 — D-B4: HarkenIQ's visual personality (LOCK).** HarkenIQ
communicates operational precision, authority, safety, explainability and
calm. It must remain readable at enterprise operational density for
prolonged use — an operator should be able to work in it for eight hours.
It must NOT adopt consumer-AI visual decoration as its primary product
language. **Brand colour never competes with semantic severity:** when
something is wrong, severity dominates. Whitespace clarifies hierarchy
rather than reducing information density, and animation communicates
state transition rather than decorating.

**A28.21 — D-B5: brand foundation lands early (LOCK).** Established at
PX1B, before the shell and components are designed: canonical HarkenIQ
spelling and capitalization; one mark concept and initial mark
constraints; an owned accent-family direction (the assessed accent
`#632ca6` is a competitor's brand colour and is not defensible for a
vendor-neutral platform); semantic-colour independence; typography
principles; mono typography principles for IDs, SHAs, IPs, service tags
and hashes, which are load-bearing content in this product; density
principles; border, radius and elevation principles; icon-family
direction; plane naming and lockup constraints. Final SVG artwork,
completed lockups, full typography rollout, dark mode and formal brand
documentation remain PX8. Deferring all of it to PX8 would have PX2–PX7
designed against temporary assumptions and restyled twice.

**A28.22 — D-DS6: a domain component requires a real consumer (LOCK).**
No detached domain-component catalogue. A new L2 component enters
production only together with at least one real product workflow that
consumes it. This is what keeps the design system a product language
rather than a showroom.

**A28.23 — Migration. D-M1 stands; D-M2 is new.** D-M1 is unchanged: **no
rewrite, migrate in place** — the 37 pages encode years of hard-won
correctness, including defects found and fixed by the slices that built
them, and a rewrite discards that to gain consistency. D-M2 replaces the
absolute earlier phrasing: **do not combine UNRELATED workflow behaviour
and broad visual restyling in one PR**; accessibility, responsive
behaviour and semantic interaction may legitimately change both
appearance and behaviour when they are intrinsically part of the same
focused correction. Tokens precede components; components precede pages.
New code uses the new layer from day one, enforced by lint rather than
review memory. Every PX slice asserts that no second RBAC, scope
resolver, capability authority, approval path, execution engine or
identity model was introduced — PX is uniquely able to create a second
governance universe by accident, because a UI that computes an answer IS
a second authority.

**A28.24 — Quality gates.** Console Vitest becomes mandatory in CI as
PX1A's first commit: the `ui-build` job runs only `npm ci && npm run
build`, and `vitest` appears nowhere in CI, `scripts/` or any git hook,
so the tests protecting A27.14's provenance rule pass only when run by
hand. Later gates: token lint (no raw hex or px outside the token file);
a contract-render test per L2 component; `jest-axe` on every primitive
and page shell; a route-contract test that every listed object is
addressable; a terminology lint banning bare "Agent" in user-facing
strings; visual regression on the primitive gallery; and a compose-gate
step proving the human and machine projections never describe one
operation differently. The tests that have caught the most in this
codebase are structural, and the UI equivalents are what will keep PX
honest after PX ships.

**A28.25 — The ratified program.** A6-4 (External Agent API Plane) ·
PX1A (engineering and semantic foundation) · PX1B (brand and visual
foundation) · PX2 (shell, IA, Attention operational home, context bar,
responsive foundation) · PX3A (accessible interaction primitives) · PX3B
(Attention/Incident/Approval domain components) · PX3C
(Execution/Campaign/Outcome domain components) · PX3D
(Governance/Scope/Capability/Learning domain components) · PX4A (stable
object URLs and query-string collection state) · PX4B (cross-surface and
audit links) · PX5A (Governed Operation correlation and identity
contract) · PX5B (human Governed Operation projection and experience) ·
PX6 (Governance and Operational Agent recomposition) · PX7 (Site Manager
alignment) · PX8 (brand completion, dark mode, formal accessibility and
VPAT evidence). **This is sequencing discipline, not a reduction of
HarkenIQ's end-state product.**

**A28.26 — Disposition of the 44 assessed decisions.** RATIFIED (36):
P1–P7, D-T1, D-T2, D-T3, D-IA1, D-IA2, D-SH1, D-SH2, D-SH3, D-DS1,
D-DS2, D-DS3, D-DS4, D-AX1, D-AX2, D-GX1, D-GX2, D-GX3, D-OA1, D-OA2,
D-OA3, D-SM1, D-SM2, D-SM4, D-HS1, D-AC1, D-AC2, D-QG1, D-QG2, D-M1.
RATIFIED AND RE-SEQUENCED (3): D-B1, D-B2, D-B3 — foundation at PX1B,
completion at PX8. RATIFIED AND DOWNGRADED (1): D-IA3 — a strong design
constraint, not an invariant. CONDITIONAL (2): D-AX3 pending PX5A;
D-SM3 pending an authoritative connectivity contract. DEFERRED (2):
D-DS5 dark mode, until the token set is complete; D-HS2 warranty —
accepted as identified UI debt with **no owning slice assigned**, pending
validation of persona, workflow and backend maturity. REJECTED: none.
NEW THIS AMENDMENT (6): D-IA4, D-B4, D-B5, D-AC3, D-DS6, D-M2.

**A28.27 — What this amendment does not do.** It changes no production
code, no permission, no route, no dependency and no backend authority. It
starts no A6-4 work. The A6-4 boundary is produced as a proposal for
separate ratification after this amendment is merged and main-verified;
no A6-4 branch, spec amendment or implementation exists or is authorised
by this amendment.

### A29 — 2026-09-07 — A6-4: the deliberate External Agent API Plane (decided: Vinod)

A6-1 gave an external runtime a governed way to write, A6-2 a governed
way to read, A6-3 the human's ability to supervise both. None of them
asked the prior question: **which of Central Command's routes an external
runtime should be able to reach at all.** Nobody ever decided. Ratified
with amendments after review; recorded BEFORE the code, per change
control.

**A29.1 — The gap, measured by executing the contract on `8ab45c5`.** A
machine principal reaches **46 of 98 declared routes**, not because a
product decision admitted them but because their required permission
happens to be a member of `MACHINE_PRINCIPAL_CEILING`. `fleet.view` alone
opens 43. Exactly **one** is a write — the governed
`POST /api/operational-agents/{agent_id}/proposals` — so the write
boundary is already correct and A6-4 does not reopen it; this is a
read-surface correction. Of the 46, ten are agent-self reads already
object-gated, and **23 are estate, fleet-intelligence and
governance-posture reads with no stated machine job**: campaigns, learning,
outcomes, predictive risk, warranty, firmware exposure, approval-policy
topology, autonomy budgets, tenant enforcement settings and its impact
census, sites, and Harken Node inventory.

**A29.2 — The declaration already exists and is discarded.**
`operational_agent.READ_CAPABILITIES` declares five machine read jobs and
`INGRESS_CAPABILITIES` a sixth, each naming its own route family. At
`auth.py`, `machine_permissions()` converts those bindings into coarse
permissions and **the bindings themselves are never carried onto the
`UserContext`**, so the route guard sees only `fleet.view`. An agent bound
to `attention` alone — the floor, since `REQUIRED_READS` is
`("attention", "autonomy")` — thereby reaches the whole estate. The house
pattern again: declared, and not consulted where the decision is made.
A6-4A's enforcement is carrying an existing declaration to the guard, not
designing a machine policy model.

**A29.3 — B1: default deny.** No named machine job means no machine
reach. A route becomes machine-reachable by being declared against a job
and by nothing else — not by a permission entering the ceiling, not by a
binding being added, not by a handler forgetting to check.

**A29.4 — B3: route-surface eligibility is a product/API contract, not
authorization.** It answers exactly one question: *may this principal
species use this product surface?* It grants no permission, scope,
autonomy, approval or execution, and it replaces nothing. The
authorization chain runs unchanged after it: identity → canonical
permission vocabulary → canonical scope resolver → machine ceiling →
explicit A0 binding → object/self gate → handler. **No second RBAC, no
second resolver, no second machine policy engine, no route ACL
configuration, no duplicated authorization table.**

**A29.5 — Ratified surfaces, and INTERNAL deferred.** `HUMAN`, `MACHINE`
and `BOTH` are ratified for A6-4A. `INTERNAL` is **deferred on evidence**:
`ROUTE_CONTRACT` contains zero routes matching `internal`, and Central
Command has no independently authenticated internal or service principal
route class — the CC↔Console channel is CC acting as a *client* against
Console, not a Central Command route. Inventing a service-to-service
identity taxonomy merely to complete an enum is refused. `INTERNAL`
returns when a concrete route needs it.

**A29.6 — B4, and the typed job model.** Bindings become load-bearing at
the guard: `bound_reads` and `bound_ingress` are carried onto the
`UserContext` and checked against the route's declared job. **Human
description strings are never load-bearing** — the job is an explicit
typed identifier, and the vocabulary is the A0 binding vocabulary itself
so there is no second naming system: `attention`, `incidents`,
`proposals`, plus `self`. `self` is deliberately not a binding: it names
the agent's own record, which is gated by identity through the existing
A25.5 helper rather than by configuration, so an agent can always read
itself and can never read another. A test asserts that every job other
than `self` is a real A0 binding name.

**A29.7 — B2: broad fleet and tenant-learning APIs are not part of the
plane.** Direct machine reach is removed from generic `/api/fleet/*`
browsing and generic `/api/learning/*` tenant-learning state. The runtime
receives the device and evidence context it needs to reason **through the
governed candidate and attention projections**, not through estate
browsing. Future capability-intent or target-discovery work may add a
deliberate, machine-safe target discovery contract if proven necessary.
**A6-4A does not delete the `fleet` and `learning` binding persistence or
configuration vocabulary** — configured bindings are preserved and
reported for compatibility and migration evidence. This slice narrows API
reach; unrelated binding-schema cleanup needs its own decision.

**A29.8 — B8: governance conclusions, never governance construction.** A26
kept the posture reads at `fleet.view` on the ratified ground that
*posture belongs to the people living under it* — reasoning about
**people**, into which the machine ceiling swept machine principals as a
side effect. Machine direct reach is therefore removed from approval
policy topology and rules, autonomy budget configuration, the tenant
scope-enforcement settings route, the scope-enforcement impact census, and
approval group and principal topology. A6-4B may later supply narrowly
allow-listed **conclusions** — autonomy disposition, approval required,
blocking reason, stop or halt conclusion, effective enforcement posture —
and never the configuration that produced them. This is the line A25.3
drew for approver identity, applied to policy topology.

**A29.9 — B7: a machine reads its effective reach, never its
construction.** An agent that cannot see what it may address cannot avoid
proposing outside it. The projection carries reach and enforcement
posture; it withholds per-grant type, ref and permission subset, inert
grant targets and reasons, `previously_granted`, `synthesis`,
`administered` and `contextual_unit_ids`. A23 and A26 fail-closed
lifecycle semantics are preserved exactly: a revoked, expired or
vanished-target grant reaches nothing and the projection reports reduced
reach rather than concealing it. **The replacement projection is A6-4B**;
in A6-4A `/api/scope-grants/me` becomes `HUMAN`.

**A29.10 — B5/B6: discovery, and no vocabulary change.** Discovery
separates ADDRESSABLE from AUTHORIZED TO EXECUTE and authors **no second
catalogue**, composing from `load_capability_registry`,
`capabilities.parameter_contract`, `catalogue_view`, `ACTION_RISK` and
`ACTION_REVERSIBILITY`. It grants no permission, scope, autonomy,
approval or execution. **A6-4 adds no permission and does not move the
ceiling.** `capability.discover` was considered and refused: `fleet.view`
is honest for discovery, the defect was surface breadth rather than a
missing authority, and minting `machine.*` duplicates would be the
parallel machine RBAC A29.4 forbids. The vocabulary stays at 25;
`MACHINE_PRINCIPAL_CEILING` stays
`{fleet.view, incident.view, proposal.submit}`.

**A29.11 — B9: A6-4A then A6-4B.** A6-4A is a **pure narrowing** —
reviewable as one question, *can a machine still reach anything it was
not declared for* — and adds no endpoint and no projection, so nothing new
can leak. A6-4B is **additive**, and every new projection is a potential
leak deserving its own A25.9 sweep rather than sharing a review with a
narrowing. A6-4A first: narrow the surface, then design the deliberate
contract inside the narrowed boundary. **A6-4B receives its own boundary
and review after A6-4A is merged and main-verified.**

**A29.12 — No route stays broadly machine-readable to cover a temporary
gap.** Verified rather than assumed: the CC-resident evaluator composes
in-process (`load_attention`, `load_autonomy_contract`) and never over
HTTP, and the compose gate's machine token drives only agent-self reads,
`dry-run`, `ingress` and the submit — all of which A6-4A keeps. Where a
REPLACE route cannot remain without violating default-deny, its existing
human endpoint is classified `HUMAN` and the machine capability is
deferred to A6-4B. `/api/autonomy/` and `/api/capabilities/*` are the two
that take a temporary capability gap, recorded here rather than papered
over.

**A29.13 — The permanent structural invariant.** A non-vacuous test proves
that a permission entering `MACHINE_PRINCIPAL_CEILING` opens **zero** new
routes unless both the route is declared `MACHINE`/`BOTH` and the
authenticated agent carries the required job binding. Also proved: every
machine-reachable route is declared; every machine-reachable route names a
job; every declared machine route matches runtime behaviour in both
directions; no undeclared route is machine-reachable; a persona holding
one job reaches exactly that job's declared set; and the ceiling and
canonical permission vocabulary are unchanged. The before/after census is
frozen and reported. **Baseline: 46 of 98.** The completion report states
the exact post-enforcement count and list.

**A29.14 — A6-4A scope.** IN: route-surface declaration; typed machine-job
declaration; `bound_reads`/`bound_ingress` on `UserContext`; canonical
guard enforcement; default-deny; KEEP/REMOVE applied; broad fleet and
learning machine reach removed; governance-construction routes removed
from the machine surface; the proposal submit path unchanged; audited and
metered authenticated refusals; REPORT → ASSERT → ENFORCE; the
before/after matrix; the structural tests; a one-job-at-a-time
authenticated machine persona sweep; and migration reporting for
configured bindings that lose direct route reach. OUT: the discovery
endpoint; the self-scope, autonomy, attention and incident replacement
projections; the `actor.species` accuracy fix where it needs an additive
projection change; A6-4B; events, webhooks, MCP, SDKs; capability-intent
or evidence ingress; new permissions; ceiling changes; PX work; and any
redesign of the proposal write path.

**A29.15 — Compatibility, stated plainly.** There is no GA external-agent
contract: A6-1 shipped 2026-09-05 and no `/api/v1` namespace or published
external API version exists. The before/after report is produced anyway,
because removing 23 routes from a machine surface deserves one. Refusal
shape follows existing convention — 404 where confirming the object would
itself leak, 403 for a species refusal on a route whose existence is not
sensitive, matching how `/api/operational-agents/catalogue` already
answers a machine.

### A30 — 2026-09-08 — A6-4B: canonical reach before published reach (decided: Vinod)

A6-4A narrowed which routes an external runtime may reach. A6-4B was
scoped as the additive half: discovery, and the machine projections
A29.11 separated out. The pre-implementation inspection found that the
platform cannot yet publish reach truthfully, because it does not have
one answer to give. Ratified after independent reproduction from merged
`main` at `6f822f7`; recorded BEFORE the code, per change control.

**A30.1 — Six findings, reproduced by execution, with corrected
severity.** Every finding below was reproduced from merged `main`, not
accepted from a prior report.

* **F1 — HIGH, fail-CLOSED.** `ResolvedScope.site_ids` is built from
  SITE grants, org-unit-expanded sites and tenant-wide reach; `device`
  and `device_class` grants contribute nothing to it **by
  construction**. `scope_sites()` builds the entire repository read
  filter from that one field, and an empty set yields `sa_false()`. A
  device-scoped or device_class-scoped principal therefore reads ZERO
  rows from all fourteen `apply_scope` sites, from `_audit_scoped` and
  from `narrow_to_sites()`, while `covers_device()`, `permits()` and
  `resolve_scope()` all say those devices are in scope. The reported
  severity was fail-open; it is **fail-closed**, and the correction is
  recorded because the direction of a defect decides its priority. It is
  HIGH for one reason: `/api/scope-grants/me` already publishes
  `device_ids` and `device_classes` as authority fields, so the platform
  **already tells a principal it reaches devices it cannot read**. Both
  principal types are affected: `scope_grants.py` validates against all
  five scope types for `user` and `agent` alike.
* **F2 — BLOCKER, fail-OPEN.** `OperationalAgentRepo.list_scopes()`
  filters `revoked_at` and **not** `expires_at`. An EXPIRED grant of type
  `site`, `device` or `device_class` still yields devices to
  `resolve_scope()`. Revoked is correctly closed; expired is not.
* **F3 — HIGH.** `enforce_route_surface` charges only on refusal; a
  served machine read is metered by its handler, and
  `_charge_machine_read` is called only from handlers inside
  `api/operational_agents.py`. `GET /api/attention/`,
  `GET /api/incidents/` and `GET /api/incidents/{id}` joined the machine
  plane in A6-4A from two other routers and are served unmetered,
  unbounded and unattributable. The A25 completeness guard filters
  `path.startswith(PREFIX)` and is complete for one router only.
* **F4 — HIGH, one sub-finding newly reproduced.** The incident contract
  is already the most machine-aware payload in the codebase and neither
  payload carries operator identity, so the reported identity hazard does
  not exist. What does: `correlation` serializes `correlation_meta`
  verbatim as an open dict, and on `GET /api/incidents/{id}`
  `recommended_next.summary` is assigned **raw generated
  `suggested_action` text, promoted out of the `generated` block and out
  from under its `trust` marker**, into an imperative-named field. For a
  model consumer that is the highest-risk field in either payload.
* **F5 — PRODUCT DECISION.** `REQUIRED_READS` injects an `autonomy`
  binding at creation and no route declares an `autonomy` job after
  A6-4A, so every agent is issued a mandatory binding that reaches
  nothing and the catalogue reports it `"required": true`. `learning` and
  `fleet` are unreachable **by ratified decision** (A29.7) and are not a
  defect; `autonomy` is the only mandatory one, and A29.12 deferred its
  surface rather than withdrawing the promise.
* **F6 — MEDIUM, B1 prerequisite.** `actor_species` carries three
  undeclared values (`human`, `agent`, `campaign`) with no canonical
  constant. It is **projection-only** — it influences no decision in
  `build_autonomy` — and `/api/autonomy/` is HUMAN after A6-4A, so the
  hardcoded `"human"` is correct today. The reported framing of a live
  machine-facing defect is corrected: it becomes wrong only when B1
  publishes an autonomy conclusion to a machine.

**A30.2 — Two reach paths, opposite errors, cancelling by accident.**
The canonical path is lifecycle-correct and its `site_ids` projection is
type-incomplete. The raw path (`list_scopes()` → `resolve_scope()`) is
type-complete and lifecycle-blind. Neither is canonical alone, and
`agent_runtime.run_once` passes **both at once** — raw rows as `scopes`,
canonical sites as `resolved_site_ids` — so `resolve_scope` unions them
and the runtime compensates for F1 by importing F2. F1 and F2 are one
defect seen from two sides. `load_agent_scope`'s own docstring already
named half of it: *"It was latent only because all four call sites read
`.site_ids`."*

**A30.3 — The canonical owner.** `harkeniq_cc.scope`:
`resolve()` produces a `ResolvedScope`; `Grant.covers_site` /
`covers_org_unit` / `covers_device` / `covers_tenant` are the coverage
primitives; `ResolvedScope.permits()` is the only decision method. It is
the only place lifecycle, inertness, realm, subset, recorded-role ceiling
and synthesis are applied together. `ResolvedScope.site_ids` is a
**convenience projection, never a reach answer** — and this amendment
records that distinction because building a filter from it is what
produced F1.

**A30.4 — AD-1 RATIFIED: administrative grant-row access is not an
authorization source.** `list_scopes()` must cease being an operational
authorization or reach source, and F2 must NOT be solved by adding
`expires_at` filtering to a second reach path — that would leave two
paths and repeat the pattern. The two concerns are separated by name:

* **ADMINISTRATIVE GRANT ROW ACCESS** — the configured rows, including
  expired-but-unrevoked ones. A clearly named repository operation may
  retain it where lifecycle management requires it, `clear_scopes()`
  being the case that must keep working: rows that cannot be seen cannot
  be retired.
* **EFFECTIVE AUTHORIZATION REACH** — what a principal may operate on
  now. Operational runtime, read and proposal reach resolve through the
  canonical loader and resolver.

**There must be one operational reach answer.**

**A30.5 — PD-1 RATIFIED, option (a) with an explicit constraint:
context is not authority.** A device-scoped or device_class-scoped
principal may receive the MINIMAL containing-site context required to
understand and navigate the objects it canonically covers. The
invariant, recorded verbatim:

> Object authority may expose bounded ancestor context for
> comprehension; ancestor context never implies ancestor authority.

Therefore: `covers_site()` MUST remain false for a device-only or
device_class-only grant; contextual site visibility MUST NOT grant access
to sibling devices, site-wide actions, site administration, site
policy or governance topology, the complete site audit stream, or any
widening of approvals or mutations. `/api/sites` may expose only the
containing sites, as explicitly contextual read-only projections. Audit
visibility remains object-authority-derived; the site is context only.
The distinction is recorded here and **structurally tested**, not left to
review.

**A30.6 — PD-2 RATIFIED, option (a): publish the conclusion, not the
topology.** The machine autonomy **governance conclusion** is published
and `autonomy` is NOT withdrawn from `REQUIRED_READS`: a runtime must be
able to understand the governance conclusion under which it operates.
The projection exposes the conclusion only. It MUST NOT expose approver
identities, approval group membership or topology, role assignments,
internal governance topology, or protected policy internals. **No new
authority results from reading the conclusion.** This is A29.8/B8 applied
to the one job A6-4A deferred, and it is consistent with A26: posture a
principal lives under is readable; the people and structures that decide
it are not.

**A30.7 — PD-3 RATIFIED: withhold `correlation_meta`.** It is not
published to machine consumers until HarkenIQ has a bounded canonical
contract for it. F4's newly reproduced trust-boundary finding is accepted
with it: `recommended_next.summary` MUST NOT promote raw generated
`suggested_action` text outside its generated/untrusted trust envelope.
Both are addressed in B2 and nowhere earlier.

**A30.8 — PD-4 RATIFIED: A6-4B0a ships alone.** F2 is a live fail-open
authorization-integrity defect and outranks the feature sequence. A
security correction is not bundled into a slice that also changes human
reads, and it is not held behind a product decision it does not need:
expired means expired is already this platform's ratified rule
(`is_active`, A23-3), so B0a requires no ratification that B0b does.

**A30.9 — The five-slice sequence, LOCKED.** The corrected program
replaces the three-slice sketch. Each slice merges and main-verifies
before the next begins; **the slices are not combined.**

| Slice | Name | Direction |
|---|---|---|
| A6-4B0a | Grant lifecycle integrity | corrects an over-reach (narrows) |
| A6-4B0b | Canonical reach convergence | corrects an under-reach |
| A6-4B0c | Machine read metering completion | narrows |
| A6-4B1 | Governed discovery | additive |
| A6-4B2 | Operational context projection | additive |

**A30.10 — A6-4B0a scope.** Objective: **expired means expired
everywhere operational reach is evaluated.** IN: eliminate
`list_scopes()` as an operational reach source; preserve a clearly
separated administrative raw-row read where lifecycle management such as
`clear_scopes()` requires it; `resolve_scope()` must not accept inactive
grant rows as effective reach; the evaluator, dry-run, runtime,
agent-view and preflight consume canonical lifecycle-correct reach;
structural proof that no operational reach path consumes unfiltered
grants; a lifecycle regression matrix over active, revoked, expired,
future-expiring, inert, vanished-target where applicable and other-realm;
proof `clear_scopes` still retires expired-but-unrevoked grants; a real
PostgreSQL expiry proof; a live Keycloak and PostgreSQL proof; proof an
expired agent stops proposing; proof no stale candidate or proposal path
reintroduces expired reach; preserved audit integrity; full regression
and compose gate. OUT: F1 and the device/device_class projection
correction; ancestor contextual visibility; F3 metering; F4 payload
changes; F5 autonomy discovery; F6 species work; discovery;
attention/incident projection changes; any new endpoint, permission,
ceiling change, schema or migration; and any B0b/B0c/B1/B2 code.

**B0a is a security correction, not product capability work.** If
removing `list_scopes()` as a reach source exposes F1 more visibly, that
is expected and it is NOT compensated for with raw grant rows. B0b
exists to correct that under-reach properly.

**A30.11 — A6-4B0b scope.** IN: the repository read filter derives from
GRANTS rather than from `site_ids` alone — site ∪ org-expanded ∪
`device_ids` ∪ `device_classes`, per table, on columns that already
exist; `_audit_scoped` and `narrow_to_sites` converge on the same
construction; A30.5's context-is-not-authority invariant implemented and
structurally tested; a regression proving every existing human persona
reads byte-identically, so only device and device_class principals
change; a test that fails if a new reader builds a filter from
`.site_ids`. OUT: F2; metering; discovery; any ceiling, permission or
route change. This slice **changes human reads** and says so.

**A30.12 — A6-4B0c scope.** IN: served-read metering on the three
off-router machine routes; the completeness guard re-anchored from one
router prefix onto `MACHINE_SURFACE` itself, so it fails when any future
machine route lacks a decided meter answer. OUT: payload changes; new
limits for human callers. Compatibility: a correctly-bound runtime can
receive 429 for the first time, so the window is set against real gate
traffic before it lands.

**A30.13 — A6-4B1 scope, and the eight facts.** IN: capability discovery
composing `load_capability_registry`, `capabilities.parameter_contract`,
`catalogue_view`, `ACTION_RISK` and `ACTION_REVERSIBILITY`; the canonical
A5 parameter projection including `unavailable`; the self-scope
projection (A29.9/B7 — reach only, never grant construction); the
autonomy governance conclusion per A30.6; the species declaration (F6).
OUT: attention and incident projections; any ceiling change; `learning`
and `fleet` route reach, since A29.7 stands.

**Discovery informs; it does not authorize.** Eight facts remain eight
separate fields and are never collapsed into a synthetic `allowed`:

    CAPABILITY EXISTS
    IMPLEMENTED
    REACHABLE
    BOUND
    IN EFFECTIVE SCOPE
    GOVERNANCE CONCLUSION
    APPROVAL REQUIRED
    CURRENTLY OPERABLE

No second capability catalogue and no second scope resolver, per A17 and
E1.2 respectively.

**A30.14 — A6-4B2 scope, and trust semantics.** IN: allow-list
projections for attention and incidents, built by naming what may pass
and never by serializing then stripping (A25.9); bounded evidence;
`correlation_meta` withheld per A30.7 until it has a contract;
`recommended_next.summary` corrected so generated text never leaves its
trust envelope. **Generated and evidence trust semantics survive machine
projection**: `origin` and `trust` are mandatory on any projected
diagnosis, generated text stays inside the block its trust marker
governs, and a consumer must be able to tell platform-produced references
from model-produced text without inspecting the values. OUT: raw human
DTO reuse; new permission; ceiling change; human payload changes beyond
additive provenance.

**A30.15 — No permission change, no ceiling change, no migration in
B0.** B0a, B0b and B0c introduce no new permission; the canonical
vocabulary stays at 25. `MACHINE_PRINCIPAL_CEILING` is **unchanged** at
`{fleet.view, incident.view, proposal.submit}` across the whole program;
changing it is a spec amendment, per A20.3. No new column, backfill or
schema change anywhere in B0 — CC head remains `0026` through B0c. B1 and
B2 are expected to need none either, and any that proves necessary is
declared before its code.

**A30.16 — What B0a changes that a human can see.** Reach that resolves
through an expired grant stops resolving, and an agent whose grants have
all expired becomes visible only to a tenant-wide reader — which is the
behaviour an agent with no rules already has, so no new lockout shape is
created and a tenant owner can always administer it. Refused reach is
reported rather than left silent, so an operator learns why an agent
stopped acting instead of watching it go quiet.

**A30.17 — Pre-merge remediation (independent review, HIGH): approval is
not perpetual execution authority.** Found by independent review of
`356c4c3` and reproduced by execution before any code changed. Central
Command had TWO places deciding whether an approved Operational Agent
proposal may cross CC → SM, and they asked different questions:

| gate | synchronous human approval | background runtime |
|---|---|---|
| current effective scope | **never asked** | asked, as `site_id ∈ site_ids` |
| capability still bound | **never asked** | asked (A24.15) |
| tenant stop switch | asked | **never asked** |
| identity | refuses `status ≠ active` | refused only `revoked` |

So on the synchronous path an ACTIVE, an EXPIRED and a REVOKED grant all
produced HTTP 200, an SM call and a dispatched directive; a site grant
that lapsed while another survived still carried the approved target; and
a dual approval begun under live authority and completed after expiry
dispatched. The background path refused those and dispatched past an
asserted stop switch. A slice whose objective is "expired means expired
everywhere operational reach is evaluated" cannot ship while a production
dispatch path executes after expiry, so this is B0a's to close; it does
not redesign admission, approval, autonomy, capabilities or execution.

Ruling, applied: ADMISSION decides whether work may enter governance;
APPROVAL decides whether approval requirements were satisfied; DISPATCH
decides whether the work is STILL AUTHORIZED NOW; the NODE decides whether
execution is finally safe. `agent_runtime.revalidate_dispatch` is the ONE
Central Command dispatch decision for an agent proposal and both paths
call it; it is the only caller of the fail-closed `dispatch_permitted`
algebra, whose `DISPATCH_GATES` gain `effective_scope` and
`capability_binding` so an input nobody evaluated refuses on either path.
Reach is asked through `load_agent_reach` — the answer admission used
(A30.4) — of the proposal's ACTUAL target (the device, at the site the
directive is addressed to, with its current class from the fleet), never
"does the agent have some scope". The per-agent unattended budget stays on
the autonomous branch (A19 D2). Each path keeps its existing refusal
semantics: synchronous → `failed` + `agent_proposal.refused_at_dispatch`;
background → left `approved` with `dispatch_reason` +
`agent_proposal.dispatch_withheld` (A22.12). No proposal or approval
record is rewritten and no decision is fabricated.

What is NOT claimed: the gate runs in CC's transaction immediately before
the SM call and takes no lock on grant rows, so a grant lapsing between
that read and the Site Manager queueing the directive is not caught in
CC. The Site Manager's lease, preconditions and blast radius and the
node's allow list remain the final authority, unchanged.

**A30.18 — A consequence recorded rather than hidden.** The background
gate judged reach by `site_ids`, the projection A30.3 says is never a
reach answer, so an approved proposal from a `device`- or
`device_class`-scoped agent was withheld at dispatch indefinitely although
admission admitted it and the synchronous path dispatched it. Converging
on the admission answer means such a proposal now dispatches on BOTH
paths while its target is covered, and on neither once it is not. This
is **not** F1: F1 is the repository read filter and the
`/api/scope-grants/me` projection; neither changed, `site_ids` is
unchanged, and the test asserting F1 open still passes. B0b still owns
it.

**A30.19 — The review's other findings.** MEDIUM: the evaluator expiry
proof was vacuous (no binding, no incident, so ACTIVE and EXPIRED both
yielded zero); replaced by one configuration shown proposing while live,
nothing once lapsed and again once renewed, and shown RED against the
pre-B0a lifecycle code. MEDIUM: the live proof is now repeatable — compose
gate steps A6-4B0a/AE–AH (active reach, expired reach on runtime/dry-run
with the configured-versus-effective statement, approval after expiry
reaching no Site Manager directive with history intact, renewal restoring
reach without resurrecting the refused work), and the existing A5/H source
scan follows the gate to its one home and now also asserts the
synchronous path consults it before the SM call. LOW: the structural
invariant covers the indirect path — a function feeding its own parameter
into `resolve_scope`/`evaluate` (`agent_view`) is a reach sink, so
`get_agent` now holds a configured COUNT and never the configured rows.
INFO (configured-scope annotation) deferred.

**A30.20 — Unchanged by the remediation.** No permission, no
`MACHINE_PRINCIPAL_CEILING` change, no route, no migration (CC head
`0026`). F1 open; B0b, B0c, B1 and B2 not started. Campaign wave dispatch
(A18) is not an agent proposal, carries its own stop-switch check and
plan-bound target set, and was not re-examined here. Observed on the live
stack and deliberately left unchanged: a machine whose every grant has
lapsed is refused its OWN agent and runtime records (404 — object
visibility follows the caller's effective scope, as A30.16 records),
while its dry-run is answered by the self rule alone (200, zero devices).
Both are "no reach"; aligning machine self-visibility is B1's self-scope
projection (A29.9/B7), not B0a's.

**A30.21 — Second independent re-review (of `d8d6c17`): the revocation
half of the live proof.** Verdict REMEDIATE, narrowly. The one dispatch
gate, target-specific revalidation and both dispatch lifecycles were
accepted as implemented, and A30.18's consequence (device and
device_class work dispatching) was confirmed as correct target-aware
authorization rather than accidental B0b work. The one open MEDIUM:
A30.19's repeatable live proof covered EXPIRY (AE–AH, the lapse set by
SQL because no route expires a grant in place), and nothing at the
production boundary revoked a SCOPE GRANT under approved work. A6/K does
revoke a machine identity, but it belongs to A6-1 and proves a 401 on
SUBMISSION — the dispatch-side identity gate has no live coverage, and
this slice does not add any. Compose gate steps A6-4B0a/AI–AL now do,
on both paths, with a fresh agent that owns its state: a real machine
identity, COLLECT_DIAGNOSTICS bound, and two site grants — G_T on the
target's site and G_O on another device-bearing site that is never
touched, so the agent keeps real reach and a refusal has to be about the
target. The sync half's second approver is created and granted there too,
for a reason the first full run found: A23-3 has two administrators revoke
each other CONCURRENTLY and asserts only that exactly one wins, restoring
the owner when the owner is the loser — so `gate-a23-admin2` ends that
race revoked about half the time, and borrowing it made the step pass on
a coin flip. A step's identities are part of the state it must own.
Synchronous: approval 1 of 2 is recorded while authorized, G_T is
revoked through `DELETE /api/scope-grants/{id}`, approval 2 completes →
refused on `effective_scope`, zero matching SM directives, both approvals
on the ledger. Background: the approval completes while authorized with
the site briefly unreachable (injected on Central Command's route to that
one site, timed after a fleet poll — the case the approval route leaves
`approved` for the background pass), G_T is
revoked, and the real operational-agent loop withholds the still-approved
proposal on `effective_scope` with zero matching SM directives; restoring
ONLY G_T then lets the same approved proposal dispatch — the causal
control. The outage is a direct write to `cc_sites.sm_endpoint`, the one
state change here not made through a production route, because none
exists to take a site offline; its window is bounded by the poll interval
read from the running service and the poller's own failure count is
compared across it, so a poll that landed fails the step by name instead
of surfacing later as an unattributed ERROR line.

Attribution follows `DISPATCH_GATES` order — the sixth gate's message
means the first five passed — with the binding, identity, activation,
pause, stop switch and the target's fleet membership asserted unchanged
on both sides of each refusal, and reach asserted still non-empty at
G_O's site so a refusal cannot be "this agent has no scope at all".
Emptied reach has three causes that share one message — revoked, expired,
or inert because the target vanished — so each refusal names which rather
than leaving a reader to chain inferences: the production grant read must
show G_T **revoked**, not expired, with its site still present, while G_O
stays effective.

The synchronous path has its positive control in the same steps, on the
same agent and target: AK's approval is taken while G_T is LIVE, and on
that path `revalidate_dispatch` runs before anything else and a refusal
marks the proposal `failed`, so P2 arriving at `approved` can only mean
the gate passed. AJ and that approval are therefore a matched pair
differing in one variable. One limit is stated rather than implied: the
live proof exercises `site`-type grants only, and `device`,
`device_class` and `org_unit` revocation stay covered by A30's unit tests
rather than on a stack. No production code changed.

**A30.22 — A6-4B0b-S1: multi-target authorization integrity (recorded
BEFORE the code).** B0b's Phase 1 analysis, reading every path from a
`ResolvedScope` to data visibility, found something outside its own
question: an authorization DECISION over a SET of devices evaluated
against ONE of them. `_decide_campaign_wave` passed
`wave.device_agent_ids[0]` — the lexicographically first device — into
`_record_and_evaluate`, which used it for the `permits("action.approve")`
scope gate, for `governing_policy()` (so `required_approvers` and the
bound group for the whole wave followed the FIRST device's class), and
for the ledger's `authority_snapshot.target_device_agent_id`. A principal
holding a `device` grant on A therefore approved a wave over [A, B, C],
and the E0.1 evidence named A. Live today; B0b would have made it
DISCOVERABLE by making device- and class-scoped principals real, which is
why S1 lands first and alone. A repository-wide inventory (approvals,
campaigns, dispatch, revalidation, preflight, skill delivery, SM
planning/dispatch/firmware, node playbooks — 89 classified paths) found
the same family in three more places and nothing else: the campaign
runner dispatches an APPROVED wave re-checking stop switch, status, plan
hash and capability and never the approvers' CURRENT authority over the
set (the A30.17 shape, one origin later); skill delivery at activation
fans out over the device list the PREFLIGHT stored, with no
re-resolution against current reach; and the wave's subject digest is
computed once at `build_waves` and used only as a lookup key, so an
out-of-band edit to `device_agent_ids` passed every existing check. Every
other multi-device path is single-target by construction or an all-rule
ceiling; the Site Manager evaluates only inputs it owns.

**Locked rule.** For any authorization decision over an immutable
authoritative target set, AUTHORIZED means EVERY required target is
covered by the decider's CURRENT canonical effective scope — never "any",
never the first, never a representative, never the site as a proxy where
target-specific authority is required, never a visible subset. One
uncovered target refuses the whole set. The set is never narrowed,
filtered or recomputed to fit the caller: capability may narrow what
executes (A18 D2, unchanged); authority never narrows what was approved.
Empty target set, duplicate ids, an id Central Command cannot identify at
the wave's site, and a stored wave whose recomputed digest no longer
equals its `subject_ref` all refuse.

**Three boundaries, one predicate.** At approval, every target is asked
through the canonical `permits("action.approve", site_id,
device_agent_id, device_class)` with the class read from the CURRENT
fleet row — so a `device_class` grant may satisfy a wave whose targets
ALL carry that class (ratified here: class-scoped approvers were
previously refused on every device-targeted subject because no caller
supplied the class), a class matching only some targets refuses, and an
empty class never matches a class grant (`Grant.covers_device`'s rule,
which is also A30.19's B0b-D-R3 answer for THIS path; `resolve_scope`'s
own default is B0b's to reconcile and is untouched). The governing policy
is resolved per distinct target class; distinct classes resolving to
DISTINCT policies refuse the approval with the reason named, because a
wave that two policies govern differently cannot be signed under one of
them — deterministic and fail-closed, with no multi-policy arithmetic
invented. The ledger records the full target list. At dispatch,
immediately before CC → SM and after the plan-hash check, a human-approved
wave re-verifies its digest and re-resolves each ledger approver's
CURRENT scope token-lessly through the ONE loader `governance.load_scope`,
with the role the approval recorded as the permission basis (the
grant-recorded role ceiling of A23-3 still applies inside `resolve()`);
E0.1's completion rule is re-run counting only approvers who still cover
every target, and if completion no longer holds the wave is WITHHELD —
its approval untouched (approval is historical truth), its targets marked
`revalidation = authority_lost`, audited once per distinct cause — and
dispatches on a later pass if authority returns. This is `revalidate_
dispatch`'s position for waves; it does not route through that function,
whose inputs are an agent's, and it takes no lock on grant rows — the
Site Manager's lease, preconditions and blast radius and the node's own
allow list remain the final execution authority. Autonomous waves have no
human authority to revalidate and are unchanged. At activation,
`install_bound_skills` re-resolves the agent's reach through
`load_agent_reach` and INTERSECTS the preflight's stored device list —
narrow-only, never adding a device the preflight did not report, skipping
with a reason.

**What is stated rather than implied.** A Keycloak realm-role demotion
after approval is not visible to a token-less resolution; a grant
revocation, expiry, subset narrowing or role-ceiling narrowing is.
Composition is refused: under `required_approvers = 2`, X covering [A]
and Y covering [B, C] do not add up to an approved [A, B, C] — each
approver must cover every target. **Unchanged:** the permission
vocabulary (25), `ROLE_PERMISSIONS`, `MACHINE_PRINCIPAL_CEILING`,
`MACHINE_SURFACE`, every route (zero added), the schema (CC head stays
`0026`), autonomy, proposal admission and dispatch, SM and node authority,
`scope_sites`/`apply_scope` (F1 stays open and is general B0b's), and the
`device_class` vocabulary `server | switch` — the ratified datacenter
taxonomy is not implemented here. B0b, B0c, B1 and B2 are not started.
**Device-identity widths (S2): NOT REQUIRED.** Every real node id is
`sha256(public_key)[:16]`; every operand S1 compares (`device_agent_ids`
JSONB, `cc_scope_grants.scope_ref` 128, `cc_fleet_cache.agent_id` 255)
exceeds it and is written verbatim with no normalization; PostgreSQL
RAISES on overlength and sqlite stores in full (verified), so no engine
can hold an id that differs from what was sent and truncation-induced
false-allow is structurally impossible. The 64-char evidence columns
refuse an overlong write and fail closed. Recorded as informational: the
identity-width invariant guards no device column. **Named follow-ups, not
touched:** the SM's legacy R4-3 firmware surface does not enforce the
E1.3 one-site boundary on its create and skills-install lists;
`DispatchAction` supplies `agent_scope=True` as a constant rather than an
evaluated input; `InstallSkill` keeps a pre-E0.2 unresolved-site
fallback; `_scope_rule_within` never resolves a `device` rule's site (a
site administrator is refused on a device at their own site —
over-restrictive, never fail-open).

**A30.23 — A6-4B0b-S1 pre-merge remediation (independent review, HIGH):
the approver's permission basis at dispatch is CURRENT, never the role
the approval recorded (recorded 2026-09-15, BEFORE the code).** A30.22
stated a limit rather than hiding it: the dispatch-time re-resolution
took `authority_snapshot.role` — the role the approver's token carried
WHEN they decided — as the permission basis and applied it to the grants
they hold NOW, so a Keycloak realm-role demotion after approval was
invisible to the gate. The review ruled the stated limit a HIGH, and it is
right: a principal's authority has two halves, the scope grants Central
Command holds and the realm role Keycloak holds, and A30.17's rule —
approval is historical governance evidence; execution permission is
CURRENT authority — admits no half. An operator who held `action.approve`
when they approved an immutable wave over [A, B, C] and was then demoted
to a role without it, their grants untouched, still counted toward that
wave's dispatch. **Ratified.** (1) The permission basis at dispatch is
derived from the approver's CURRENT effective realm roles in the tenant
realm, turned into permissions by the SAME rule the request path uses —
`auth.role_basis`, `pick_role` over the ranked roles then
`ROLE_PERMISSIONS`, ONE function that `get_current_user` and the dispatch
gate both call, pinned structurally — and then resolved through the ONE
scope loader exactly as before, so the grant-recorded role ceiling of
A23-3 still narrows inside `resolve()`. No second RBAC, no second role
resolver, no route-specific permission logic, no new permission;
`MACHINE_PRINCIPAL_CEILING` unchanged. (2) Central Command holds no
Keycloak admin credential (A20) and asks the Console over the EXISTING
CC→Console internal channel, which gains one READ —
`GET /api/internal/tenants/by-realm/{realm}/principals/{subject}/authority`,
answering found / enabled / effective realm roles, resolved by realm
exactly as A23-5's owner read is. It is an identity-plane read on the
shared-key channel, not a tenant-plane route: `ROUTE_CONTRACT` and
`MACHINE_SURFACE` are unchanged and no human or machine principal can
reach it. (3) FAIL CLOSED, with the cause named. An approver the realm no
longer holds, a disabled account, a realm the Console cannot bind to a
tenant, an unreachable Console and an unconfigured realm each mean that
approval does NOT stand — never "the recorded role", never a silent
default. Each lost approver on the withheld audit entry carries a cause
from a closed set (`current_role`, `principal_not_found`,
`principal_disabled`, `unresolvable`, `scope`) and the current role where
one was resolved, so a demotion is distinguishable from a revocation and
from an outage. (4) The ledger is not touched. `authority_snapshot.role`
stays exactly as written — evidence of the authority held at decision
time — and a restored role lets the SAME historical approval count again:
the approval is re-established as current authority, not re-made, and
nobody approves twice. **Stated rather than promised:** no lock is held
between the current-authority read and the CC→SM call (the interval
A30.22 already named); `pick_role`'s `viewer` default for a principal
with no ranked realm role is the request path's own rule, kept identical
here so the two paths cannot disagree, and `viewer` carries no
`action.approve`. **Unchanged:** the permission vocabulary (25),
`ROLE_PERMISSIONS`, `MACHINE_PRINCIPAL_CEILING`, `MACHINE_SURFACE`, every
Central Command route, the schema (CC head `0026`; the Console schema is
untouched), autonomy, proposal admission and dispatch, SM and node
authority, and every S1 boundary A30.22 ratified. General B0b, B0c, B1,
B2 and the taxonomy remain not started.

**A30.24 — A6-4B0b-S2: permission-aware read reach (decided: Vinod;
recorded 2026-09-19, BEFORE the code).** Producing the general B0b
boundary reproduced a second defect underneath F1, and it points the other
way. **P1 — fail-OPEN, pre-existing since E1.2.** `_project` builds
`ResolvedScope.site_ids` — and `tenant_wide`, and the permission-less
`covers_site` / `covers_device` / `covers_org_unit` helpers — from EVERY
effective grant, whatever that grant's `permission_subset` carries. The
route guard is nominal role membership (`has_permission`, which A26.11
already established cannot enforce a per-grant subset). A read therefore
took its PERMISSION from the role and its REACH from any grant at all:
with grant A over site-a carrying the full role and grant B over site-b
narrowed to `incident.view`, `site_ids` is `{site-a, site-b}` while
`permits("fleet.view", site_id="site-b")` is False — and every fleet,
approval, outcome, audit, campaign and site read at site-b answered. No
explicit subset is needed to reach it: the role a grant RECORDS is a
ceiling (A23-3), so a person who is `site_admin` at one site and `viewer`
at another carries a `site_admin` token, passed the guard on the approval
queue and the grant list, and read the second site's — the realistic face
of P1. The
stronger form is the `tenant_wide` shortcut: a TENANT grant narrowed to
one permission unfiltered every read in the platform. It is the defect
A26.11 closed for `governance.view`, left standing on every other read,
and it outranks general B0b for the reason S1 did: B0b adds the device and
device_class dimensions to this same construction, so it would have
extended a fail-open. **The invariant, LOCKED.** For any read requiring
permission P, a resource is readable only when ONE effective grant both
carries P and covers the resource:
`READABLE(P, r) = ∃ effective grant G: P ∈ G.permissions ∧ G covers r`.
Permission from one grant never combines with reach from another; role
membership never substitutes for a grant's subset; nothing is synthesized.
**The primitive.** `scope.read_reach(resolved_scope, *permissions)` returns
a frozen `ReadReach` derived ONLY from the effective (non-inert) grants
that carry at least one of the named permissions (`"*"` counts, exactly as
in `permits`): `tenant_wide`, `site_ids` (site grants ∪ org-expanded),
`org_unit_paths`, `device_ids`, `device_classes`, and coverage helpers that
delegate to those same grants' `covers_*` — so `reach.covers_x(t)` is
`permits(P, t)` by construction, asserted over a generated matrix. Several
permissions mean ANY OF, for the routes whose guard is
`require_any_permission`. It is a PROJECTION of canonical grant semantics,
not a second resolver: it reads no row, no token and no tree, only a
`ResolvedScope`. Contextual ancestry is not an input. A WHERE-only scope
(A22.13) refuses the question, as `permits` does. **`ResolvedScope.site_ids`
is NOT changed** — it stays the lifecycle-correct, permission-NEUTRAL
projection, because legitimate consumers ask a permission-neutral question
(an Operational Agent's operational reach, `/api/scope-grants/me`, the L2
approval snapshot). What changes is that it may no longer be a read
filter: `scope_sites`, `_audit_scoped` and `CampaignRepo.list_all` accept
`None` (an internal caller) or a `ReadReach` and RAISE on anything else, so
a permission-neutral scope cannot reach a repository filter by oversight;
a source-level guard additionally fails the suite when an API module reads
`.site_ids`, `.tenant_wide` or a permission-less `covers_*` off the
resolved scope outside a named allow-list, and when the permissions a
handler passes to `read_reach` differ from the permissions its own route
guard accepts. **The one permission-neutral filter, named.** Three internal
reads narrow to an Operational Agent's OWN reach — the CC-resident
evaluator, the dry-run and the ingress re-derivation — and that scope is
WHERE-only by ratified design (A22.13): an agent's authority is its
bindings, so it carries no permission to be aware of. `scope.where_reach`
is `read_reach`'s mirror and its only other constructor: it builds the
same projection from a WHERE-only scope and REFUSES every other scope, so
a principal's own authorization scope can never become a
permission-neutral filter through it, and `read_reach` refuses a
WHERE-only scope in turn. **Scope of S2, exactly.** Every class-C reader of the
Phase-1 inventory takes its reach from `read_reach` with its route's own
permission: fleet, agents, capabilities, firmware exposure, predictive
risk, warranty and attention (`fleet.view`); incidents (`incident.view`);
approval queue, history and records (`action.approve` or `audit.view`);
outcomes, patterns, learning candidates and signals, autonomy narrowing,
campaigns and Operational Agent reads (`fleet.view`); audit
(`audit.view`); sites (`fleet.view`); the org tree (`site.view`); the grant
list (`user.view` or `audit.view`); and the campaign preflight's
caller-intersection (`site.manage`). Object visibility that precedes a
mutation gate (404 before 403) asks the object's READ permission.
**What a human can see change:** only a principal holding a grant whose
subset withholds the permission a route requires reads less — which is the
correction. A principal whose grants all carry the full role reads
byte-identically, asserted by regression over the existing tenant, org and
site personas. **OUT, and general B0b's:** the device / device_class
under-reach (F1 stays OPEN — `ReadReach` carries `device_ids` and
`device_classes` and no repository filter consumes them yet), contextual
site projection, incident and outcome ownership, approval class arity,
parent-incident redaction, candidate-skill ownership, the empty-class
reconciliation, the taxonomy. **Unchanged:** the permission vocabulary
(25), `ROLE_PERMISSIONS`, `MACHINE_PRINCIPAL_CEILING`, `MACHINE_SURFACE`
(13), every route (zero added) and response shape, the schema (CC head
`0026`), `resolve()`, `permits()`, every mutation gate, S1. General B0b,
B0c, B1, B2 and the taxonomy remain not started.

**A30.25 — A6-4B0b: canonical reach convergence — F1 closed (decided: Vinod;
recorded 2026-09-19, BEFORE the code).** All four prerequisites are closed and
main-verified: S1 (A30.22/A30.23 — a SET of devices is authorized only when
every device is, on current authority), S2 (A30.24 — a read takes its reach
from the grants that carry its permission), S3 (A30.26 — a principal's
autonomy contract is COMPOSED over the sites its reach authorizes, which
closed P2 below) and S4 (A30.28/A30.29 — learned-signal, fleet-pattern and
generated-content payloads are projected per reader, which closed E3-F1).
*Reconciled 2026-09-22: this slice was written against S2 and merged onto S3
and S4 before its review; wherever its text or code overlapped theirs, S3 and
S4 win, and the paragraphs below say what that changed.* General B0b is the
slice A30.11 scoped, and it builds ON S2's `read_reach`:
`ReadReach.device_ids` and `device_classes` have been permission-narrowed and
unconsumed since S2, and this amendment consumes them. **F1, restated.** A
`device` or `device_class` grant satisfies `covers_device()` and `permits()`
while contributing nothing to `site_ids`, and every device-bearing repository
read filtered on `site_ids` alone, so such a principal read ZERO list rows
about devices the platform itself says it reaches — while the device DETAIL
reads, which ask coverage, already answered. **The ratified decisions.**
R1–R10 were locked when the boundary was requested and D2–D10 were ratified on
the boundary report; they are recorded here as the slice's law. **R1** a
blank, null or empty `device_class` is never a wildcard and is never
reinterpreted as `server` for an authorization question; a configured row with
a blank class stays administratively visible and confers zero class reach.
**R2** correlation never widens reach: a visible child incident does not
authorize its hidden parent, a sibling, or site-wide correlated fault data.
**R3** device and device_class scope never expands to site audit —
`cc_audit_log` has a `site_id` and no device column, so ownership of an entry
by a device cannot be proven and the read fails closed. **R4** raw site-level
error budgets, stop-switch configuration and governance aggregates are not
context; B1 may publish narrow conclusions. **R5** S1 is not redesigned; the
immutable target-set and plan semantics are unchanged. **R6** a matching
`device_class` may satisfy the SCOPE dimension of a single-target approval
where the target canonically belongs to that class; every other approval gate
stays mandatory and no standalone authority is created. **R7** the broader
safe approval-evidence projection is a named follow-up. **R8** no
device-identity width migration. **R9 / D5** a missing or deleted device is
NATURAL ZERO: the configured grant is never revoked, mutated or made inert
because the fleet cache cannot currently resolve its target (that would flap
the grant lifecycle with a cache), it stays administratively visible
(`target_status` already reports it), and it confers zero operational reach
until the target resolves canonically. **R10** contextual ancestry never
participates in authorization. **D2** a device or device_class principal
receives MINIMAL containing-site context, explicitly marked `contextual: true`
and reduced to `id` and `site_name` — never the Site Manager endpoint, licence
fingerprint, authoritative site state, org placement, timestamps, governance
or administrative configuration. **D3** contextual org-unit ancestry is
DEFERRED. **D4** a candidate skill is SITE-owned; `source_device` is
provenance, not ownership. **D6** the ingest compatibility that stores a
missing class as `server` remains as transitional debt for the datacenter
taxonomy to replace with UNKNOWN, and no authorization decision in this slice
depends on an INFERRED class. **D7** `parent_incident_id` is `null` when the
parent is not independently visible. **D8** approval records follow the owner
rule and nothing broader. **D9** the campaign-wave approval-queue asymmetry
(S1 lets a class approver sign a wave the queue does not list) is a named
follow-up. **D10** the Site Manager's outcome-identity fallback is NOT a
prerequisite; no read is broadened to accommodate a non-canonical identifier.
**The owner rule — canonical, and ONE.** A row or subject that names a device
is DEVICE-OWNED when that device CURRENTLY RESOLVES in the fleet cache AT THE
ROW'S OWN SITE, and SITE-OWNED otherwise — when it names no device, or names
one Central Command cannot identify there (S1's rule for waves, A30.22, now
the rule for every device-bearing read and for the single-target approval
gate). A device-owned row is readable under permission P when ONE effective
grant carrying P covers the device: by site (so by org unit or tenant), by
device id, or by the device's CURRENT class. A site-owned row is readable when
such a grant covers the site. This is `Grant.covers_device` asked of the
target as it currently resolves — no second resolver, no taxonomy-driven
authorization, no contextual authority — and it is what makes R9 natural and
R1 structural: an unresolved device has no class to match and no identity to
own a row with. **The predicates — named and table-aware, not one generic
filter.** `scope_fleet_devices` (the fleet cache itself: site ∪ device id ∪
`lower(device_class)`), and `scope_device_owned(site_col, device_col)` for
every other device-bearing table — site, OR a non-empty device column whose
device resolves in the fleet cache at that row's site and is covered by id or
by current class (a correlated `EXISTS`, because only `cc_fleet_cache` carries
`device_class`). Both consume a `ReadReach` and nothing else, refuse a bare
`ResolvedScope` as S2's filters do, and yield `false()` for an empty reach;
`read_reach` drops an empty device or class ref so a blank can never become
`IN ('')`. **Device-owned domains (changed):** the fleet cache (list, filtered
list and its count, health counts, total); incidents (list, children, detail);
approval routes (queue and history, plain and paginated); agent proposals
(awaiting-approval, an agent's own list and receipts); outcome history;
approval records (the owner rule, D8); and the single-target approval scope
gate (R6/R9 — the target's id, site and CURRENT class, or the site question
when it does not resolve). Everything that reads THROUGH those repositories
converges with them and needs no rule of its own: attention, predictive risk,
firmware exposure, warranty, the capability registry, fleet summary, the
Operational Agent view, and the CC-resident evaluator's attention read
(ordering only — it never gated a proposal). **Incidents (R2/D7).** Children
are filtered by the same predicate as the list; `parent_incident_id` is `null`
wherever the parent is not independently visible; and for a caller who sees an
incident ONLY through device ownership — who does not cover its site —
`correlation` is withheld (it is an open dict with no bounded contract, A30.7,
and a device-owned `network_ambiguity` incident stores its peers' ids in it as
`votes`) and prior learning is narrowed to cohort knowledge, never the site's.
The attention contract narrows site-scoped learned signals by the same rule.
Both COMPOSE with S4 (A30.28/A30.29), which is applied first and taken as
given: the signals are projected through the reader's `LearningView`
(`fleet.view` reach — a cohort conclusion kept, its evidence bounded, a
site-scoped signal only for a site held under `fleet.view`), and this slice's
ownership rule then withholds the incident's site from a caller who reads the
incident only through device ownership. The diagnosis — its pattern citations
and its generated block — is projected by that same `LearningView`,
independently of the owner rule; nothing here restores raw generated text or
an unbounded payload. **Site-owned domains (deliberately unchanged):** audit
(R3), campaigns and waves (R5), candidate skills (D4), learned signals and
fleet patterns (bounded per reader by S4, and not made device-aware here), the
org tree (D3), and the grant list. **Autonomy (R4) — true by construction
under S3; P2 CLOSED by A30.26.** As first written, this slice made R4 true by
EMPTYING a composed contract for a reader who holds no site: `narrow_to_sites`
(A23-1) filtered the top-level lists whose items carry a `site_id` and never
looked at the error-budget aggregate FOLDED across sites or at each action
class's `safety` block, so a reader holding no site still received every
site's drop-back status, suppressed fault-domain names and per-site budgets.
It was found by this slice's LIVE gate, where the stack has real safety state
(the unit estate had none, so the first R4 assertion compared an empty list
with an empty list; the estate now carries safety state at every site so the
assertion means something), and it recorded **P2** beside it: a reader who
DOES hold a site read those same fields about EVERY site, a pre-existing gap
this slice left for its own ratified narrowing slice (A30.8). That slice is S3
(A30.26), shipped and merged BEFORE this one: `narrow_to_sites` no longer
exists, a contract is composed over the reader's authorized sites by
`select_site_inputs` BEFORE anything is folded and is never narrowed
afterwards, and a stored verdict is narrowed where it is read, through
`AutonomyView`. This slice therefore carries NO autonomy change of its own —
its `holds_no_site` edit and its frozen pre-S3 oracle were dropped in the
reconciliation, not merged — and R4 holds structurally: an empty reach selects
nothing and is never read as unrestricted, `reported` is false, nothing is
zero-filled, and the tenant posture and the ladder the reader operates under
remain, with the dispositions composed over the selected sites as S3 ratified.
The pin `test_P2_is_recorded_here_not_fixed_here` is INVERTED, as the F1 pin
was, into the assertion that a site holder reads only its own site's drop-back
status, suppressed domains and remaining budget. What a site holder reads
changed by S3's ratification, not this slice's — its own site's error budget,
not the tenant total — and the Operational Agent view and every proposal
projection this slice touches consume S3's `load_autonomy_contract(reach=…)`
and `autonomy_view(scope)` as given. **Context is not authority (R10/D2).**
`SiteRepo.list_all` stays AUTHORITATIVE — it is what `sites_count`, site
reach, delegation and administration are computed from.
`SiteRepo.list_context` is a separate read of the sites that merely CONTAIN a
device the caller reads, used for two things only: the contextual rows of
`/api/sites/` and `/api/sites/{id}`, and site NAMES in payloads about the
caller's own devices. A contextual id never enters `ResolvedScope`,
`read_reach`, a delegation ceiling or an approval decision; `covers_site()`
stays false for it; and every site mutation, already gated on
`permits(site_id=…)`, refuses it — asserted structurally and by a mutation
sweep, not left to review. Authoritative site rows are byte-identical to
before. **R1 alignment.** The authorization call sites that defaulted a blank
class to `server` — the device detail reads, the campaign preflight
caller-intersection and `operational_agent.resolve_scope` — stop doing so;
display projections and the ingest default are untouched (D6). **What a human
can see change:** only device- and device_class-scoped principals, who gain
the legitimate reads they were denied. For tenant, org and site principals
every predicate collapses to the `site_id IN (…)` it replaced, asserted
differentially against the legacy filter for every read route. **Machine
plane:** unchanged in shape — `MACHINE_SURFACE` stays at 13, no route reopens,
the ceiling is unmoved; a device-scoped agent's existing reads now agree with
its canonical reach. B0c follows immediately and completes their metering.
**Unchanged:** the permission vocabulary (25), `ROLE_PERMISSIONS`,
`MACHINE_PRINCIPAL_CEILING`, `MACHINE_SURFACE`, every route (zero added), the
schema (this slice adds no migration; CC head `0027` and SM head `0011` are
carried from S4, A30.15), `resolve()`, `permits()`, `ResolvedScope.site_ids`
(still truthful and permission-neutral — a device grant still contributes
nothing to it), S1, S2, and the `device_class` vocabulary `server | switch`:
the datacenter taxonomy is not implemented here. **Named follow-ups:** S3-E1
and S3-E2 (A30.27 — ratified, NOT implemented, and never inside this slice);
D3 org-ancestor context; D6 UNKNOWN class at ingest (taxonomy); D9 the
wave-queue asymmetry; R7 approval-evidence projection; D10 skip-and-warn
hardening of the SM outcome-identity fallback; the composite projections and
the `/api/scope-grants/me` self-description recorded by S2; and, as test
hygiene outside this slice, `tests/unit/cc/test_warranty.py`'s hard-coded
`2027-01-01`, the same wall-clock class as the checkpoint fixtures fixed in PR
#46. B0c, B1, B2 and the taxonomy remain not started.

**A30.26 — A6-4B0b-S3: autonomy scope isolation (decided: Vinod; recorded
2026-09-20, BEFORE the code).** *Numbering: A30.25 is the general B0b
amendment; its number was reserved here while that slice waited on this one.
It has since merged (PR #48, `d69105c`, 2026-09-22).* General B0b's live gate
found a second pre-existing defect, **P2 — FAIL-OPEN information
disclosure across the site boundary, present since S5 and only partly
closed by A23-1.** `build_autonomy` folds EVERY site's safety state into
the contract and `narrow_to_sites` then filtered the top-level lists whose
items carry a `site_id`. It never looked at an aggregate, because an
aggregate has no `site_id`. Reproduced on `main` with three sites and a
site-A reader: `safety_state.error_budgets` and every class's
`safety.error_budget` read `total 81, sites_dropped_back [site-C]`;
`safety.suppressed_domains` named `fault-A, fault-B, SECRET-C`;
`safety.site_budget_remaining` was keyed by all three site ids;
`posture.stop_switch.sites_reporting_active`, `safety_state.reported`, the
outcome `evidence` and `advancement` counted site C; and the class
`disposition` itself read `requires_approval` BECAUSE site C had dropped
back. The Phase-1 inventory found two further faces of the same defect.
**(a) A probe oracle:** `site_id` is a query parameter and the composer
honours it before narrowing, so the same reader calling
`/api/autonomy/?site_id=site-C` received site C IN ISOLATION — its error
budget (47 outcomes, 14.89 %), its suppressed fault domain, its remaining
budget, its stop switch — a targeted read of any site in the tenant.
**(b) Persisted copies:** the evaluator copies the tenant-wide class row's
`blocking_conditions` and learned signals onto every proposal it admits, so
a proposal at site A carries `error_budget_dropped_back / site-C` and
`domain_suppressed / site-C / SECRET-C`, and seven projections return them
unfiltered: the approval queue, both approval-decision responses, the
Operational Agent detail and proposal list, the dry-run, the machine
submission response and the machine receipts — the last five reachable by
an external runtime whose grant names one site. **Classification.** In HarkenIQ site
and org scope are a security boundary; this is a scope isolation defect,
not a display one. **The invariant, LOCKED.** For any autonomy projection
returned to principal P, EVERY site-derived fact is derived only from
sites covered by P's current canonical effective scope:
`visible_autonomy_fact(P, fact)` requires ALL site inputs contributing to
`fact` to be inside P's authorized reach. It is not satisfied by narrowing
the outer site list while keeping a global aggregate, by keeping a hidden
site's name in suppression or fault data, by computing a budget over hidden
sites, by presenting site safety state through a tenant-level-looking
structure, or by treating contextual site ancestry as authority. **The
authority source is the one that already exists.** A site-derived autonomy
fact is a `fleet.view` fact — that is the permission `/api/autonomy/` is
read under — and it stays one wherever it is projected, so the authorized
set is `read_reach(scope, "fleet.view")` (A30.24) on EVERY projection,
including those whose route is guarded by another permission: an approver
holding `action.approve` at a site without `fleet.view` there reads the
proposal and not that site's safety rows, which is what stops a
subset-narrowed grant carrying these facts out through a different route.
`tenant_wide` means the whole tenant, otherwise exactly `site_ids` — site
grants and org-expanded sites, from grants that carry the permission. No
second resolver, no autonomy-specific authority model, no separate
interpretation of site membership; a `device` or `device_class` grant
contributes no site (R4), an inert, expired, revoked or subset-narrowed
grant contributes nothing, and a contextual site (general B0b, R10) is not
an input because `ReadReach` has no field that could carry one. **Field
ownership.** *Tenant-owned, unchanged for every reader:* contract version,
actor block, the tenant stop switch, configured level and its source,
budget limit / period / used, device-scoped budget rows, the ladder, each
class's risk, required permissions, grant level, `budget_mapped`,
`never_budget_grantable`, the approval policy block, and tenant-scoped
blocking conditions. *Site-owned:* `scope.sites`, `sites_reporting`,
`sites_not_reporting`, `suppressions`, `site_stop_switches`, site- and
domain-scoped blocking conditions, each class's `safety.suppressed_domains`
and `safety.site_budget_remaining`, `sites_dropped_back`, site-scoped
learned signals. *Aggregates derived from sites:*
`stop_switch.sites_reporting_active`, `safety_state.reported` and each
class's `safety.reported`, `safety_state.error_budgets` and each class's
`safety.error_budget`, and — because they are folded from those — each
class's `disposition`, `disposition_reason`, `approval.required`,
`evidence` and `advancement`. **The rule: SELECT, then aggregate.** The
composer selects its site inputs — safety rows, sites, outcomes and
site-scoped learned signals — for the authorized set BEFORE anything is
folded, and then folds only what was selected; a hidden site's row is never
read past its site id. Computing the tenant-wide value and hiding labels
afterwards is the defect, so `narrow_to_sites` is REMOVED rather than
repaired: a function whose input is a composed contract cannot satisfy the
rule. The composer already had this shape for one site (`site_id`); S3
generalizes that selection to a set, and `site_id` now narrows WITHIN the
authorized selection and can never widen it — a site outside the caller's
reach composes over nothing and is indistinguishable from a site that does
not exist. **Principal behaviour.** Tenant-wide: tenant-wide aggregates,
byte-identical to before. Org-scoped: the authorized subtree's sites only.
Site-scoped: that site only. `device` / `device_class` only, and every
principal whose reach is empty (no grant under strict, expired, revoked,
inert, or a subset without the permission): NO site-derived error budget,
stop or safety internal, suppressed fault domain or drop-back state — the
site-derived part of the contract is composed over nothing, so it is empty
by construction rather than zeroed, `reported` reads false (unreported is
UNKNOWN, never safe), and tenant-owned posture is preserved because it is
genuinely tenant-owned and not reconstructed from hidden sites. B1
publishes safe conclusions for such principals later; nothing here does.
**Persisted verdicts are narrowed where they are READ.** A proposal's
stored `blocking_conditions` and `evidence.learned_signals` keep what the
evaluator recorded; every projection returns a row that names a site only
when that site is inside the reader's `fleet.view` reach, and keeps
tenant-scoped rows. One implementation, asked by all seven projections,
through a typed view that only the canonical scope can construct and that
every projection REQUIRES, so a new projection cannot omit it and cannot
be handed "unrestricted" by mistake; applied
at read time so rows written before this slice are covered and no write or
decision path changes. **A stored REASON follows its row.** The evaluator
copies a verdict's `disposition_reason` from a blocking row's own text, so a
reason can BE the text of a row the reader may not read — and a
domain-scoped row's text names its fault domain. Where the stored reason is
the text of a withheld row and of no row the reader keeps, it is replaced by
one neutral sentence saying the verdict rests on a governance condition
outside the reader's authorized scope. It states that something is withheld
and not what; a tenant-wide reader reads the reason as recorded. **What S3 deliberately does NOT change — internal
decisions.** Five consumers use the contract to DECIDE and never return it:
the CC-resident evaluator, the ingress re-derivation, the dry-run's
reasoning (A22.6: it must reason exactly as the runtime does), campaign
submission and the activation preflight. They keep the tenant-wide
composition, named by an explicit `reach=None` that a structural test
allow-lists by call site, so execution semantics are byte-identical.
**Ambiguous, REPORTED and not changed (Vinod's to rule).** *E1 — the
tenant-wide disposition fold is execution semantics:* S5 made a drop-back
at ANY site require a human for that class at EVERY site, while E0.2 made
the Site Manager's enforcement per site. A proposal at site A can therefore
be `requires_approval` because of site C, and its `disposition` and status,
the preflight's unattended / attended class lists and its
`safety_reported` flag still reflect that; they name no site and say
nothing about which condition or where, and removing the inference needs
per-site evaluation, which WIDENS autonomy at Central Command and is a
product decision. *E2 — tenant-wide
outcome statistics persisted on a proposal* (`evidence.outcome_evidence`
and the sentence `_rationale` writes from it) cannot be recomputed for a
reader after the fact; closing it changes what the evaluator WRITES. *E3 —
cohort learned signals* are fleet knowledge by A23's ratified decision and
name a vendor and model, never a site; they stay visible. **What a human
can see change:** only org-, site-, device- and class-scoped readers, and
only by reading LESS — the facts, totals, booleans and dispositions of sites
they do not hold. **Machine plane:** unchanged in shape; a machine
principal's agent view, dry-run, submission response and receipts stop
naming sites outside its grants. **General B0b is NOT changed by this
slice** and its branch is not touched: no device or device_class repository
filter, contextual site projection, incident, approval or outcome
ownership, campaign logic, S1 or S2. When B0b merges `main` it drops its
own `narrow_to_sites` edit (R4 for a reader holding no site is now true by
construction), inverts the test that pins P2 open, and re-verifies.
**Unchanged:** the permission vocabulary (25), `ROLE_PERMISSIONS`,
`MACHINE_PRINCIPAL_CEILING`, `MACHINE_SURFACE` (13), every route (zero
added) and response SHAPE, the schema (CC head `0026`), `resolve()`,
`permits()`, `read_reach`, every mutation gate, the `server | switch`
vocabulary. B0c, B1, B2 and the taxonomy remain not started.

**A30.27 — The S3-E1 / S3-E2 / S3-E3 architecture decision package
(decided: Vinod, 2026-09-21; RECORDED here, NOT implemented).** A30.26
reported three things it did not change. The package was produced read-only
on `a5f9549` after S3 closed and ratified as R1–R7. They are recorded so
that the slice which implements them starts from a ratified text, and so
that S4 (A30.28) can say exactly what it is not. *Naming: S3-E1/E2/E3, never
bare "E1" — that collides with the Enterprise Governance slices E1.1–E1.4.*
**R1 — Model C:** a proposal is assessed against its TARGET SITE (the S3
primitive already yields that row: the composer over the device's own site)
plus a CLOSED, typed global safety gate; the tenant-wide disposition fold
stops being the assessment. **R2 — option A:** the gate's hidden-derived
membership is EMPTY — no condition at a site the target does not belong to
enters the gate. Established by execution: the fold globalises only
`error_budget_dropped_back` and `budget_window_exhausted`; a site's Site
Manager stop switch and an unreported site do not affect disposition today,
suppression is already per target site, and the Site Manager enforces
drop-back and site stop per the device's OWN site. **R3:** a target site
that has not reported is REQUIRES_APPROVAL (unreported is unknown, never
safe); a target-site Site Manager stop is a LOCAL denial. **R4:** a campaign
is autonomous only if EVERY site in its own immutable plan assesses
autonomous AND the gate passes. **R5:** the activation preflight assesses
over the AGENT's own reach, and `safety_reported` means every in-reach site
reported — which also removes the one non-monotone input the package found
(a hidden site reporting flipped the dimension UNKNOWN → READY). **R6 — E2
dual evidence:** `decision_evidence_at_creation` is immutable and
`viewer_projected_evidence` is what a reader is shown; the rationale is
rendered from typed facts, not stored as a sentence. **R7:** E3-F1 is fixed
BEFORE general B0b (PR #48) is refreshed. **Sequencing, unchanged:** R1–R6
change what Central Command DECIDES and could widen autonomy; they are
implemented after general B0b merges and main-verifies, in their own slice
with their own boundary. **A23 is NOT reopened by E3:** a vendor/model
cohort conclusion is tenant knowledge. What the package demonstrated is a
PAYLOAD defect under that ruling, which is A30.28.

**A30.28 — A6-4B0b-S4: learned-signal and fleet-pattern payload isolation
(decided: Vinod, 2026-09-21; closes E3-F1).** *Ordering truth: the
ratification preceded all code; an interrupted session then drafted the
production change in the working tree before this text was committed.
Nothing had been committed or pushed, so this docs commit is still the
first commit of the slice and no history was rewritten to make it so.*
**The defect, E3-F1 — FAIL-OPEN disclosure across the site boundary,
pre-existing since S3's learning substrate, missed by the A23-1 sweep and
by A30.26's sentinels because every seeded signal carried
`evidence = {}`.** A fleet pattern is detected over the whole tenant, and
`derive_signals` copies its whole `evidence` — for a `cross_site_batch`,
`site_failure_counts` keyed by SITE ID, `sites_affected`, `total`,
`failures` — onto the cohort signal and onto every site signal. Readers
selected ROWS by scope and returned their CONTENT as stored. Demonstrated
over HTTP under STRICT: a site-1 operator read hidden site-3's id and its
count of 27 on a cohort row they were entitled to see, through
`/api/learning/signals`, `/api/attention/` (`evidence.learned_signals[]`,
and ON `MACHINE_SURFACE`) and incident `prior_learning`; the statement said
"30 of 40 attempts, across 2 sites". `/api/outcomes/patterns` had a
route-local `_narrow_sites` that filtered two keys and only when they were
a dict or list — production writes `affected_scope.sites` as a
comma-joined STRING, so it passed whole — and never touched
`sites_affected`, the totals or `description`. **A23 is RETAINED, exactly:**
the vendor/model cohort CONCLUSION is tenant knowledge and every reader
keeps it; a site-scoped signal follows its site; a pattern whose named
sites are all outside the reader's scope is absent (A23-1's read rule).
**S4 fixes payload isolation ONLY** — no decision path, no stored row, no
selection rule changes. **The invariant, LOCKED.** The cohort conclusion is
tenant knowledge; site-specific SUPPORTING evidence is scope-filtered. A
scoped principal must not recover a hidden site's identity, count, or the
size of the hidden estate through structured evidence, a pattern payload,
statement or description text, a rationale, a distribution payload,
`evidence_cited`, a learning cycle, a counter, a percentage, a rank, row
order, list length, site cardinality, attempts or totals. **Three S4
decisions (Vinod).** *(1) Bounded bands.* For a reader who is not
tenant-wide, a RATE is the conclusion and is kept rounded to the nearest
5 %; CONFIDENCE is rounded to a 0.25 grid; EVERY COUNT is withheld — total,
failures, attempts, model total, `sites_affected`, a hidden site's entry —
and the payload says so in an explicit `withheld` list rather than
inventing a smaller number. Exact values are not kept because they are
counts in disguise: `batch_failure` confidence is `min(1, total/20)`,
`reliability` `min(1, total/30)`, `cross_site_batch`
`min(1, total/20 + 0.1·sites)`, so exact confidence plus a three-decimal
rate plus the reader's own site count recovers the hidden estate's totals
exactly. A site-keyed map keeps only the reader's own sites and is ALWAYS
marked `partial`, dropped-or-not, so the marker says nothing about sites
the reader does not hold. Field types and response shapes are unchanged;
a tenant-wide reader's payload is byte-identical. A band is permitted only
because an exact hidden-estate value cannot be reconstructed from it; it is
not a licence to publish a coarser count. *(2) Source patterns are IN S4*,
through ONE canonical projection (`harkeniq_cc.learning_projection`) that
serves signals AND patterns; `_narrow_sites` is deleted, not repaired. It
names what may pass, never what may not (A25.3): an evidence key the
projection does not recognise is withheld. *(3) The reach rule is
`fleet.view` on EVERY projection* — A30.26's rule applied to learning — so
the authorized set is `read_reach(scope, "fleet.view")` (A30.24) wherever
learned knowledge is shown, including incident detail under
`incident.view`. No second resolver; the view is a TYPE
(`LearningView`) every projection REQUIRES, so a new projection cannot
omit it or be handed "unrestricted" by mistake, and the composer's
`learning` argument is REQUIRED with no default: a `LearningView` for a
principal, `None` for the internal decision paths (evaluator, dry-run
reasoning, ingress re-derivation), allow-listed by call site in a
structural test. **TEXT FOLLOWS ITS EVIDENCE.** Where typed evidence
exists, a statement or description is RE-RENDERED from the PROJECTED
evidence by the generator that wrote it (`render_statement` is that one
generator, and every number in it comes from its `evidence` argument), so
text cannot state what the structure withheld. Where none exists — the
frozen copy on a proposal keeps the statement and not the evidence; a Site
Manager cites a pattern by description — the stored text is reduced through
the two generators' small fixed grammar and then PROVEN: anything still
shaped like a count replaces the whole text with one neutral sentence.
Fail closed. **Three further ratifications (Vinod), all IN S4.**
*(A) Distribution payload.* `PushPolicy.learned_patterns_json` carried the
whole tenant pattern to every Site Manager whose fleet held the cohort, so
site A's Site Manager durably stored site C's id and count. A receiving
site is a reader that holds exactly itself and gets the SAME projection by
the SAME function; it is still told about a cohort it holds and is not yet
failing on (R-C2 is why the loop exists), and told the bounded conclusion
only. The Site Manager upserts by pattern id and the distribution ledger is
in-process, so a Central Command restart re-pushes and overwrites what an
earlier release delivered: no Site Manager change, no migration. A hidden
site's facts do not survive merely because the pattern crossed CC→SM.
*(B) `evidence_cited`.* A Site Manager's diagnosis cites the patterns it
reasoned with, by description, and that text rides back inside a diagnosis
for a device the reader DOES hold — including text pushed before this
slice. Bounded at READ, by the same projection, and ONLY for an entry that
cites a fleet pattern: the rest of that list is the device's own telemetry
("3 of 5 fans") and reducing it would destroy the diagnosis to protect
nothing. No second redaction model. *(C) `/api/learning/cycles`* names no
site, which is why it was declared UNSCOPED on the strength of "counts
only" — and a hidden-estate count is exactly what a scoped reader may not
be handed. `sites_distributed` and `devices_applied` are withheld (null,
and named in `withheld`), `outcomes_before/after` take the evidence
projection, `improvement_pct` is rounded to 5 points; the route is
re-declared READ_SCOPED so the contract describes runtime truth. Rows are
not filtered (none names a site) and the stored ledger is untouched.
**Same-band ordering protection.** Two hidden-estate states that fall in
one band must not be told apart by a deterministic secondary channel. The
repository orders signals by the EXACT stored confidence, so two signals in
one band were still ranked by the value the band withholds. A scoped
reader's rows are ordered by what they are SHOWN — banded confidence, then
the signal's own key — on every path: the signal list, the autonomy
contract's `learning[]`, attention's per-device signals and the two quoted
into `reasons[]`, incident `prior_learning`, and the frozen copy on a
proposal, which the evaluator wrote in exact order and which is re-ordered
at read, never rewritten. **A second ordering channel, found by the
slice's own twin estates and corrected here before the code landed.**
`OutcomeAggregator.get_metrics` sorts cohorts by their tenant attempt
TOTAL; the detector stamps each pattern with `time.time()` in that order,
and the pass opens its cycles and upserts its signals in it. So within one
engine pass DETECTION ORDER IS A RANK BY HIDDEN TOTAL, and it reached a
scoped reader five ways: the pattern list (`ORDER BY detected_at DESC`),
the cycle list (`started_at DESC`), the sub-second part of every learning
timestamp, the caller-controlled `?limit=` on `/api/outcomes/patterns`
(cut in SQL over the exact instant, `limit=1` returned the cohort with the
SMALLEST tenant total), and the order patterns are pushed to — and
therefore cited by — a Site Manager. Demonstrated: a site-A reader who
knows POWER_CYCLE's 15 attempts are all their own read whether hidden
BMC_RESET totalled more or fewer than 15 from which row came first. For a
reader who is not tenant-wide: patterns, cycles, the pushed payload and the
pattern citations inside `evidence_cited` are ordered by what is SHOWN
(floored instant, then the cohort the row names; a random id only makes
the order total); learning timestamps are FLOORED TO THE MINUTE, stored
type kept; and `limit` is applied AFTER projection. *Stated limit:* a pass
that straddles a minute boundary still orders its two halves — the pass is
in-memory and takes milliseconds, so this is a one-bit residue at roughly
1-in-10⁴ passes, and removing it needs a per-pass instant at WRITE, which
changes stored rows and is not this slice. **Frozen copies** keep what the evaluator
recorded; A30.26 drops a site-scoped entry whose site the reader does not
hold, and S4 bounds the entries that survive. **Proof obligations.** Two
hidden estates that deliberately collapse to the same band, a scoped reader
who holds the same site in both, and byte equality of everything that
reader is returned — values, confidence, rate, order, rank, count,
statement text, payload shape, nested evidence, list length — with a
non-vacuity guard proving the tenant-wide reader DOES tell them apart;
positive controls proving the cohort conclusion stays visible and the
tenant-wide payload is byte-identical to `main`. **Named follow-ups,
deliberately NOT pulled in** — each is a different semantic, none carries
E3-F1's payload: *WHEN, at minute grain and coarser* — `detected_at`,
`last_confirmed_at`, `observation_count`, and the order of rows from
DIFFERENT engine passes say when the tenant concluded something, which is
a property of the conclusion A23 publishes (the sub-minute part was proven
to carry a hidden rank and IS in this slice, above); *the unprojected
window* — the signal (500) and cycle (200) repositories cut in SQL before
projection, which matters only to a tenant holding more rows than that, and
is not caller-controlled; *the Site Manager's pattern store is keyed by
pattern id with no site* — on a multi-site Site Manager (E1.3) the last
push wins, so after this slice the row holds ONE receiving site's bounded
payload (the conclusion, and at most that site's own count) rather than a
row per site; nothing reads it back through a site-scoped API today, and
making it per-site is a Site Manager schema change; *(closed by A30.29, below — the
independent review showed the site's reasoning consuming another site's
projection is a live disclosure path, not a storage nicety)*; *predictive `cohort_failure_rate` / `outcomes_considered`* are
tenant-wide OUTCOME statistics — S3-E2's family, decided by R6 and
implemented with it; *Site Manager explanation free text* other than a
pattern citation is model- or rule-authored prose about the reader's own
device. *(This reasoning was WRONG and is corrected by A30.29: the prose is
generated from a prompt that carried the unbounded pattern description.)* **E1 and E2 are NOT implemented by this slice.** **General B0b is
NOT changed and PR #48 is not touched**; when it consumes `main` it takes
this projection for attention's signals as it takes S3's composer.
**Unchanged:** the permission vocabulary (25), `ROLE_PERMISSIONS`,
`MACHINE_PRINCIPAL_CEILING`, `MACHINE_SURFACE` (13), every route (zero
added; ONE treatment re-declared, UNSCOPED → READ_SCOPED), every migration
head (CC `0026`, Console `0004`, SM `0010`), `resolve()`, `permits()`,
`read_reach`, every stored row, every decision path, S1, S2, S3. Only
readers who are not tenant-wide change, and only by reading LESS. B0c, B1,
B2 and the taxonomy remain not started.

**A30.29 — A6-4B0b-S4 remediation: generated content inherits the
projection it was generated from (decided: Vinod, 2026-09-22; closes the
independent review's HIGH on PR #51 at `540a0cd`).** *Ordering truth: the
review verdict was FIX BEFORE MERGE; the finding was reproduced by
execution before this text was written; this text is committed before the
remediation code.* **The HIGH.** A30.28 bounded a pattern CITATION at read
and left the PROSE the same pattern was quoted into as stored. A Site
Manager feeds every matching fleet pattern's `description` into the
reasoning prompt (`ingest._matching_fleet_patterns` →
`reasoning._build_messages`), persists `summary = completion[:500]`, and
then feeds that summary and the same evidence into the skill prompt whose
YAML is persisted as `yaml_text`. Central Command's `_diagnosis` bounded
`evidence_cited` only and returned `generated.*` verbatim;
`/api/learning/candidates` returned `yaml_text` verbatim. Reproduced on the
S4 estate: a site-A principal read "SEL_CLEAR fails at 65% on Dell R750
across 3 sites (35/54)" through `GET /api/incidents/{id}` and through the
candidate list, at 200. The review overstated one detail — the LLM
provider's `reasoning_steps` are deterministic templates and its
`suggested_action` is always empty — and the correction changes nothing:
the block is DECLARED generated, and a future provider may populate every
field of it. **The window is not "pre-S4 rows".** A Site Manager reloads
its pattern mirror from `sm_fleet_patterns` at boot, so after an upgrade it
keeps generating from an unbounded payload until Central Command re-pushes,
which happens on a restart and not on a clock; and on a multi-site Site
Manager the store is keyed by pattern id alone, so site B's projected
payload overwrites site A's and a site-A diagnosis quotes site B's own
count — a LIVE post-S4 source, not history. **Ratified: Option A —
explicit generation provenance and fail-closed scoped projection. Option
B (withhold generated text from every scoped reader unconditionally) is
REJECTED — it takes LLM Explain from the persona it was built for. A clock
cutoff is REJECTED — the boot reload makes the clock wrong. Regex or text
sanitisation of model output is REJECTED — it cannot be proven.** **The
invariant, LOCKED.** *Generated content inherits the confidentiality
boundary of the evidence projection it was generated from.* A scoped
reader may receive generated content ONLY when HarkenIQ can PROVE that the
generation projection is covered by that reader's CURRENT canonical reach.
Unknown, missing or ambiguous provenance FAILS CLOSED for scoped readers.
Historical canonical content stays stored unchanged; a tenant-wide
authorized reader retains it. **The provenance model.** One durable,
structured marker, `generation_visibility`, describes the AUTHORIZATION
BOUNDARY of the evidence projection an artifact was generated from —
never the evidence: `{"scope": "site" | "tenant", "site_id": <canonical
Central Command site id> | null, "projection_version": 1}`. It carries no
site list, no count, no total, no fault name, no metric, no rationale, no
signal content. Its vocabulary and its algebra live ONCE, in
`harkeniq.generation_provenance`, the package both services already
import, so the writer at the Site Manager and the reader at Central
Command cannot hold two rules: the visibility of an artifact generated
from several inputs is their JOIN — unknown anywhere is unknown; `tenant`
anywhere is `tenant`; one site throughout is that site; two different
sites is `tenant` (only tenant-wide authority covers evidence of more than
one site, and the vocabulary deliberately has no multi-site form). A
marker is never inferred from prose. **Site-generated content.** Central
Command's per-site distribution payload (A30.28 (A)) now MARKS every
pattern as projected for that canonical site. The Site Manager preserves
the marker from the projected pattern through the reasoning evidence to
the diagnosis, the persisted explanation, candidate generation and the
persisted candidate. The visibility of a diagnosis is the join of the
device's own site (the device's telemetry and its own history are that
site's facts) with the marker of every fleet pattern the reasoning
consumed; the candidate generated from that diagnosis and that evidence
carries the same visibility. A device whose site is not bound to a Central
Command identity (E0.2) has no canonical site to name, so nothing is
written and the artifact reads UNKNOWN. **Tenant-generated content.** A
pattern consumed WITHOUT a marker — pushed by a Central Command older than
this amendment, or reloaded from the pre-A30.29 store — is an unbounded
tenant payload, and an artifact generated from it is marked `tenant`. That
is the truth about its inputs, is never downgraded to a site afterwards,
and a non-tenant-wide reader does not receive that artifact's generated
text because the containing incident is otherwise readable. **Historical
and unknown provenance.** No backfill, because none is mechanically
provable: a pre-A30.29 explanation or candidate reads UNKNOWN, a
tenant-wide reader keeps its canonical content, a scoped reader gets the
generated fields WITHHELD. No stored row is rewritten. Independently safe
fields — the incident row itself, its title, status, components,
correlation, `confidence`, `evidence_cited` (A30.28 (B)),
`similar_past_incidents` (the device's own history), a candidate's site,
device, component, validation state and match count — remain available.
**Current reach at read time.** Provenance is evidence of how an artifact
was created; it is NOT an authorization token. At read, the marker is
compared against the reader's CURRENT canonical reach — A30.28's
`LearningView`, which is S2's `read_reach(scope, "fleet.view")`, on every
projection including incident detail under `incident.view` — so a reader
who held site A when the artifact was generated and no longer does is
withheld on the next read, and is shown it again when A is restored.
Nothing hardcodes "reader site == generated site": an org or site-set
reader receives a site-generated artifact when the recorded site is
covered by their current reach; a `device` or `device_class` reader holds
no site in that reach until general B0b (F1, deliberately open) and is
withheld, fail closed, under this contract; contextual ancestry never
synthesises authority (A30.5). **Incident projection.** The policy covers
the ENTIRE generated block — `summary`, `suggested_action`,
`reasoning_steps`, and any field a future provider adds — by naming what
may pass, never what may not: for a reader the marker does not cover, the
block is `{"summary": <one neutral sentence>, "suggested_action": "",
"reasoning_steps": [], "withheld": true}`, and the response never names
the site the marker names. The sentence is the same whether the marker
names another site or is missing, because "recorded for a site you do not
hold" is itself a fact about the hidden estate. Two ADDITIVE fields on the
diagnosis for every reader: `generated.withheld` and
`generation_visibility` (the marker when the reader is covered, `null`
otherwise). **Candidate projection.** `yaml_text` — and the validation
`warnings`, which are derived from it — are withheld the same way
(`yaml_text: ""`, `warnings: []`, `generated_withheld: true`,
`generation_visibility` as above). A candidate being site-owned does NOT
make its generated content site-safe; the generation provenance is
authoritative for this decision. **Generated content and provenance are
one atomic security unit.** A candidate ingest that changes `yaml_text`,
validation `warnings`, or any equivalent protected generated content MUST
replace `generation_visibility` from that same incoming artifact in the
same repository transaction. Missing, null, malformed or otherwise invalid
incoming provenance on changed content stores UNKNOWN (`NULL`) so every
scoped projection fails closed; an earlier marker never survives content it
did not describe. A byte-identical replay may preserve the existing marker,
but identity (`skill_id`) alone never proves content equivalence. Concurrent
same-id writes are serialized so the durable row is always one complete
content/provenance pair, never content from one writer with the other
writer's marker. **Multi-site Site Manager store — fixed
here, not deferred.** A30.28 named `sm_fleet_patterns` keyed by pattern id
as a follow-up; truthful provenance depends on the site's reasoning
consuming the site's own projection, so it is part of this remediation.
`sm_site_fleet_patterns` is keyed `(site_id, pattern_id)` and stores the
marker as pushed; `PushPolicy` resolves the receiving site by Central
Command's site id (E0.2 — an unresolved site is REFUSED, never guessed),
refuses a payload whose marker names a different site than the one it is
being stored under, stores an unmarked payload with `visibility = NULL`
(an older Central Command — the artifact then reads `tenant`), and one
site's push can neither replace nor be replaced by another site's row.
Reasoning for a device consumes ITS site's rows; a legacy
`sm_fleet_patterns` row is consumed only where the site holds no row for
that pattern id, as unmarked evidence, so an upgraded Site Manager
degrades to `tenant`-visibility artifacts rather than to fleet-blind
diagnoses until Central Command re-pushes; a re-push supersedes the legacy
row for that site. The legacy table is not rewritten and not dropped.
`sm_candidate_skills.site_id` (E1.3) had no writer — the snapshot joined
through the device instead — and is written now, from the same resolved
site. **Migration and protocol, additive and rolling-upgrade safe.** SM
migration **0011**: `sm_site_fleet_patterns` (new; FK `sites.id`), and
`sm_candidate_skills.generation_visibility` JSON nullable, no backfill. CC
migration **0027**: `cc_candidate_skills.generation_visibility` JSON
nullable, no backfill. Proto: `CandidateSkill.generation_visibility_json`
(tag 9, additive). The incident explanation is a JSON document at both
services and the marker is a key INSIDE it, written in the same assignment
as the generated text, so an explanation cannot exist with text and
without the provenance of its own creation; no incident migration. *Old
CC / new SM:* the push carries no marker → stored `NULL` → artifacts read
`tenant` → withheld from scoped readers at a new CC, and the old CC's
reads are what they were. *New CC / old SM:* the marker is an unknown key
the old upsert drops; the old SM writes no provenance; the new CC reads
UNKNOWN → withheld. *Old CC ingesting a new SM's snapshot:* tag 9 is an
unknown field it never decodes; the explanation key rides inside the JSON
it stores verbatim and its `_diagnosis` ignores it. Every combination is
either the old behaviour or fail-closed, and each is tested. **Unchanged:**
the permission vocabulary (25), `ROLE_PERMISSIONS`,
`MACHINE_PRINCIPAL_CEILING`, `MACHINE_SURFACE` (13), `ROUTE_CONTRACT`
(zero routes added), `resolve()`, `permits()`, `read_reach`, every
decision path, every S4 projection A30.28 already ratified, S1, S2, S3;
the Site Manager's own site-token API (E1.3's site-authority plane, not a
tenant RBAC surface) returns the site's rows as stored and is out of this
amendment's scope, recorded as a limit. A tenant-wide reader's content is
unchanged and gains two fields. **Proof obligations.** *Historical attack:*
an incident persisted with NO provenance whose `summary` carries
`SECRET_SITE_C` and whose generated fields carry "30 of 40 attempts across
2 sites", and a candidate whose YAML carries the same — a site human, a
device human, a device-class human and a machine principal receive the
row where independently authorized, its safe local facts, and neither
sentinel through list or detail; the tenant-wide reader receives the
original. *New-write matrix:* site-A projected evidence → artifact marked
A; a current covering site reader, an org reader covering A and the
tenant-wide reader see it; a site-B reader and an org reader not covering
A are withheld; revoking A withholds on the next read and restoring A
shows it again. *Multi-site Site Manager:* one Site Manager owning A and
B, pattern P pushed projected for A and for B with distinct sentinels — A's
diagnosis consumed A's projection only, B's consumed B's, neither push
overwrote the other, and each persisted artifact's provenance matches the
projection actually consumed. *Provider-independent:* every generated
field withheld even when a hypothetical provider populates all of them,
including a field this amendment does not name. *Compatibility:* each
producer/consumer pairing above. *Real PostgreSQL:* both upgrades on
databases holding rows, backfilling none; JSONB marker round-trip. *Live:*
the compose gate's multi-site Site Manager receives two per-site rows with
their markers, the historical attack on the live stack, and both live
databases upgraded from the previous heads with rows present. E1 and E2
remain not implemented; general B0b and PR #48 untouched; B0c, B1, B2 and
the taxonomy not started.


**A30.30 — S3-E1 / S3-E2 / S3-E3: architecture ratification CLOSED (decided:
Vinod, 2026-09-21 as R1–R7; closure recorded 2026-09-22, after general B0b
merged and main-verified; docs only, NOT implemented).** A30.27 recorded the
package so that the implementing slice would start from a ratified text, and
sequenced its implementation after general B0b. General B0b (A30.25) merged as
`d69105c` and main-verified on 2026-09-22, so this amendment closes the
ratification DURABLY: the semantics below are the target model, and a later
implementation may not change them silently — any deviation is a new dated
amendment under §9's change control, ratified before the code. *Naming:
S3-E1/E2/E3 throughout, as A30.27 fixed it; "E1/E2/E3" in the ledger and in
review correspondence means these three and never the Enterprise Governance
slices E1.1–E1.4.*

**S3-E1 — the ratified target model:** SITE-LOCAL AUTONOMY ASSESSMENT + CLOSED
GLOBAL SAFETY GATE = FINAL EXECUTION ELIGIBILITY. (a) Site-local autonomy and
global safety are SEPARATE concepts, and stay separate in code, in contracts
and in audit: the local assessment is S3's primitive — the composer over the
target's own site, `build_autonomy(visible_site_ids={target.site_id})` — and
the gate is a distinct, CLOSED, typed set of global safety constraints. (b)
Hidden state MUST NEVER increase machine authority. This is an invariant to be
proven structurally: no condition at a site the target does not belong to may
move an eligibility towards autonomous, and the one non-monotone input the
package found (the preflight's `safety_reported` flipping UNKNOWN → READY when
a hidden site reports) is removed as R5 ratified. (c) Hidden state may REDUCE
execution eligibility only through an explicitly ratified global safety
constraint — a named member of the gate, recorded in this specification by
dated amendment BEFORE it exists in code. (d) The gate's initial
hidden-site-derived membership is EMPTY (A30.27 R2, option A): no hidden-site
condition is a member today, and the two conditions the tenant-wide fold
currently globalises (`error_budget_dropped_back`, `budget_window_exhausted`)
return to the site they belong to — site-local drop-back and error-budget
state REMAINS site-local and never fences another site. (e) Target site not
reported → REQUIRES_APPROVAL: unreported is UNKNOWN, never safe (S5). (f)
Target-site Site Manager stop → the LOCAL assessment is DENIED; a local
denial, not a gate member. (g) A campaign is autonomous only if EVERY site in
its own immutable plan (A18) assesses locally autonomous AND the gate passes;
one non-autonomous site makes the campaign require approval, never the
reverse. (h) Scoped consumers see only BOUNDED global reason codes: the gate
contributes to any projection exactly the typed reason
`GLOBAL_SAFETY_CONSTRAINT` — the intended bounded reason — and under it NO
hidden site identity, count, metric, name or free-text reason, ever; S3's
deletion-equivalence oracle (A30.26) applies to the gate's output as to every
other site-derived fact. (i) S1's multi-target authorization (A30.22/A30.23)
is UNCHANGED: eligibility is what autonomy may do, authorization is who may
decide, and neither substitutes for the other. (j) `fleet.view` remains the
visibility basis for every autonomy fact, on every projection (A30.26). (k)
The activation preflight assesses over the AGENT's own reach, and
`safety_reported` means every in-reach site reported (R5).

**S3-E2 — the ratified dual-evidence model:** `decision_evidence_at_creation`
and `viewer_projected_evidence`, two things with two names. (a) Historical
decision evidence is IMMUTABLE truth: written once when a proposal is governed
(`govern_proposal`), never rewritten to match a viewer's scope, never
re-derived. (b) The current viewer projection MUST respect the viewer's
CURRENT canonical reach — S2's `read_reach`, S3's `AutonomyView`, S4's
`LearningView` — and it is a presentation/read projection, NOT a second
decision record; it carries no authority and is audited as a read, not as a
decision. (c) Generated rationale must not leak hidden historical or site
evidence: rationale is rendered from TYPED facts through the projection
(A30.28's rule for text — re-rendered from projected evidence, never a stored
sentence restating withheld numbers). (d) Current reach remains authoritative
for viewing. (e) Historical evidence is NOT an authorization token: reading
what a decision saw confers nothing about what a viewer may now see or do.

**S3-E3 — A23 semantics RETAINED:** a vendor/model cohort learned signal is
TENANT KNOWLEDGE; a site-scoped learned signal is SCOPE-FILTERED. E3-F1, the
payload-confidentiality defect the package demonstrated, was closed separately
by S4 (A30.28/A30.29), which remains the canonical payload-isolation
implementation. A23 is NOT reopened; no E3 semantic redesign is required.

**Implementation status, stated so it cannot drift:** S3-E1 implementation NOT
STARTED; S3-E2 implementation NOT STARTED; S3-E3 semantic redesign NOT
REQUIRED. S3-E1/E2 change what Central Command DECIDES and could widen
autonomy; when they are implemented it is in their own slice, with its own
boundary ratified by Vinod and its own independent review, AFTER the sequence
below — unless a fail-open needing immediate containment is proven first, in
which case containment is its own slice too. **Next implementation sequence
(ratified order, none started here):** A6-4B0c Machine Read Metering
Completion (A30.12) → A6-4B1 Governed Discovery (A30.13) → A6-4B2 Operational
Context Projection (A30.14); each merges and main-verifies before the next
begins, and they are not combined.

**A6-4B0c boundary preview (the contract the slice must meet; recorded, not
implemented):** meter EVERY explicit machine-readable route; anchor
completeness to `MACHINE_SURFACE` / the canonical route contract, never a
router-prefix assumption; cover Attention and Incidents; make it structurally
difficult to add a machine route without a metering decision (the completeness
guard of A30.12, anchored on `MACHINE_SURFACE`, is the intended mechanism: a
machine route with no decided meter answer fails the suite); add NO
permission, NO machine-ceiling capability and NO new machine route; preserve
A6-4A's narrowing (A29) and S1/S2/S3/S4/B0b. A correctly-bound runtime can
receive 429 for the first time, so the window is set against real gate traffic
before it lands.

**Follow-ups preserved, all NON-BLOCKING, none implemented here:** site-less
incident hardening (PR #48 review, A); `governing_policy()` target-resolution
consistency (B); the PostgreSQL fixture / `alembic_version` test debt (C);
candidate identity strengthening (`skill_id` alone never proves content
equivalence); the fixed pre-projection SQL windows; minute-boundary timestamp
residue; `observation_count` semantics at minute grain; the predictive cohort
fields (`cohort_failure_rate` / `outcomes_considered`); machine Attention
live-gate coverage; the `test_directives.py` infrastructure hang; Console
Vitest and the DSN-gated PostgreSQL proofs not run in CI; D3 / D6 / D9 / R7 /
D10; `test_warranty.py`'s 2027-01-01. **Unchanged by this amendment:** no
production code, no migration, no route, no permission, no ceiling; CC head
`0027`, SM `0011`, Console `0004`.

**A30.31 — A6-4B0c: machine read metering completion (implements A30.12 under
the A30.30 preview; boundary directed by Vinod's B0c brief, 2026-09-23;
recorded BEFORE the code).** **The inventory, measured on unmodified `main`
(`f291af3`), not inherited.** `MACHINE_SURFACE` declares 13 routes: 12 machine
READS — nine on job `self` (`GET /api/operational-agents/{agent_id}` and its
`/runtime`, `/identity`, `/preflight`, `/dry-run`, `/ingress`, `/proposals`,
`/proposals/{proposal_id}`, `/submissions/{submission_id}`), one on
`attention` (`GET /api/attention/`) and two on `incidents` (`GET
/api/incidents/`, `GET /api/incidents/{incident_id}`) — and ONE machine WRITE,
`POST /api/operational-agents/{agent_id}/proposals`, metered by A24.13's
attempt ledger. The other 85 declared routes are human-only; there are no
internal routes (A29.5) and two public ones (`/healthz`, `/metrics`).
Served-read metering held on 9 of 12: each `self` read charged exactly one
read, and the three F3 routes charged NOTHING — served (200), not found (404)
and malformed (422) alike. **Two findings beyond F3, both reproduced.** (a) A
malformed or invalid machine read is free on EVERY machine read route, the
nine that meter included: FastAPI resolves dependencies, then validates query
parameters, and only then calls the handler — so `GET .../proposals?limit=99999`
answered 422 at zero reads, because the charge lived in the handler and the
handler never ran. It is the free authenticated refusal A25.10 closed, left
open on the whole plane. (b) 77 of the 98 declared routes declare their guard
TWICE — `dependencies=[Depends(require_permission(p))]` and
`user=Depends(require_permission(p))` — and each `require_permission(p)` call
returns a NEW closure, so FastAPI's dependency cache does not merge them and
the guard runs twice per request. The guard is nonetheless the plane's one
choke point: every CC route crosses `require_permission` or
`require_any_permission`, both of which call `enforce_route_surface`, except
the write, which consumes `evaluate_route_surface` inside its attempt ledger
(A29.15). A handler-level charge also left two unreachable charge sites on
human routes (the binding catalogue and the agent listing) that would
double-meter the moment either were re-declared on the plane. **The invariant,
LOCKED.** Every authenticated machine request that crosses the route-surface
guard is charged exactly ONE read to the (tenant, agent, window) bucket its
validated token names, BEFORE any decision about surface eligibility,
permission, scope, object or payload — served, refused, not found, malformed
or throttled alike — with one exception: a SERVED attempt-metered write, whose
accounting remains the A24.13 ledger and never the read window (A25.6). An
unauthenticated request has no bucket and is not metered; the bucket is the
token's (A25.10). **The mechanism.** (1) The meter moves to the choke point.
`enforce_route_surface` charges every machine request before it decides, so a
served read of ANY on-plane route is metered without that route knowing —
`api/attention.py` and `api/incidents.py` are not modified — and no handler
meters itself; the `self` helpers keep the self rule and lose the charge. (2)
One entry point. `harkeniq_cc.read_meter.meter_machine_read` is idempotent per
request (a memo on the request's own state, which a caller cannot set), so the
doubly-declared guard charges once; the charge it wraps is A25's
`_charge_machine_read`, moved UNCHANGED out of the Operational Agent router —
token-derived bucket, its own transaction, the window computed once and
returned — and it has exactly one caller. (3) The meter is DECLARED.
`route_contract.JOB_METER` gives every typed machine job exactly one meter —
`self`, `attention` and `incidents` → `read` (A25.6's windowed counter);
`proposals` → `attempt` (A24.13) — so a route's meter is decided by the job it
already declares, `machine_meter(method, path)` derives it, and the guard
consults it at runtime: no second, hand-kept route list exists to drift. (4)
The completeness guard. `route_contract.meter_census(app)`, runtime code beside
A23's `census()`, walks the RUNNING app's dependency trees and is anchored on
`MACHINE_SURFACE` and `ROUTE_CONTRACT`, never on a router prefix. It fails
when: a machine job has no meter; a read-metered route is not a `GET` or no
guard on its path crosses the meter; a handler meters itself; the
attempt-metered write is a `GET`, carries the read-plane guard (its refusals
would be read-charged ahead of the attempt ledger, A29.15) or does not consume
the attempt ledger and the surface decision; or ANY other declared route
crosses no guard at all — a route that would be machine-reachable in fact,
unrefused and unmetered, so A29.3's default deny becomes a property of the
running app rather than of the declaration alone. A source-level test pins
the one path: `_charge_machine_read` is called only by `meter_machine_read`,
which is called only by the guard. **What each outcome records.** Served
machine read: one read charged. Not found or narrowed (404): one read, exactly
what a 200 costs; the durable row names no resource — it has no free-text
column. Cross-agent (403 by the self rule): one read, the `cross_agent`
counter as today. Permission refused after the surface admits (unreachable for
today's routes, since `REQUIRED_READS` gives every agent `fleet.view`): one
read and the `permission` refusal counter. Route-surface refusal (403): one
read plus the A29.16 evidence on the SAME window row (`surface_refused`, the
closed per-reason column, `last_surface_refused_at`). Throttled (429): the
counter moves before the comparison, so reads beyond the limit in a window ARE
the 429s, plus the rate-limited counters; no evidence row and no payload.
Malformed or invalid (422): one read (was free). Unauthenticated or invalid
token (401): not metered; `agent_identity.auth_failed` is audited where the
subject maps to a known identity, unchanged. A person: nothing, anywhere.
Polling never enters the audit chain (A25.6). **Attribution.** Durable
attribution is the A25.10 bucket — tenant, Operational Agent, window — which is
the attribution an operator needs to find a runtime (A29.16). The ROUTE/JOB is
attributed at service level by one bounded counter family,
`harkeniq_cc_machine_reads_metered_total`, which moves on every durable charge
(a 429 included) and is labelled by the route's declared job (`self`,
`attention`, `incidents`), `off_plane` for a refused off-plane route, and
`other` — a closed vocabulary derived from `JOB_METER`, registered,
carrying no tenant, agent, site, device or path (A25.11). Durable per-(agent,
job) attribution would need a new column or key, which A30.15 forbids in B0;
it is recorded as a follow-up, not built, and no migration is made. **The
window.** Unchanged: 120 reads per 60 s per agent, ONE allowance shared by
every machine read (A25.10), so no route is the unmetered substitute for
another. A correctly-bound runtime can now receive 429 from Attention and
Incidents for the first time; the compose gate measures every real machine
runtime's peak window before this lands and asserts that no correctly-bound
runtime in it REACHES the limit (only the one it throttles on purpose may).
Measured before landing on three wiped runs of the fixed gate (two local, one
CI at `a5c43ad`): across 9 machine runtimes the busiest peaked at 32–47 of 120
reads in one window (26–39%), so the window is unchanged. **Unchanged:** the permission vocabulary
(25), `ROLE_PERMISSIONS`, `MACHINE_PRINCIPAL_CEILING` `{fleet.view,
incident.view, proposal.submit}`, `MACHINE_SURFACE` (13; 3 machine-only),
`ROUTE_CONTRACT` (98; 0 routes added), `evaluate_route_surface`, the refusal
evidence columns and reason vocabulary, every response shape, every handler's
authorization, the write and its attempt ledger, human reads (no charge, no
429, no shape change), S1–S4, B0b, A23 and A26; no migration (CC `0027`, SM
`0011`, Console `0004`). **OUT, and recorded as follow-ups:** durable per-job
attribution (a schema amendment); the dry-run's pre-existing
existence-before-self ordering (a nonexistent agent id answers 404 where
another agent's answers 403 — charged, so bounded, and every other `self`
route asks the self rule first); a schema-invalid submission BODY refused
before the attempt ledger (already recorded, bounded by the 16 KiB pre-parse
ceiling); B1, B2, S3-E1/E2 and the taxonomy, none started.

**A30.32 — A6-4B1: governed capability and parameter discovery (implements
A30.13; architecture checkpoint delivered 2026-09-24 and RATIFIED by Vinod
the same day as D1–D6 with two semantic amendments; recorded BEFORE the
code).** **What the checkpoint measured on unmodified `main` (`8c561e2`).**
Every source B1 composes already exists: `action_facts()` (risk,
reversibility, inverse, implementing protocols and the A22.2 parameter
contract, read from each protocol's own declaration),
`load_capability_registry` (per-class reach over a principal's devices),
the tenant condition catalogue with `CAMPAIGN_ONLY_CLASSES`, the agent's A0
bindings, and `load_autonomy_contract(reach=…)` followed by
`effective_disposition` (S3). No existing route could be reused: the
machine agent view computes approval completion for up to 50 proposals,
answers 404 to an agent whose grants have all lapsed (A30.20), publishes
`scope.rules` (grant construction, against A29.9) and covers bound classes
only; `/api/autonomy/`, `/api/capabilities/*` and `/api/scope-grants/me`
are human compositions; the dry-run stays the DECISION preview (A22.14)
and the only source of `candidate_ref`. Findings, those marked reproduced
re-executed against the pure functions: **G1** (= F5) the mandatory
`autonomy` binding reaches nothing, and the builder catalogue still names
`/api/autonomy`, which is human; **G2** (reproduced) INTERFACE_DISABLE is
catalogue-addressable and risk `high`, so it is DENIED and every agent
proposal for it is stored `blocked` and never reaches a human, while A21.5
says newly addressable classes require a named human — the gate "proved"
A21.5 through the tenant contract's `approval.required`, which is true
even for a denied class; **G3** (reproduced) `parameter_contract` reports
FIRMWARE_UPDATE `agent_resolvable: true` while `resolve_action_params`
refuses it — `target_version` is supplied only by campaign orchestration —
and reports an unknown class resolvable while `resolve_action_params`
refuses it; **G4** (reproduced) the agent view's `capability.implemented`
is hard-coded `true` (INTERFACE_RESET and CLEAR_COUNTERS read `true` there
and `false` in `action_facts()`), and its `requires_approval` is `true` for
a denied class; **G5** `govern_proposal`'s `capability_unimplemented`
refusal is unreachable, because the contract describes all 14 classes;
**G6** an agent grant may carry a `permission_subset` that narrows its
HTTP read reach and is ignored by its WHERE-only operational reach; **G7**
the machine agent view publishes `scope.rules`; **G8** the parameter
contract has no canonical enums or ranges — constraints are prose; **G9**
the human catalogue route's `reachable_devices` is always null (read, never
written on that path); **G10** two species vocabularies, neither
validated (= F6); **G11** per-input freshness cannot be published without
enumerating devices.

**D1 — RATIFIED: one machine-only route on job `autonomy` (J1).**
`GET /api/operational-agents/{agent_id}/discovery`: permission `fleet.view`,
treatment `READ_SCOPED`, surface `MACHINE`, job `autonomy`, metered by the
EXISTING B0c read meter at the route guard (exactly one read, charged
before any decision), self-only, served `Cache-Control: no-store` with no
ETag. The job is the existing A0 read binding `autonomy`, which
`REQUIRED_READS` has injected into every agent since A0 and which reached
nothing after A6-4A (F5): the mandatory binding now names a real route, so
G1 closes and the builder catalogue's description of it is corrected. No
new permission, no ceiling change, no migration, no persistence. The route
enters `MACHINE_SURFACE`, `JOB_METER` and `meter_census` by declaration
alone. Expected counts, to be VERIFIED rather than forced:
`ROUTE_CONTRACT` 98 → 99, `MACHINE_SURFACE` 13 → 14, machine-only routes
3 → 4, machine jobs 4 → 5. Any material deviation from this route stops
for ratification.

**D2 — RATIFIED WITH AMENDMENT: the third fact is ADDRESSABLE, and the
field name `reachable` is not published.** The checkpoint named fact 3
`reachable`, but its semantic is catalogue addressability, not execution
reach, and "reachable" invites exactly that confusion. The field is
`addressable`, and it means one thing: **the action class is addressable
by the tenant's ENABLED condition catalogue, or by the campaign-only
mechanism** (`CAMPAIGN_ONLY_CLASSES`). It MUST NOT mean in scope,
permitted, autonomous, executable or currently operable, and the response
states the path (`condition_catalogue` | `campaign_only` | `none`) so a
campaign-only class is never read as agent-proposable. The rest of D2 as
checkpointed: in effective scope is measured over the agent's READABLE
reach; currently operable includes the node allow list as a reported
blocker and excludes live safety state and halted Site Manager sites;
self-scope is published as reach sets, never construction.

**D3 — RATIFIED WITH AMENDMENT: governance over the agent's own reach, and
no optimistic conclusion before S3-E1.** Governance is composed over the
Operational Agent's own canonical current reach with S3 semantics —
`load_autonomy_contract(reach=read_reach(scope, "fleet.view"))`, then
`effective_disposition` — never over the tenant (a tenant-wide composition
would break A30.26's deletion equivalence). **The amendment:** until S3-E1
is implemented, admission still folds TENANT-WIDE state (A30.26's internal
decision paths), so a machine whose reach is not tenant-wide must never be
told that approval is definitively not required when hidden tenant state
may still add a restriction at admission. The approval requirement is a
CLOSED state of four values:

* `required` — a definitive current conclusion;
* `not_required` — only when the conclusion is definitive under current
  production semantics;
* `unknown` — no approval requirement is visible within the discovery
  composition, but current pre-E1 admission semantics may still add one;
* `not_applicable` — denied, or not applicable to this agent.

`currently_operable.state` is likewise closed — `operable`,
`not_operable`, `unknown` — and for a non-tenant-wide machine before S3-E1
a known blocker is `not_operable`, definitive operability is `operable`,
and anything else is `unknown`: `operable` is never published from local,
current-reach evidence alone when hidden current-production admission
semantics may still restrict progression. Because a condition outside the
agent's reach can only ADD a restriction at admission (A30.26: more sites
fold more blocking conditions, and DENIED arises only from tenant-level
facts every reader sees), a composed `requires_approval` is definitive
(`required`) and a composed `denied` is definitive (`not_applicable`);
only a composed `autonomous` can be overturned, so only it becomes
`unknown`. When S3-E1 lands, B1 consumes its definitive split —
site-local assessment plus the closed global gate — and these `unknown`
states reduce; nothing of S3-E1 is implemented here.

**D4 — RATIFIED: two truthfulness fixes to the sources B1 publishes
from, and no more.** (a) `parameter_contract` agrees with
`resolve_action_params`: a class is `agent_resolvable` exactly when some
reported evidence lets `resolve_action_params` build its payload. A
required parameter that only campaign orchestration supplies is
unsatisfiable for an agent, as a required `unavailable` one already was,
and an unknown class resolves nothing. FIRMWARE_UPDATE therefore reads
`agent_resolvable: false` with its reason named — on `/api/capabilities/`,
in discovery, and in the activation preflight's `capabilities` dimension
(a truthful WARN for an agent bound to it beside other classes, BLOCKED
for one bound to nothing else, because it would propose nothing). No
parameter, enum or range is invented. (b) The agent view sources
`capability.implemented` from `action_facts()` rather than hard-coding
`true`, and an unimplemented class reads reach `unimplemented` there, as
it does in the Registry; this changes output only for an agent bound to
an unimplemented class before A17's binding check. The denied-class
`requires_approval` half of G4 is NOT changed (it is G2's, recorded).

**D5 — RATIFIED: the canonical autonomy actor-species declaration.**
`autonomy.py`, which owns the field, declares `ACTOR_HUMAN = "human"`,
`ACTOR_AGENT = "agent"`, `ACTOR_CAMPAIGN = "campaign"` and the closed
`ACTOR_SPECIES`; `build_autonomy` refuses any other value; `actor.py`
gains the one mapping `actor_species_of(user)`, derived from the existing
`UserContext.species` (`user` → `human`, `agent` → `agent`, anything else
refused). All ten literals are replaced by the constants with identical
values — seven `actor_species=` arguments and three `species` fields on
agent payloads — so no output changes. A structural test refuses a new
literal. `UserContext.species` is unchanged; this is NOT a second identity
system.

**D6 — RATIFIED: machine-only.** Discovery is served to a machine
principal reading ITSELF. A person is refused by the route surface (A29);
human parity is a future PX concern.

**The locked contract: eight facts, kept separate.** Per action class —
every governed class, plus any bound class the platform does not govern —
the response carries eight independent facts and NEVER synthesizes
`allowed`, `authorized`, `can_execute`, `safe_to_execute` or any
equivalent authority boolean:

1. `exists` — the class is in the governed `ActionType` vocabulary;
2. `implemented` — `action_facts()`: a protocol this build ships
   implements it, and which;
3. `addressable` — D2, above, with its path and the enabled condition
   subsystems that name it;
4. `bound` — an A0 `action_class` binding names it;
5. `in_effective_scope` — `load_capability_registry(scope=read_reach(scope,
   "fleet.view"))`: counts of devices in the agent's readable reach, of
   those implementing the class, of those whose node allow list permits it,
   and of those undeclared, with one state from the agent view's existing
   vocabulary (`unimplemented`, `no_devices_in_scope`, `available`,
   `not_permitted_on_any_node`, `no_effective_reach`, `unknown`), derived by
   ONE function the agent view shares;
6. `governance` — for a bound, governed class: the conclusion
   (`autonomous` | `requires_approval` | `denied`) of `effective_disposition`
   over the reach-composed contract, the contract's own blocking reason
   codes (an allow-list of codes that already exist, with a site id only
   for an in-reach site and never a fault-domain id or free text), whether
   the class is budget-mapped, its grant level, and whether it is
   never budget-grantable; `null` for an unbound or ungoverned class;
7. `approval_required` — D3's closed state with a closed basis;
8. `currently_operable` — D3's closed state with the closed codes that
   block it or leave it unknown, a Central Command readiness conclusion at
   `generated_at` and never a statement that execution will succeed.

**The authority rule.** Discovery is descriptive only; nothing in the
response is authority. At proposal `govern_proposal` re-derives every
fact, at approval the approval policy is evaluated normally, at dispatch
current authorization is revalidated (A30.17), and at the node its own
funnel remains final. No discovery field, digest, timestamp, version or
response object is accepted back as authorization proof: the response
carries no `candidate_ref`, digest, signature or ETag, and the ingress
request model forbids every field it does not name (A24.2).

**Self-scope.** Only `read_reach(scope, "fleet.view")`, published as
effective reach in its NATIVE types — `tenant_wide`, the org units, sites,
devices and device classes the reach covers, each list the reach of its own
type — with a device count, and never flattened into `site_ids`: a device-
or class-scoped agent's containing sites are not listed and no contextual
site authority is synthesized (A30.5). Withheld: `scope.rules`, raw grants,
permission subsets, org-unit paths and grant construction, contextual
units, inert targets and reasons, `previously_granted`, `synthesis` and
`administered` (A29.9). An agent whose grants have all lapsed is answered
by the self rule alone — 200 with empty reach — which is the A30.20
alignment of machine self-visibility.

**Parameters.** `ACTION_PARAMETERS` through `parameter_contract` is the
client contract: names, types, required flags, supplying source, prose
constraints and the missing input where one is unavailable. Parameters are
resolved by Central Command from reported evidence and the ingress accepts
none (A24.2), so executor defaults are not published. No enum or range is
invented (G8).

**S3 and S4.** The governance composition passes A30.26's deletion
equivalence over the agent's authorized sites; the discovery caller of
`load_autonomy_contract` names its reader, so the set of whole-tenant
decision paths is unchanged. Discovery contains no learned signal,
pattern, outcome evidence, advancement story, generated text or any other
S4-protected payload — the contract's `learning`, `evidence` and
`advancement` are never named — and structural tests assert it.

**What B1 does not do.** It does not pull in G2 (the denied-class policy
inconsistency), G5, G6, G7 or G9, S3-E1 or S3-E2, the datacenter taxonomy,
or B2 (attention and incident projections); each is recorded and each
stops the slice for a report if it becomes a direct blocker for truthful
discovery. It changes no admission, approval, dispatch or node behaviour,
and no human read other than the corrected catalogue description and D4's
two source fixes. **Unchanged:** the permission vocabulary (25),
`ROLE_PERMISSIONS`, `MACHINE_PRINCIPAL_CEILING` `{fleet.view,
incident.view, proposal.submit}`, the machine permissions a binding
implies, every existing route, response shape and handler's
authorization, `resolve()`, `permits()`, `read_reach`, S1–S4, B0b, B0c,
A23 and A26; no migration (CC `0027`, SM `0011`, Console `0004`).
