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
| **R3 — Intelligence + Autonomy** | Learning loop (R-C1) + skill distribution; OS-level signals (syslog/dmesg/mcelog); expanded allow-list (SEL clear, BMC reset, power cycle) with autonomy budgets + error-budget drop-back + stop switch (§2); LLM Explain + skill generation; mesh tier/quorum/claims/fencing (harken-mesh design, M1–M5 resolved first); `harken diagnose` closed | Re-sliced in a dated amendment when R2b ships, from doc 04 R2/R3 rows + §8 answers |
| **R4 — Fleet Intelligence** | Cross-tenant anonymized learning, vendor reliability comparison (the neutral moat), predictive maintenance, marketplace | Later; specced by amendment |

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
| OQ-4 | Dual-tier fault injection on real hardware without outage (mesh success criterion 1) | TODOS M1 | R3 |
| OQ-5 | Release action allow-list for autonomy (beyond R1's LED/diagnostics/fan-reset) | TODOS M2 | R3 |
| OQ-6 | Authorization lease duration vs partition detection time | TODOS M3 | R3 |
| OQ-7 | Baseline confidence refusal threshold (propose-vs-act cutoff) | TODOS M4 | R3 |
| OQ-8 | Node identity, key issuance, rotation, revocation (signed claims/actions) | TODOS M5 | R2b (PKI/license groundwork) → R3 (signing) |
| OQ-9 | Peer-set assignment where topology discovery is absent | TODOS M6 | R2a — **answered, A1.1** |
| OQ-10 | Node resource ceilings, enforced + observable | TODOS M7 | R3 |
| OQ-11 | Correlated-conclusion suppression (shared upstream cause) | TODOS M8 | R2a (parent incidents) / R3 (action suppression) |
| OQ-12 | Coverage-map presentation: silent device = unobserved, not healthy | TODOS M9 | R2a |
| OQ-13 | Two-device correlation probe: R3 or R4? | TODOS M10 | R3 (decide at slice start) |
| OQ-14 | Credential model: SM credential broker vs local encrypted config; rotation (doc 03) | Platform-Design; TODOS C1–C16 | R3 — **rescheduled, A1.3** |
| OQ-15 | Application-layer symptom source for cross-layer correlation (Prometheus scrape? logs?) | Platform-Design | R3 |
| OQ-16 | Non-Redfish device coverage (Cisco NX gRPC, OneFS REST, SNMP/IPMI fallback) | Platform-Design; telemetry matrix | R2a poll-path (R-S1) minimal / R4 broad |
| OQ-17 | Per-node price point per currency | PRD §9 | R2b (config), business decision |
| OQ-18 | Air-gapped LLM (model, GPU floor) | Platform-Design | R3 |
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
