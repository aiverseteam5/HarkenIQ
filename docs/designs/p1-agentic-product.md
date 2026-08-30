# P1 — Agentic Product (design record)

**Dated: 2026-08-29 · Status: RATIFIED (decisions D1–D6, Vinod) · Branch: `feat/p1-agentic-product`**

This document puts already-made decisions into the repository. It does not
re-litigate P0, A11–A14, OQ-23/24/25, or the ratified architecture. Full
analysis lives in the session artifact series (Reconciliation → Target
Architecture → Build-vs-Adopt → Final Assessment → P1 Assessment → P1
Blueprint → Agentic Model); this file is the authoritative in-repo starting
point for P1 development.

---

## 1. Ratified product thesis

**HarkenIQ is a governed agentic capability platform for heterogeneous
physical infrastructure.** Humans and agents use the same underlying
capabilities. The UI/Consoles are governance and operational surfaces, not
the product boundary; the capabilities, APIs, services, permissions, tenant
boundaries, approval mechanisms, agents and execution layer are the product.

The authorization model — for every actor, always:

```
tenant scope → RBAC → capability → policy → approval where required
→ execution → audit → outcome → learning → autonomy progression
```

Agents are another actor/channel in this governed system, not a separate
authorization universe. **Rule: one tenant model + one RBAC model + one
governed capability layer + one authorization path.** No parallel agent
authorization architecture, ever.

## 2. Product surfaces

| Surface | Responsibility | Must not |
|---|---|---|
| **Platform (Console)** | HarkenIQ-the-business: tenant lifecycle + registry + explicit entry, subscriptions/billing, licensing, support + access governance, marketplace review, releases, platform audit | Hold/render tenant operational data (R-H4); host operational APIs; auto-enter a tenant |
| **Central Command** (customer-facing label may be "Command Center"; spec name unchanged — D4) | The tenant's operational control plane: fleet intelligence, learning, authorization, budgets, approvals, audit; the layer humans (via Console proxy) and agents (via API/MCP, later) address | Serve two tenants (OQ-26); per-device decisions; any caller outside RBAC/approval/budget/audit |
| **Tenant Console** | The customer's operational workspace: an explicitly tenant-scoped window (`/t/{id}` URL context, visible header) onto that tenant's CC | Store operational state; bypass CC to reach SM; blur tenant context |
| **Site Manager** | Site brain: correlation, diagnosis narrative, lease signing, command brokering, directive delivery, firmware wave execution; its dashboard is break-glass local ops | Become a tenant/machine product surface; replace node intelligence |
| **Node / Agent** | Harken Node = the one runtime + operational identity + final safety gate. Operational Agent (A0+) = governed declarative bundle + identity. External agent (A3+) = credentialed consumer | A second runtime; frameworks; skipping any gate a human faces |

## 3. Capability registry

Post-P0 inventory (main @ `4dabdd4`). Statuses: **existing / partial /
wiring / missing / obsolete / internal**. "CC exposure" = reachable through
the Console proxy. Agent/MCP readiness refers to the future consumer surface
(A3/A5+); *no MCP exists today by design*.

| Capability (purpose) | API · owning service | Scope · permission | Approval · audit | UI / CC / Agent / MCP | Deps | Status → next action |
|---|---|---|---|---|---|---|
| Fleet view (what do I have, how healthy) | `/api/fleet[/{id}]` · CC | tenant · fleet.view | — · — | ✓ / ✓ / read-ready / Tier-1 | — | existing → drawer gains risk+CVE (S1) |
| Incidents + diagnosis (what is wrong and why) | SM `/api/incidents` + **CC `/api/incidents` (S4)** | tenant · incident.view | — · — | **✓ (S4)** / ✓ / read-ready, provenance-marked / Tier-1 `explain_incident` | — | **S4 landed**: proto +5 additive fields, `cc_incidents` (0006), absence-inference per D3, pseudo-incidents retired |
| Approvals (human gate on actions) | `/api/approvals/*` · CC | tenant · action.approve | IS the gate · chained | ✓ / ✓ / propose-target / Tier-2 | — | existing (P0-proven) → diagnosis excerpt rides S4 |
| Action execution (14 types, gate funnel) | node `_execute_gated` via SM directives | device · lease/approval | per class · 4 phases | via queue / — / core loop / never direct | — | existing → consumed by A1 |
| Autonomy budgets + stop switch (the trust ladder) | `/api/policies/autonomy`, `/stop-switch` · CC | tenant · reads fleet.view (D2, **landed S1**); writes site.manage | mutation human-only · chained | partial (Overview strip, **S1**) / ✓ / posture-read / Tier-1 + `activate_stop_switch` | — | **S1 landed** (read-split + strip); **S5 landed** the contract below |
| **Autonomy contract** (may this run without a human, and why) | **`GET /api/autonomy` · CC (S5)** | tenant · fleet.view (D2 read-split) | none — read-only, confers no authority · — | **✓ (S5)** / ✓ / **decision input** / Tier-1 | safety transport | **S5 landed**: declared ladder (`autonomy.py`, also THE source for `policy_push`), per-class disposition + blocking conditions + evidence + advancement; no mutation surface added |
| **Autonomy safety state** (what has already been withdrawn) | proto `FleetSafetyState` → `cc_safety_state` (0007) | tenant/site · fleet.view via the contract | — · — | via contract / ✓ / **governance input** / later | — | **S5 landed**: suppression + error budgets + site budget remaining leave the SM at last; unreported reads UNKNOWN, never safe |
| **Error-budget demotion** (autonomy withdrawn on evidence) | SM `sm_error_budgets`; recovery at SM `/api/autonomy/error-budget/{type}/recover` | site · site token | automatic demote · recovery audited | via contract / ✓ / read / later | — | **S5 landed**: R3a's model had NO runtime writer and no caller — now folded at `_record_outcome` and enforced in the lease as `propose`. Tenant-plane recovery = registry candidate (§11) |
| Approval policies/groups | `/api/policies/*` · CC | tenant · site.manage | — · chained | ✓ / ✓ / later / later | push unwired | **partial → A2 wires `approval_policies_json` push** |
| Outcomes + patterns (what worked) | `/api/outcomes/*` · CC | tenant · fleet.view | — · — | partial (Reliability) / ✓ / read-ready / Tier-1 | — | existing → Learning surface (S3) |
| Learning loop (candidates, cycles, promotions) | `/api/learning/*` · CC | tenant · fleet.view | promotion = human (marketplace) · — | ✗ / **✓ proxied (S1)** / read-ready / Tier-1 | — | **S1 landed** (reachable at last) → S3 surface; cycles in-process (label honestly; durable P2) |
| Predictive risk (what fails next) | `/api/predictive/risk` · CC | tenant · fleet.view | — · — | per-device drawer (**S1**) / ✓ / read-ready **but not site-scopable** / Tier-1 | site attribution (S2) | **S1 landed** (per-device) → S2 fleet ranking + site attribution |
| CVE exposure (what is vulnerable) | `/api/firmware/*` · CC | tenant · view fleet.view, import site.manage | — · — | per-device drawer (**S1**) / ✓ / read-ready (already carries site_id) / Tier-1 | — | **S1 landed** (per-device) → S2 fleet view; import stays API-only |
| Warranty/lifecycle | `/api/warranty` · CC | tenant · fleet.view (import site.manage) | — · — | ✓ drawer / ✓ / read / later | — | existing |
| Harken Nodes (deployed agents) | `/api/agents` · CC | tenant · fleet.view | — · — | ✓ (truthful since P0; **named + framed S1**) / ✓ / read / read | — | existing |
| Audit + chain verify (prove it) | `/api/audit[/verify]` · CC + Console | per store · audit.view | — · IS audit | entries ✓, **operational verify ✓ (S1)** / ✓ / read / Tier-1 `verify_audit` | — | **S1 landed**; Console-chain verify remains platform-only (backlog §11) |
| Firmware campaigns (waves, halt, rollback) | SM `/api/firmware-campaigns/*` | site · site token | campaign-level human · chained | ✗ / **✗ no CC path** / status-read later / later | proto RPCs | **wiring → S6 CC mediation, WHOLE flow (D6)** |
| Sites | `/api/sites` · CC | tenant · fleet.view (register site.manage) | — · register chained | filter facet (**S1**) / ✓ / read / read | — | **S1 landed**; site rollup consumes it in S2 |
| Skill distribution (marketplace → node) | Console→CC `InstallSkill`→SM directives→node | tenant · skill.install | marketplace review · chained | ✓ / ✓ / A1 reuses / later | — | existing (R5-1/R5-2 proven) |
| Operational Agent (the product noun) | **`/api/operational-agents/*` · CC (A0+A1)** | tenant · reads fleet.view, writes interim site.manage; agent.manage at A2 (matrix review first) | activation human · chained | **✓ (A0+A1)** / ✓ / **is the consumer** / later | S5 contract | **A0+A1 landed**: bundle + scope + bindings + lifecycle (CC 0008), CC-resident evaluator, labelled proposals into the ONE approval queue, `DispatchAction` CC→SM onto the existing directive transport, attribution through to `cc_outcome_history` |
| Machine identity (service accounts) | — (API keys inert; page retired in P0) | tenant · role bundle ≤ ceiling | — · species-labeled | — | Keycloak client credentials | **missing → A3**; remove api-keys routers then |
| MCP adapter | — | tenant · caller's token | same as API · same | — | A3, A4 | **missing (by design) → A5 Tier-1 (5 read tools), A6 Tier-2** |
| NL intent compiler | — | — · zero authority | human approves the compiled form | — | A2 (+A7 prompt/schema) | missing → A7; reuses `LLMProvider` |
| Credential custody + rotation | node-local (no API) | device · — | — · rotation audited | ✗ | — | internal → surface deferred (open item C9) |
| Imports: CVE / warranty / site YAML / signed usage | CC + SM + Console | tenant/site · site.manage/admin | — · — | API-only by design (one usage-upload affordance later, R-H6) | — | existing/internal |
| Metering → billing | CC→Console `/api/internal/usage-events` | tenant · internal key | — · ledger | ✓ / n/a / later reads / later | — | existing (contract fixed in P0, wire-tested) |
| Obsolete: api-keys routers, impersonation routers, `PushSkill` RPC, CC pseudo-incidents | Console / proto / CC | — | — | retired UIs (P0) | — | **obsolete → remove** (api-keys+impersonation at A3; PushSkill P2 cleanup; pseudo-incidents at S4) |

A future engineer's test: *"what capability do we already have that the next
agentic slice should consume?"* — answer from this table; do not invent new
APIs where a row already provides the behavior.

## 4. Capability lifecycle (the product expansion model)

```
OBSERVE      node protocols + skills + baselines; SM ingest; CC fleet cache
UNDERSTAND   verdicts, correlation (5 rules), LLM diagnosis      → S4 surfaces it
RECOMMEND    skill proposals with evidence + rollback semantics
GOVERN       RBAC, budgets, policies, tenant scope               → S5 surfaces it
APPROVE      SM broker → CC routes → named human (P0 gate-proven)
ACT          node gate funnel only (allow-list → preconditions → stop switch
             → lease → blast radius); delivery ≠ authorization
AUDIT        4 hash chains, verify endpoints                     → S1 surfaces verify
OUTCOME      signed outcomes → sm_action_outcomes → cc_outcome_history
LEARN        R-C1: patterns → candidates → validated → promotion rec (95%/50)
                                                                → S3 surfaces it
AUTONOMY     budget levels 0–3 (A10.4) earned per class; demotion automatic;
             promotion human-ratified                            → S5 + A2/A8
```

Every new capability enters at OBSERVE/UNDERSTAND and earns its way down.

## 5. Agentic model

Progression for any capability becoming agent-operated:

```
existing capability → agent-readable (read APIs under RBAC)
→ agent-selectable (bound into an Operational Agent bundle: scope, skills,
  action classes, approval policy, budget)
→ proposal (labeled, evidence-carrying, into the SAME approval queue)
→ approval (human, per class, until earned)
→ execution (node gate funnel — unchanged)
→ audited outcome (attributed to the agent)
→ repeated successful outcomes (95% over 50+, per class)
→ bounded autonomy (budget level raised BY A HUMAN; error budget can lower it
  without one)
```

No separate capability system for agents. The Operational Agent is a
declarative bundle over existing primitives (9 of 12 attributes existed
pre-P1; see the Agentic Model artifact) — configuration, never a runtime.

## 6. Human + agent parity

| | Human | Agent |
|---|---|---|
| Identity | Keycloak user (tenant realm) | Bundle attribution key (`op-agent:<id>@v<n>`); external: Keycloak service account |
| Invocation | Tenant Console / CC API | node pipeline (in-platform) / CC API / MCP (later) |
| Tenant scope | same — structural (one CC per tenant) | same |
| Permissions | same 24-atomic vocabulary, ceiling-bounded | same |
| Approval classes | same (A2.1/A10.4) | same |
| Budgets | n/a (humans approve; they don't consume budgets) | same distributed enforcement CC→SM→node |
| Audit | same chains, actor-labeled | same chains, species-labeled |
| Safety gates | approval never overrides them (A10.3) | identical — delivery is not authorization |

The agent differs in identity and invocation mechanism only — never in the
trust boundary.

## 7. Current agentic frontier (post-P0, main @ `4dabdd4`)

- **A. Already agent-ready:** every CC read capability under real RBAC
  (fleet, agents, outcomes, learning, predictive, CVE, warranty, sites,
  audit+verify, approvals list); the full propose→approve→execute→outcome
  loop (gate-proven); budgets/lease/stop-switch distribution.
- **B. Needs only wiring:** learning proxy prefix (S1); autonomy read-split
  (S1, per D2); incidents to CC (S4: additive proto fields + `cc_incidents`);
  campaign mediation (S6); approval-policy push (A2); audit species labels
  (A1/A3); outcome→agent attribution join (A1).
- **C. Needs capability/bundle definition:** the Operational Agent object +
  registry + lifecycle (A0), binding + deployment + attribution (A1),
  validation/simulation + activation gate + per-agent budgets (A2),
  progression UX (A8).
- **D. Needs machine identity:** Keycloak client-credentials service accounts
  at CC; proposals endpoint; api-keys retirement (A3).
- **E. Needs MCP:** Tier-1 read tools (A5); Tier-2 propose + the single
  Tier-3 exception `activate_stop_switch` (A6). Official SDK, pinned; adapter
  holds no authority of its own.
- **F. Needs new platform capability:** NL intent compiler (A7 — one
  constrained `LLMProvider` call → draft bundle → validated by existing
  `SkillValidator`/scope/ceiling → human-approved form; zero authority);
  durable learning cycles (P2); suppression/error-budget state in snapshots
  (P3).
- **G. Permanently human-only:** budgets/policy mutation, stop-switch
  deactivation, autonomy promotion ratification, users/roles, marketplace
  review, campaign approval, tenant lifecycle, support grants. Structurally
  impossible for agents regardless of configuration: FIRMWARE_*/INTERFACE_*
  via budget (never grantable — `policy_push.py`), anything at L4,
  cross-tenant anything, audit mutation, bypassing node gates.

## 8. P1 slices (approved sequence — do not reorder without evidence)

```
P0 (landed: PR #13, merge 4dabdd4)
→ S1 quick wins: learning proxy prefix · D2 autonomy read-split ·
   audit-verify status line · fleet drawer risk+CVE · Overview enrichment ·
   sites filter facet · agents framing copy
→ S2 Risk & Exposure surface (predictive + CVE; honest insufficient_data)
→ S3 Learning surface (observed outcome → learned pattern → candidate →
   human-approved promotion, visibly distinct; freshness labels)
→ S4 Incidents & Diagnosis (proto +4 additive FleetIncident fields → SM
   populate → cc_incidents + migration 0005 + reconcile-by-absence (D3) →
   /api/incidents (incident.view) → proxy → UI + drill-throughs → retire
   pseudo-incidents)
→ S5 Autonomy — the governed decision boundary for action (LANDED):
   declared ladder as one object shared with policy_push · GET /api/autonomy
   (fleet.view) · FleetSafetyState proto + cc_safety_state (0007) ·
   error-budget demotion made real at the SM and enforced in the lease ·
   Console Autonomy page. See §12.
→ A0+A1 named-agent thesis slice (bundle tables + migration 0006 → CRUD/UI →
   binding + deployment via existing directives → attribution → labeled
   proposals → demo: agent proposes, operator approves, node executes,
   audit shows the agent throughout)
→ S6 Campaigns — WHOLE governed flow (D6): proto RPCs → SM thin delegation →
   CC /api/campaigns → proxy → UI (wave preview, approve, advance, halt)
→ A2 governed autonomy → A3 machine identity → A4 capability register +
   /api/v1 → A5 MCP Tier-1 → A6 MCP Tier-2 → A8 progression UX → A7 NL
   compiler
```

Every slice lands separately with tests + e2e-gate additions; the product is
coherent at any stop point.

## 9. Design principle

**"One capability, one implementation, one governance model; multiple
consumers."** Consumers: Tenant Console, Central Command API, Site Manager
(execution), Node/Agent, MCP, future natural-language interface. A capability
is never duplicated per consumer; a consumer never gets a private
authorization path.

## 10. Future product direction

MCP, customer-created agents, natural language, machine identity, autonomy
and agent bundles are progressively added layers over the same governed
capability model — not separate products. Target experience:

> "Monitor Site 7, investigate abnormal GPU health, and automatically handle
> low-risk remediation. Ask me before anything production-impacting."

compiles to: scope=site-7 + health skills + classes {SEL_CLEAR, BMC_RESET} +
budget level 2 + approval human-for-medium+ — all existing machinery (traced
hop-by-hop in the Agentic Model artifact). S1 implements none of MCP/agents;
it establishes surfaces so those layers add without redesign.

## 11. Open items (genuinely unresolved only)

### Capability-registry candidates (named, sequenced, NOT abandoned)

Recorded per Vinod's S5 sequencing call (2026-08-29): these are deferred
capabilities that stay on the roadmap as candidates for the Operational
Agent's capability registry — not silent drops.

- **The 9 unmapped action classes.** *(A0+A1, 2026-08-30: the APPROVAL
  half is closed — an unmapped class now reads as "always needs a named
  human" rather than "forbidden", so an agent may propose it and a person
  may approve it. What remains open is the AUTONOMY half: whether any of
  them should ever run unattended, which is still the product decision
  below.)* A10.4 maps 5 of the platform's 14 action types. Four are structurally fenced (risk `high`: FIRMWARE_UPDATE,
  FIRMWARE_ROLLBACK, INTERFACE_RESET, INTERFACE_DISABLE). The remaining
  nine — IDENTIFY_LED and COLLECT_DIAGNOSTICS (risk `none`), FAN_RESET,
  CLEAR_COUNTERS, INTERFACE_ENABLE (risk `low`), and the rest — are
  neither granted nor fenced. S5 reports them as `not_budget_mapped`
  rather than widening a boundary inside a slice about boundaries. The
  live stack shows COLLECT_DIAGNOSTICS at 8/8 SUCCESS and still unable to
  run unattended, which is the case for mapping it. **Mapping is a product
  decision, not an evidence threshold** — it needs its own call.

- **Tenant-plane recovery from an error-budget drop-back.** Demotion is
  automatic and now real; recovery lives at the Site Manager
  (`POST /api/autonomy/error-budget/{action_type}/recover`, site token,
  audited). A tenant operator can SEE the drop-back but must use the site
  break-glass to clear it. Closing this needs a one-shot CC→SM command
  verb: `PushPolicy` is convergent and re-applies every poll, so a
  recovery flag riding it would defeat drop-back permanently. That verb
  belongs with A2's governed per-agent control design, not invented here
  as a third CC→SM control path.

- **Suppression re-enable from the tenant plane.** Same shape, same
  reason; SM `POST /api/autonomy/suppression/{domain_id}/re-enable`
  remains the control today.

### Backlog capabilities (discovered during implementation; not built)

- **Tenant-scoped Console audit-chain verification** *(found in S1, 2026-08-29)*.
  A tenant owner or auditor can verify their **operational** chain (Central
  Command, `audit.view`) but cannot verify the **Console governance** chain
  whose entries the Audit page lists — the only verify for that chain is
  `GET /api/admin/audit/verify`, which is `require_super_admin`. Closing this
  needs BOTH a new tenant-scoped endpoint AND a permission decision (does
  `audit.view` on the tenant plane entitle verification of the Console chain
  for that tenant's rows?). Deliberately **not** implemented opportunistically
  in S1; the banner instead names which chain it verifies. Owner: a later
  slice, on an explicit decision.

### Test-infrastructure debt (NOT product architecture)

- **`test_r2a_exit_gate` is load-sensitive** *(observed once, 2026-08-29,
  during S4)*. It boots real agents and a Site Manager in-process with
  compressed timings (0.2s sweeper, 0.5s inference) and a 30s `wait_until`
  budget, so a full-suite run competing with Docker for CPU can miss the
  deadline. Verified unrelated to the S4 change: the only SM edit is inside
  `GetFleetSnapshot`, which that test never calls. Passed six consecutive
  isolated runs and two consecutive full runs afterwards. Deliberately NOT
  "fixed" by inflating the timeout on one observation — the budget is part
  of a ratified exit gate. If it recurs, make the wait budget explicit and
  load-aware rather than larger.

- **The compose gate is not hermetic** *(observed 2026-08-29)*. Docker volumes
  (postgres) survive between runs, so SM actions and CC approval routes
  accumulate. A gate assertion that pins "the thing we just created" must
  select by **state** (first action with `status == "pending"`), never by list
  position — `actions[0]` returns the previous run's approved action, which by
  definition never appears in CC's pending queue, and the C2 step then times
  out for reasons unrelated to the code under test. Fixed in S1 by selecting on
  state. Remaining debt: the gate still has no clean-slate mode; `docker compose
  down -v` is the manual answer. This is test infrastructure, not architecture —
  it changes no product contract.

- **A15 recording:** the Operational Agent object + machine-caller mechanism
  belong in the spec amendment record; timing (now vs. when A0+A1 lands) is
  Vinod's open call (D-A3). This design doc records the decisions either way.
- **agent.manage / agent.view permissions:** required at A2; per A13.4
  sequencing the permission matrix is presented for review before code.
- **C10 (PRD §6):** plain-language fleet Q&A is promised in the free Observe
  tier; NL sits at A7. Narrow the PRD claim or pull a read-only Q&A forward —
  positioning call, unresolved.
- **C9 (PRD §6):** credential rotation is sold as a feature but has no
  surface; deferred deliberately, unscheduled.
- **Ledger truth notes:** R4-0 "Prometheus metrics" (registry unwired — no
  service exposes /metrics) and R6 interface actions (INTERFACE_RESET
  unimplemented; network preconditions unpopulated → fail closed) overstate;
  correct the status ledger at the next milestone entry.
- **Incident resolution reasons:** absence-inference ratified (D3); explicit
  reasons only on a concrete compliance/product requirement.

---

## 12. The S5 autonomy contract (landed 2026-08-29)

`GET /api/autonomy` — Central Command, `fleet.view`, tenant-scoped
structurally, optional `?site_id=` / `?action_type=`. **A stable
machine-readable governance contract, not a Console DTO.** The Console is
its first consumer; the Operational Agent (A0/A1) is its second and gets
nothing extra.

### Three axes, never merged

```
PERMISSION       may this ACTOR address the capability      RBAC, Keycloak
AUTONOMY         may this ACTION CLASS run without a human  this contract
EXECUTION GATES  may this SPECIFIC ACTION run right now     node funnel
```

**Autonomy is not permission, and autonomy is not execution
authorization.** A `disposition` of `autonomous` is a PREDICTION an actor
may plan with. Execution still runs the unchanged funnel: allow-list →
preconditions → stop switch → lease → blast radius.

### What the contract carries

`actor` (identity, species, tenant, may_observe/approve/change_posture) ·
`scope` (tenant, optional site, per-site safety-reporting) · `posture`
(stop switch with who and when, configured level, budget, the declared
`ladder`, device-scoped budgets marked `enforced: false`) ·
`safety_state` (suppressions, error budgets, site stop switches, and
explicitly which sites did NOT report) · `action_classes`, one row per
action type the executor can run, each with: `risk`,
`required_permission`, `granted_at_level`, `never_budget_grantable`,
`disposition` + `disposition_reason`, `blocking_conditions` (scoped
tenant/site/domain), `evidence`, `learning`, `safety`, `approval`,
`advancement`.

### Invariants (pinned by tests, asserted again at the compose gate)

1. **No `high`-risk action is ever budget-grantable, at any level.** Stated
   as a derived rule over `ACTION_RISK`, not a hand-kept list, so a new
   high-risk action is fenced the moment it is classified.
2. **The contract's ladder IS `policy_push`'s mapping.** They were two
   copies; `grants_for_level` is now the single object and a test fails if
   they diverge.
3. **Unreported safety reads UNKNOWN, never safe.** The one direction a
   governance input may not err.
4. **Evidence below 5 outcomes yields no rate**, not a flattering one.
5. **Demotion is automatic; promotion is always human.** Evidence
   qualifies a class; only a person raises the level.
6. **S5 added no mutation surface.** Every autonomy mutation stays on
   `/api/policies/*` at `site.manage`.

### The path an Operational Agent takes

```
token (tenant realm)              -> actor.identity, actor.tenant_id
GET /api/autonomy?site_id=...     -> 403 without fleet.view (same guard as a human)
read disposition                  -> autonomous | requires_approval | denied
read blocking_conditions          -> say WHY in the proposal, not "denied"
read safety.suppressed_domains    -> do not propose into a suppressed domain
read evidence + learning          -> attach real justification
propose into the SAME queue       -> no agent queue, no agent endpoint
a named human approves            -> until the class has earned its level
execute via the node funnel       -> unchanged; the contract authorized nothing
outcome -> cc_outcome_history     -> becomes evidence on the next read
```

### What S5 fixed that was not on its list

R3a ratified the A2.2 error-budget drop-back and R3b-1 declared
`sm_error_budgets`, but **nothing at runtime ever constructed a
`KnowledgeBase`**: the table had no writer, `is_action_type_dropped_back`
had no caller, and a class could fail repeatedly and keep its autonomy on
a running system. S5 folds every terminal outcome at
`ApprovalService._record_outcome` and enforces the result in the lease as
`propose` (drop back to Approve — not `deny`, since the action is still
the right one). Automatic demotion is a ratified safety property; it is
now real.

---

## 13. A0+A1 — the named Operational Agent (landed 2026-08-30)

The product noun becomes an object. An **Operational Agent** is a
declarative bundle over capabilities that already exist:

```
identity      a named, versioned, tenant-owned row; attribution key
              op-agent:<id>@v<n> (design §6), frozen onto every proposal
scope         explicit site / device_class / device rows. No rows, no
              devices: "everything by default" is structurally impossible
capabilities  REFERENCES to governed capabilities (action classes from the
              executor's own ACTION_RISK, reads from the CC surfaces).
              Nothing here defines a capability
policy        a ceiling that can only ever TIGHTEN the tenant's own
```

It is configuration, never a runtime. It holds no credential (machine
identity is A3), it has no API a human lacks, and it reaches nothing its
bundle does not name.

### Where the decision path lives, and why

Ratified by Vinod: **CC-resident evaluator**. The agent's whole
contribution is evidence a device cannot see — fleet-wide outcome rates,
learned signals, cross-site patterns, the autonomy contract, live safety
state, incident diagnosis. All of it is composed at Central Command by
pure functions a browser, an MCP tool and a service account read
identically. An agent anywhere else would re-derive those joins and drift
from what the operator sees.

So the agent **evaluates at CC and executes at the Site Manager**. SM
remains the execution boundary; the node funnel remains the only thing
that authorizes an action. At A3 the evaluator becomes a credentialed
external caller reading the same contracts through the same guards, and
nothing in `operational_agent.py` has to change — that is the test of
whether the seam is real.

### The chain, hop by hop

```
create -> scope -> bind -> ACTIVATE (human, audited, refuses an agent
                                     that could see or do nothing)
       │
evaluate   attention (same ranking the operator sees) + autonomy contract
           + learned signals, filtered by scope and bindings
       │
propose    cc_agent_proposals: rationale, evidence refs, the S5
           disposition AT PROPOSAL TIME and the conditions that produced it
       │
govern     denied            -> blocked, recorded with the reason
           requires_approval -> the SAME /api/approvals queue, same
                                action.approve, same audit vocabulary
           autonomous        -> dispatched, decided_by "autonomy:level-N"
       │
dispatch   DispatchAction (CC->SM, same channel and site token as
           RouteApproval) -> DirectiveService.enqueue_action
       │
execute    node _execute_gated, unchanged, and still able to refuse
       │
attribute  sm_action_outcomes(actor) -> error budget -> FleetOutcome(actor)
           -> cc_outcome_history(actor) -> S5 evidence -> next proposal
```

### The one new authorization rule

A directive now carries `authorization_basis`. `human_approval` may
proceed past an authorization-shaped lease verdict, because that verdict
gates autonomous *initiative* and a human already took it.
`autonomous_grant` may not: the lease is the whole authorization, so its
refusal is final. **Without this, the S5 error-budget drop-back could not
stop an agent**, which is the only thing it exists to do. Every hard gate
(preconditions, stop switch, expired lease, blast radius) refuses both,
as it always did.

### Corrections this slice made to its own model

* **`not_budget_mapped` is about autonomy, not permission.** The first
  implementation read it as "forbidden", which silently removed
  IDENTIFY_LED, COLLECT_DIAGNOSTICS and FAN_RESET — most of the low-risk
  work worth delegating — from anything an agent could ever ask for. It
  now means "always needs a named human", which is what it is. This
  closes the §11 "9 unmapped action classes" gap for the *approval* path
  without widening any autonomy boundary: nothing became autonomous.
* **A tenant stop switch denies every class, mapped or not.** Proposing
  into a stopped tenant would spend a human's decision on work the node
  refuses anyway (A10.3).

### The defect it found

**Directed actions produced no outcome record at all.**
`_run_directed_action` built its `Action` outside the queue that
`_sync_actions` reports from, and `DirectiveService.report_result` wrote
only an audit row. So no `sm_action_outcomes`, no `FleetOutcome`, no
`cc_outcome_history`, no error-budget accounting: **every
firmware-campaign execution since R5-1 was invisible to learning and to
the error budget.** Same shape as the S5 `KnowledgeBase` find and QA-042
— a declared mechanism with no writer on one of its paths. One shared
writer now serves both paths (`harkeniq_sm/outcomes.py`).

### Invariants (tests, and again at the compose gate)

1. An agent's disposition is the tenant's, only ever narrower. An agent
   with ceiling 3 against a tenant at level 0 is granted nothing.
2. A fenced (`high`-risk) class stays denied for every agent at every
   ceiling; those classes keep their own approval paths.
3. Scope fails closed. No scope rows means no devices, never all of them.
4. A bundle may only reference capabilities that already exist; an
   unknown action class or read is refused at write time.
5. Activation is a human act and refuses an agent that could see nothing
   or propose nothing.
6. One approval queue, one permission, one audit vocabulary. There is no
   agent execution surface on the agent router (asserted as a 404).
7. Denial is final (D16); the dedupe key stops a re-proposal.
8. An outcome with no matching agent proposal stays unattributed rather
   than being claimed.

### Deliberately deferred (not dropped)

Per-agent budgets, validation/simulation and the full activation gate
(A2, per §7C); machine identity and the service-account swap (A3); MCP
(A5/A6); the natural-language builder (A7 — the `catalogue` endpoint is
the shape it will compile into). Event-driven evaluation stays on the
roadmap; A1 evaluates on a cadence.

### Also fixed here

The Approvals page decided actions by the approval-route row id rather
than the `action_id` its endpoint takes, sent `{ids, decision:"approve"}`
against a `{action_ids, decision:"approved"}` contract, and rendered five
fields Central Command has never sent. The one queue this slice unifies
had to work before an agent could use it.

---

## 14. E0.1 — the approval policy binds (landed 2026-08-30)

First slice of the **Enterprise Governance Foundation** (E0 + E1 +
Capability Registry), ratified 2026-08-30. Spec amendment **A15**.

### What was wrong

`cc_approval_policies` has carried `approval_mode`, `required_approvers`
and a group link since R2b. The Console has full CRUD for it. The S5
autonomy contract faithfully reports it. **No code path consulted it
when a decision was made** — `ApprovalPolicyRepo` had exactly two
readers, the governance composer (for display) and its own CRUD router.
A tenant could configure dual authorization and receive single
authorization, silently. Fourth instance of the declared-with-no-caller
pattern after `KnowledgeBase` (S5), fleet outcome dictification (QA-042)
and directed-action outcomes (A0+A1).

### The shape of the fix

A decision is now a **set**, not a field. `cc_approval_records` holds one
row per approver per subject (CC migration **0009**);
`cc_approval_routes.decision` remains as a projection for compatibility.
`unique(subject_type, subject_ref, approver_ref)` makes duplicate-approver
prevention a database guarantee rather than a check a later path can
forget.

Judgement lives in the pure `harkeniq_cc/approval_policy.py`: policy
resolution (most specific active match on action type, then device type,
then risk; deterministic ties), required count, group membership, and
the completion rule. Enforcement lives in the shared decision function
both origins already called, so a node action and an Operational Agent
proposal cannot diverge — there is one implementation.

### Three defects the slice found beyond its own

* **`auto_approve` would have been an autonomy bypass.** The Console
  shipped policy presets using it. While policies were unenforced the
  mode was inert; enforcing it as written would have made a single policy
  row a second, ungoverned path to unattended execution — no evidence
  bar, no budget, no error-budget drop-back, and no fence for the
  risk-`high` classes that `never_budget_grantable` refuses at every
  autonomy level. **Refused on write, coerced on read (A15.7).** The
  autonomy contract stays the one governed answer to "may this run
  without a human." Presets and the mode option retired from the UI.
* **Approval policies could not be created at all on PostgreSQL.**
  `created_by` was `String(32)` while a Keycloak subject is a
  36-character UUID, so every create raised
  `StringDataRightTruncation` on a real deployment and succeeded only on
  the sqlite used in tests. Widened to 255 on policies and groups. Found
  by running the slice on the live stack. Guarded by a new model
  invariant test over identity-column widths, because sqlite ignores
  VARCHAR length and no insert-based test can catch this class — second
  instance after QA-040.
* **A policy created as "dual approval for everything" governed
  medium-risk actions only.** `risk_level` defaulted to `"medium"` while
  `action_type` and `device_type` defaulted to `"*"`. All three selectors
  now default to the wildcard, and the Console form gained the
  "All Risk Levels" option it never had.

### Proven on the live stack, two real Keycloak identities

`auto_approve` refused with 400 · a dual policy binds three pending items
across both origins · `operator1` approves and it records 1 of 2 without
executing · the same person is refused 409 · `admin` approves and it
decides, dispatches, and returns a real directive id · the ledger names
both approvers individually · one approval plus one denial is denied,
because a denial is terminal · one audit entry per approver and the chain
still verifies.

2683 → 2737 tests.

### Left for the next slices

Approver scope (`scope_ok` is written and enforced, resolving tenant-wide
until grants exist) is delivered by **E1.2** without touching any
approval code.

The declared-but-unread sweep for this surface, so nothing here is
mistaken for working: `approval_mode: "escalate"`,
`CCApprovalPolicy.time_window_json`, `CCApprovalGroup.escalation_chain`,
`slack_channel` and `github_team`. None is read by any runtime path. The
first three are governance and belong to **A2**, where escalation is
designed; the last two are notification integrations and belong with the
outbound-integration work that OQ-3 assigned to R3 and that has never
been built. They are left in place rather than removed because each names
a real product capability, and removing the column would be mistaking
"not built" for "not part of the product".

---

## 15. E0.2 — authoritative site identity (landed 2026-08-30)

Second slice of the Enterprise Governance Foundation. Spec amendment
**A16**. Hard prerequisite for E1.3.

### What was wrong — six leaks, not one

The review reported a single "return all devices" fallback. Tracing the
runtime found six paths, and the two worst were destructive rather than
merely leaky:

| # | Data | Before | Severity |
|---|---|---|---|
| 1 | devices | matched CC's id against the SM's own PK, missed, **fell back to every device** | leak |
| 2 | incidents | `list_open()` — no site filter at all | leak |
| 3 | pending actions | `list_by_status("pending")` — no site filter | leak |
| 4 | outcomes | no filter, **and set `reported_to_cc`** | **data loss** |
| 5 | candidate skills | no filter, **and set `reported_to_cc`** | **data loss** |
| 6 | usage | counted the whole Site Manager, labelled with one site | **billing** |

4 and 5 mean one site's poll consumed another site's evidence, so that
site never received it: fleet learning silently starved. 6 reaches
invoices.

### The binding

`sites.cc_site_id` (unique) is persisted at `RegisterSite` and is the
only resolution path. Registration is idempotent under the same
identity, allows a rename, creates a second site alongside the first,
and **refuses** to re-point a bound site — audited as
`site.bind_refused`. Recovery is the audited unbind at the site-token
API, requiring the site name as a typed confirmation and a reason.

An unresolved site returns an empty snapshot carrying
`site_resolved=false` and a reason (additive proto fields). Central
Command's poller **skips ingest** on that: ingesting it would clear the
site's fleet cache and, through D3 absence inference, resolve every one
of its incidents. It re-registers instead, which is also how an existing
deployment crosses the upgrade with no adoption guesswork at the Site
Manager.

### Error budgets became per site

Keyed `(site_id, action_type)`. Withdrawal, recovery, the dispatch gate
and the lease an agent receives are all resolved from the device's own
site. The Site Manager stays the execution and safety boundary; what is
per-site is the evidence and the withdrawal it justifies. The migration
carries existing drop-backs onto the site rather than dropping them,
because losing a withdrawal would restore autonomy nobody reviewed.

### Proven on real PostgreSQL, two sites on one Site Manager

Upgrade self-healed: the poller logged the unresolved site, skipped
ingest, re-registered, bound. Migration carried `SEL_CLEAR`'s drop-back
onto the site. Site A saw only its device and incidents, Site B only
its own, symmetrically. Polling A consumed **only** A's outcome and left
B's unreported; polling B then received exactly its own. Each site
metered 1 node, not 2; an unbound site metered 0. A conflicting
re-registration was refused and changed nothing. The unbind produced
`site.bound` / `site.bound` / `site.unbound` on a chain that still
verifies, and CC re-bound the site automatically afterwards.

2737 → 2772 tests.

### Deliberately not in this slice

The write path that lets an **agent** declare its site is E1.3 (additive
`AgentRegistration.site`, per-request site resolution replacing
`config.site_name` at its ~14 call sites, correlation looping per site).
E0.2 owns the read path and the identity; it proves that no read can
leak before the write path can create the situation.

---

## 16. E0.3 — observability, auditor reads, and one inert declaration (landed 2026-08-30)

Third and final slice of E0. No new spec amendment: this implements A13
(auditor scope) and applies D2's existing posture read-split
consistently. Nothing ratified changed.

### `/metrics`

`MetricsRegistry`, with Prometheus text export, shipped with R4-0 and had
**zero callers**. All three services had real `/healthz`, so the platform
could say it was alive and nothing about what it was doing.

Mounted on Central Command, Site Manager and Console, with a **request
counting middleware** — mounting an endpoint that always reports zero
would be the same declared-with-no-writer pattern E0 exists to remove, so
the counter is asserted to move. The registry is per app rather than the
module global: two services in one process (every test run) would
otherwise report each other's traffic. Unauthenticated like `/healthz`,
carrying only service counters and no tenant identifiers.

### A13 auditor read gates

Three reads were gated on permissions the auditor never holds, so the
ratified "read-only everything" scope was not real:

| Read | Was | Now |
|---|---|---|
| `GET /api/tenants/{id}/roles/` (Console) | `role.manage` | `user.view` |
| `GET /api/approvals/`, `/history`, `/{id}/records` | `action.approve` | `action.approve` **or** `audit.view` |
| `GET /api/policies/`, `/groups`, `/groups/{id}` | `site.manage` | `fleet.view` |

No permission was invented — the vocabulary is unchanged. A new
`require_any_permission` guard admits either of two personas to a read
they need for different reasons (an operator works the queue; an auditor
reads the R-C3 evidence), and is deliberately read-only: an `any-of` gate
on a write would be exactly the broadening D2 forbids. The policy
read-split matches what S1 already did for autonomy budgets, whose own
comment says posture must be visible to the people living under it.
**A viewer still cannot read approval evidence** — they hold neither
permission — and every mutation is asserted to have stayed put.

### The inert skill binding

A0 accepted `kind: "skill"`, rendered it in the UI, **validated nothing
about it**, and wired it to nothing: no skill installed, no directive
queued, no device changed. Making it real needs four things that do not
exist — a Console endpoint serving a skill's YAML by id (today it is
exposed only through the marketplace-*installs* feed), a CC fetch path,
per-device targeting on `InstallSkill` (it fans out to every device on
the site), and an install-on-activation trigger. That is A2's binding and
deployment work.

So the kind is refused, with a reason that names A2 and tells the
operator what they can bind today. `KIND_SKILL` stays defined with the
four missing pieces written down. Deferred, not discarded.

### Proven on the live stack

`/metrics` on all three services with counters that move (site-manager
3→5, central-command 2→4, console 4→6). A real Keycloak auditor read
approvals, history, policies, groups, audit, fleet, incidents and
autonomy — all 200 — and was refused every mutation, including approving
an action and creating an agent. A skill binding was refused with the
reason naming A2.

**Honest limit on one item.** The Console role-bundle gate is proven by
test but **cannot be exercised on this stack**: the Console honours
tenant roles only from a *tenant* realm, and the demo runs a single
platform realm, so no tenant persona reaches the Console there at all.
That is the ratified A11/A12 separation behaving correctly plus the
already-recorded TODOS item "SPA realm discovery", which **E1.4** owns.
Not a gap in this change, and not claimed as live-proven.

2772 → 2808 tests.

## 17. E1.1 — the generic organizational tree (landed 2026-08-30)

The first slice of the Enterprise Governance Foundation, and deliberately
the one with no authorization effect at all. A tenant describes its own
structure — regions, clusters, circles, trusts, territories, availability
zones — and attaches each site to exactly one node of it.

### The distinction this slice exists to protect

**Containment is not authorization** (ratified decision B). The tree says
where a site sits; a scope grant says who may reach it. E1.2 introduces
`cc_scope_grants`, and a grant will happen to reference an org unit the
way it could reference a site — but nothing in E1.1 resolves, widens or
implies permission, and nothing later may make it do so. An org-chart
edit must never be a privilege change. The compose gate asserts this
directly: moving a site between units leaves `/api/autonomy` byte
identical.

**Containment is not blast radius.** The Site Manager's rack and
fault-domain model is untouched. A power domain is a physical fact about
which devices die together; an org unit is an administrative fact about
who owns them. Conflating them would let an org-chart edit change what an
action is allowed to touch.

### What landed

| Object | Shape |
|---|---|
| `cc_org_units` | id · tenant_id · parent_id · unit_type · name · **path** · depth · sort_order · created/updated by/at |
| `cc_sites.org_unit_id` | FK, nullable through the migration, backfilled by it |
| `harkeniq_cc/org_tree.py` | pure: path composition, subtree expansion, depth bound, cycle refusal, move rewriting, display assembly |
| `OrgUnitRepo` | every query conjoined with `tenant_id`; subtree is one `startswith(path, autoescape=True)` |
| `/api/org-units/*` + `PUT /api/sites/{id}/org-unit` | reads `site.view`, mutations `site.manage` — no new permission |
| Console `Organization` page | one new proxy prefix, same pattern as `autonomy` and `operational-agents` |
| CC migration **0010** | guarded and idempotent; backfills one root per tenant |

### Why a materialized path rather than a recursive CTE

A subtree is one prefix match, which behaves identically on PostgreSQL
and on the sqlite the unit tests run against. A recursive CTE would have
diverged between them, and E1.2's scope resolver stands on this query —
the place where a divergence would become an authorization bug rather
than a display bug. `tests/integration/test_e1_prefix_equivalence.py`
runs the same query on both engines and asserts identical results.

### The trailing delimiter, stated accurately

Paths are `/id/id/id/` with a **trailing** delimiter, so `/u1/u7/` does
not prefix-match the sibling `/u1/u70/`.

Worth being precise about what this buys today: ids are `uuid4().hex`,
always 32 lowercase hex characters, and two ids of equal width cannot be
strict prefixes of one another — so on current data the sibling trap
**cannot arise at all**. That is a property of the id generator, not of
the query. The trailing delimiter makes the query correct *without
depending on it*: if a future import ever admitted a shorter or
variable-width id, the prefix match would still be right. The integration
test proves exactly that case by inserting deliberately short ids.

Hex ids also mean `/`, `%` and `_` cannot occur inside a segment, so the
delimiter cannot be forged and LIKE's wildcards cannot be smuggled in
through a unit name. `autoescape=True` holds the line regardless, and a
test forces a poisoned `/%/` row in to prove it.

### Rules, and where they are enforced

All server-side; the Console renders refusals rather than preventing
them, because hiding a button is never an authorization boundary.

- **Depth bounded at 8.** A move checks the whole subtree's height, not
  just the dragged node: a three-level subtree cannot land under a
  level-7 parent.
- **Cycles refused.** A destination whose path starts with the moving
  node's own path is rejected.
- **Sibling names unique** per (tenant, parent), a database constraint
  as well as a check.
- **Delete refuses** while a unit holds children or sites. Cascading
  would orphan sites silently, and at E1.2 an orphaned site is one nobody
  can be granted.
- **A move rewrites every descendant path in one transaction**, audited
  with both the old and the new path.
- **`unit_type` is a free slug, never an enum** — the customer's own word
  for the level, which is what decision A rejected a hard-coded
  Region/Cluster model in favour of.

### Backward compatibility

Nothing reads `org_unit_id` in E1.1. The compatibility promise is
asserted rather than trusted: tests confirm the fleet read and the
autonomy contract are byte identical before and after a tree is built and
a site attached, and that creating a tree changes no site field.

### Live proof (real PostgreSQL, real Keycloak)

Migration 0010 applied on the running stack and backfilled the demo
tenant's two E0.2 sites under one root. Over HTTP: a four-level tree
built with correct paths and depths; a re-parent moved Hall A's whole
branch and rewrote its path and breadcrumb; a cycle refused 400; a
depth-9 insert refused 400; a delete of a unit holding a site refused 409
with the site still attached; auditor read 200 / write 403; operator read
403 (no `site.view`); no token 401; the audit chain carries
`org_unit.created` / `moved` / `site_attached` / `deleted` and still
verifies.

## 18. E1.2 — scoped RBAC (landed 2026-08-30)

Central Command had exactly one authorization question — does this role
hold this permission — and no answer at all to "over which objects".
E1.2 is that second answer, for humans and for Operational Agents,
through one resolver.

### The model

```
principal -> grant(s) -> permission subset -> scope refs
          -> resolved authorization -> target-object check
```

Two questions, deliberately different, asked in different places:

- **"Could this actor ever possess this permission?"** — the route
  guard. All 68 `require_permission` sites unchanged.
- **"Does this actor possess it over THIS target?"** — the repository
  read filter, the object gate, and the approval gate.

They cannot be the same question because `permission_subset` is **per
grant**: a principal may hold `site.manage` over Cluster A1 and read-only
over Region B, so there is no single set of permissions that is true
everywhere. That is why `UserContext` gains nothing and why scope arrives
through a separate `get_scope` dependency — and it is what vindicates the
ratified "do not add scope to the decorators".

### What landed

| Object | Shape |
|---|---|
| `cc_scope_grants` | principal_type (user\|agent) · principal_ref · scope_type · scope_ref · permission_subset · role · expires_at · revoked_at |
| `cc_tenant_settings` | `legacy_open` \| `strict`, per tenant |
| `cc_approval_records` | `+scope_snapshot` `+authority_snapshot` (L2) |
| `cc_audit_log` | `+site_id`, **outside the hash payload** |
| `cc_agent_scopes` | **migrated in and dropped** |
| `harkeniq_cc/scope.py` | pure resolver: `resolve()`, `ResolvedScope`, `preflight_strict()` |
| `/api/scope-grants/*`, `/api/tenant-settings/scope-enforcement` | grant administration and the L1 flip |
| Console `Access Scope` page | one new proxy prefix pair |
| CC migration **0011** | guarded, idempotent, seeds `legacy_open` |

### Five scope types, not three

The ratified list named tenant, org-unit and site, then "any other scope
already supported by the product". The repository answered that clause:
`cc_agent_scopes` supported **site, device_class and device**. Dropping
the last two in the merge would have taken reach away from every agent
shipped in A0, so the unified vocabulary is five, and `org_unit` is
**added** to what an agent may be bound to — which is what lets a region
owner build an agent for their own region at all.

### Invariants, and where each is enforced

- **A subset only narrows.** `effective_permissions` is
  `role & subset`, computed once in the resolver. A subset naming a
  permission the role lacks grants nothing, and the grant API refuses it
  at write time with a readable reason rather than silently reducing it.
- **Grants do not union into authority.** Coverage and permission are
  checked on the **same grant**, so a grant covering the target but
  lacking the permission never borrows it from one that has it
  elsewhere. `may_ever()` exists for the fail-fast and is never
  consulted by `permits()`.
- **Ancestors are visible, never authoritative.**
  `contextual_unit_ids` is a separate field and no decision method reads
  it. The test that proves this empties the authority fields, leaves the
  ancestors in place, and asserts the scope decides nothing.
- **Read authority is not mutation authority.** The twelve
  tenant-governance tables have no site dimension: they are readable at
  permission level (a cluster manager must be able to read *why* they
  are blocked — that is the S5 contract's whole point) and mutable only
  at tenant scope.
- **Delegation cannot exceed the delegator.** Both for human grants and
  for agent bindings, checked per requested scope row.

### Two defects the slice found in its own work

**The delegation ceiling had no effective caller.** Agent creation was
first gated on *tenant* scope, which meant the ceiling could only ever
cap a tenant-wide creator — who can delegate anything. It was the
declared-with-no-caller shape this codebase keeps producing. The gate is
now object-level per requested scope row, so a region owner can build a
region agent and nobody can build one reaching past themselves. Proven
live both ways.

**The strict preflight would have locked out a grantless tenant.** Under
`legacy_open` a principal with no grants resolves tenant-wide — that is
what keeps upgrades working — and the preflight would have counted that
synthesized grant as evidence of an administrator, passed, and left
every principal with nothing. `Grant.synthesized` now marks it and the
preflight refuses to count it.

### The audit column, precisely

`AuditRepo._chain_payload` hashes `ts, actor, action, subject,
tenant_id, detail` and only those. `site_id` sits outside it, so every
chain written before E1.2 still verifies — asserted three ways: the
payload keys are pinned, a chain written without sites is extended with
scoped entries and re-verified, and two rows differing only by site are
shown to hash identically.

Honest limit: historical rows cannot be backfilled, because the site was
never recorded. They read as tenant-level and are visible only to a
tenant-scope holder. A scoped principal therefore sees *less* audit than
before E1.2; that is the correction, not a regression, and an auditor
holds tenant scope and loses nothing.

### The executable matrix

Ten personas against 68 endpoints is 680 cells, so the matrix is
executed rather than maintained:

1. `ROUTE_CONTRACT` declares each route's permission and one of four
   scope treatments. The only hand-written part.
2. A **route-contract test** walks the live route table; an `/api` route
   with no declaration **fails the suite**.
3. A **generated persona sweep** derives every expected outcome from the
   declaration and drives the real ASGI app.

No test asserts that a UI hid anything.

### Personas are roles plus grants, never new roles

"Region Manager" and "Cluster Manager" are `site_admin` with an
org-unit grant. The permission vocabulary is fixed at 24 permissions and
7 roles (spec §4); adding roles would fork it and make every future
organizational level a schema change. A test asserts no route demands a
permission outside the fixed vocabulary.

### Site Manager is untouched

SM authenticates with a site/service identity and remains the execution
and safety boundary. There is no second authorization resolver inside
it, and E1.2 adds no SM migration.

### Live proof (real PostgreSQL, real Keycloak, six identities)

Migration 0011 applied, the live A0 agent's site scope carried into
`cc_scope_grants`, `cc_agent_scopes` dropped, `legacy_open` seeded, and
the audit chain verified before and after.

On the acceptance tenant (Region A → Cluster A1 → site-1, site-b;
Region B → Cluster B1 → site-3), under **strict**:

| Persona | Reads | Refused |
|---|---|---|
| Tenant owner | all 3 sites, 3 devices | — |
| Region manager (`site_admin` + Region A) | site-1, site-b | site-3 |
| Cluster manager (`site_admin` + Cluster A1) | site-1, site-b | sibling A2 invisible (404); Region A readable but mutation 403; site-3 404; policy mutation 403; site register 403 |
| Site admin (`site_admin` + site-1 + site-3) | **site-1 and site-3, across different ancestors** | site-2 |
| Operator (`operator` + site-1) | site-1 | approving a `BMC_RESET` at site-b — 403 at layer 4 |
| Auditor (`auditor` + tenant) | all 3 | every mutation |
| Cluster-scoped auditor | **6 audit entries, one site** | the other sites' entries (tenant auditor sees 105) |
| Platform super admin, no grant | surface 200, **sees nothing** | every mutation |

Plus: a subset naming `role.manage` for an `operator` refused 400; a
region owner granting Region B / site-3 / tenant all 403; an agent bound
inside the region 201 and one reaching site-3 403 with the ceiling's own
message; no token 401; another realm's token 401; chain valid at 113.

## 19. E1.3 — one Site Manager, many sites (landed 2026-08-30)

E0.2 made every Central Command-facing **read** resolve to one
authoritative site. Every **write**, and all twenty of the Site
Manager's own endpoints, still resolved a single name from its config
file. E1.3 is the other half.

### The defect this slice existed to fix

**An agent never said which site it was at.** `AgentRegistration`
carried eleven fields and no site; `Ingest.register()` resolved
`config.site_name` and memoized the row id on the instance
(`self._site_id`, a process-lifetime cache of "the one site"). Two sites
on one Site Manager would have put **every device from both into one
site row** — and the E0.2 reads would then have scoped perfectly to a set
that was already wrong.

### Ratified D1 — site identity is authoritative, not agent-declared

The Site Manager issues a site-bound, revocable enrollment credential;
an agent presents it; the Site Manager resolves it to exactly one site
and persists the binding.

A `site_name` field on the registration message would have been the
smaller change and the wrong one: with one Site Manager token shared by
every agent, a declared site is a **claim** any agent could make about
any site, and correlation, blast radius, error budgets, metering and
(since E1.2) both human and agent authority all resolve from it.

- `site_enrollment_tokens` — hash only, so a leaked database yields no
  usable credential; the secret is shown exactly once.
- Unique `token_hash`: one secret can never resolve to two sites. The
  ambiguity is removed by the database, not by a check.
- Unknown, revoked, expired, or naming an inactive site → **refused**,
  and the refusal is audited. A device that cannot prove its site does
  not get one.
- No credential on a **multi-site** Site Manager → refused. On a
  **single-site** one → the one site, so an existing deployment upgrades
  without re-enrolling its fleet.
- **Changing site is an explicit re-enrollment.** The upsert already
  happened not to rewrite `site_id`, so the outcome was right by
  accident; a silent no-op is not enforcement, so it is now refused with
  a reason.

### Ratified D2 — the site is the normal operational safety boundary

Three distinct halts, and they are not interchangeable:

| Halt | Reaches | Set by |
|---|---|---|
| `tenant` | every site this Site Manager serves | pushed from Central Command |
| `site` | one site; the others keep running | an operator, per site |
| `site_manager` | every site, as an emergency | an operator, with a typed confirmation |

Lifting the emergency halt does **not** resume a site an operator
stopped separately.

**The pre-E1.3 switch was an in-memory boolean.** It was neither per site
nor persisted, so an operator could halt a site, the process could
restart, and autonomy would silently resume with nothing in the record
saying it had ever stopped. A stop switch that forgets is worse than no
stop switch, because it is trusted. `sm_stop_switches` fixes both at
once.

### The governed execution decision

`execution_permitted()` takes ten inputs and refuses unless every one of
them fails to object:

```
tenant stop + site stop + SM emergency halt + agent scope + permission
  + capability + autonomy + lease + preconditions + blast radius
```

An input **nobody supplied** is treated as *not yet evaluated and
therefore refusing* — an unevaluated governing input must never read as
consent. That default is what makes "an autonomy level is a ceiling,
never unconditional execution authority" structural: autonomy is one
input, evaluated seventh, and it can only ever fail to object.

### Correlation, the correctness risk

`on_onset` now correlates inside the **device's own** site and refuses to
correlate a device that has none; `sweep` runs once **per site**. Left
alone, two sites on one process would have produced a shared-power
incident spanning estates that share no power at all.

### Migration 0009

`site_enrollment_tokens`, `sm_stop_switches`, and `site_id` on
`agent_identities`, `sm_candidate_skills` and `audit_log`. The audit
column sits **outside** `_chain_payload`, exactly as CC's E1.2 column
does, so every chain written before it still verifies. The six
site-anchored tables needed nothing (E0.2 built them) and the nine
device-anchored ones reach a site through a total join. Backfill is
unambiguous by construction: a pre-E1.3 Site Manager has exactly one
site.

### No second authorization model

The Site Manager still authenticates with a site/service identity. The
new site resolver answers *which site is this request about*, never *who
may ask*. A test asserts over the **import graph** that no
`harkeniq_sm` module references `harkeniq_cc` — the boundary as a fact,
not a convention.

### Live proof (two Site Manager processes, real PostgreSQL)

SM-01 serves site-1 and site-b; SM-02 serves site-c, each with its own
database.

- Credentials are site-bound and distinct; **0 rows hold a raw secret**.
- Registration lands each agent at its credential's site; re-registering
  with another site's credential is **refused** and the device stays put.
- Unknown credential → refused, **0 device rows created**; no credential
  on a 2-site Site Manager → refused, naming why.
- `AgentRegistration` has **no `site_name` and no `site_id` field**.
- An unnamed read on a multi-site Site Manager → **400** naming both
  sites; named reads return only their own; a site it does not serve →
  404; a single-site Site Manager still answers unasked.
- Stopping site-1 left site-b running; **the halt survived a restart**;
  the emergency halt stopped both and required the typed confirmation;
  lifting it left site-1's own halt standing.
- SM-02 answered `site_resolved=false` with a reason for both site-1 and
  site-b, and returned **0 devices** — never broadened.
- Both audit chains verify with `site_id` recorded.
