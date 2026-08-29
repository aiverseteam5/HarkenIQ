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
| Incidents + diagnosis (what is wrong and why) | SM `/api/incidents` only | site · site token | — · — | ✗ / ✗ / read-ready / Tier-1 `explain_incident` | proto ext. +`cc_incidents` | **wiring → S4** (the one P1 contract repair; resolution by absence per D3) |
| Approvals (human gate on actions) | `/api/approvals/*` · CC | tenant · action.approve | IS the gate · chained | ✓ / ✓ / propose-target / Tier-2 | — | existing (P0-proven) → diagnosis excerpt rides S4 |
| Action execution (14 types, gate funnel) | node `_execute_gated` via SM directives | device · lease/approval | per class · 4 phases | via queue / — / core loop / never direct | — | existing → consumed by A1 |
| Autonomy budgets + stop switch (the trust ladder) | `/api/policies/autonomy`, `/stop-switch` · CC | tenant · reads→fleet.view (D2, lands S1); writes site.manage | mutation human-only · chained | ✗ / ✓ / posture-read / Tier-1 + `activate_stop_switch` | — | partial → S1 read-split, S5 surface |
| Approval policies/groups | `/api/policies/*` · CC | tenant · site.manage | — · chained | ✓ / ✓ / later / later | push unwired | **partial → A2 wires `approval_policies_json` push** |
| Outcomes + patterns (what worked) | `/api/outcomes/*` · CC | tenant · fleet.view | — · — | partial (Reliability) / ✓ / read-ready / Tier-1 | — | existing → Learning surface (S3) |
| Learning loop (candidates, cycles, promotions) | `/api/learning/*` · CC | tenant · fleet.view | promotion = human (marketplace) · — | ✗ / **✗ not proxied** / read-ready / Tier-1 | proxy prefix | wiring → S1 prefix + S3 surface; cycles in-process (label honestly; durable P2) |
| Predictive risk (what fails next) | `/api/predictive/risk` · CC | tenant · fleet.view | — · — | ✗ / ✓ / read-ready / Tier-1 | — | existing → S2 surface |
| CVE exposure (what is vulnerable) | `/api/firmware/*` · CC | tenant · view fleet.view, import site.manage | — · — | ✗ / ✓ / read-ready / Tier-1 | — | existing → S2 surface; import stays API-only |
| Warranty/lifecycle | `/api/warranty` · CC | tenant · fleet.view (import site.manage) | — · — | ✓ drawer / ✓ / read / later | — | existing |
| Harken Nodes (deployed agents) | `/api/agents` · CC | tenant · fleet.view | — · — | ✓ (truthful since P0) / ✓ / read / read | — | existing |
| Audit + chain verify (prove it) | `/api/audit[/verify]` · CC + Console | per store · audit.view | — · IS audit | entries ✓, verify ✗ / ✓ / read / Tier-1 `verify_audit` | — | existing → S1 verify status line |
| Firmware campaigns (waves, halt, rollback) | SM `/api/firmware-campaigns/*` | site · site token | campaign-level human · chained | ✗ / **✗ no CC path** / status-read later / later | proto RPCs | **wiring → S6 CC mediation, WHOLE flow (D6)** |
| Sites | `/api/sites` · CC | tenant · fleet.view (register site.manage) | — · register chained | ✗ / ✓ / read / read | — | existing → S1 filter facet |
| Skill distribution (marketplace → node) | Console→CC `InstallSkill`→SM directives→node | tenant · skill.install | marketplace review · chained | ✓ / ✓ / A1 reuses / later | — | existing (R5-1/R5-2 proven) |
| Operational Agent (the product noun) | — | tenant · interim site.manage; agent.manage at A2 (matrix review first) | activation human · chained | ✗ / ✗ / — / — | A0 tables | **missing → A0+A1 thesis slice** |
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
→ S5 Autonomy surface (trust ladder + per-class evidence + stop-switch
   control; D2 split already live from S1)
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
