# HarkenIQ — Product Requirements Document

*Version 1.0 · July 2026 · Owner: Founder/CEO · Status: Draft for team review*

---

## 1. Product overview

**HarkenIQ is the vendor-neutral intelligence layer for data center hardware — one embedded brain for every server you own, regardless of whose name is on the bezel.**

Every server becomes a smart device: it senses its own state, understands its own health, acts within limits it has earned, and reports to one control center. The data center stops being a room full of dumb metal that humans babysit, and becomes a fleet of machines that largely runs itself.

Positioning in six words: **they bolt on, we build in.**

HarkenIQ is both the company and the product. This venture stands alone — it does not sit under any existing brand.

---

## 2. The problem

Every hardware vendor's intelligence stops at their own logo. Dell's tool sees Dell. HPE's tool sees HPE. Lenovo's tool sees Lenovo. No real data center is single-vendor — and the fastest-growing operators run the most mixed fleets of all.

Above the hardware, a new generation of AI operations products reasons about applications and software. None of them touch the physical machines.

Between those two layers sits a gap where the hardware actually lives. Today that gap is filled by humans doing toil: watching six consoles, chasing symptoms across layers, doing the same repetitive fixes at 2am, and scaling their headcount linearly with every rack added.

The cost of the gap, in the buyer's language:

- **Engineers per hundred boxes.** Ops headcount grows with fleet size because the intelligence doesn't.
- **Hours lost to the wrong layer.** A single physical fault echoes upward as application symptoms; humans routinely spend hours chasing the echo instead of the cause.
- **Idle hardware is burned money.** For AI-infrastructure operators, every hour a machine sits broken is directly lost revenue.

**Structural advantage no vendor can copy:** a manufacturer's fleet tool ultimately serves the manufacturer — sell more boxes, more support contracts. A neutral brain serves only the fleet owner, including telling them honestly which vendor's hardware fails more. That feature can never ship from a vendor.

---

## 3. Who it is for

### Beachhead segment
**GPU clouds and AI-factory operators in the US and Europe.** Mixed fleets assembled from whatever allocation they could get, five-person ops teams, every idle hour burning real money, minimal vendor loyalty, fast procurement.

### Expansion segments (post-MVP)
Colocation providers, sovereign and air-gapped data centers, regulated enterprises (banking, healthcare, telco, manufacturing) building AI infrastructure on heterogeneous hardware — segments where auditability and data sovereignty already win deals.

### Explicitly not the buyer
Single-vendor enterprise shops satisfied with their vendor's own console.

### The three people who matter in every deal

| Person | Role in the deal | What wins them |
|---|---|---|
| Ops engineer / SRE | **Adopts.** Finds the open agent, installs it on a few boxes, becomes the champion | Install in minutes; a day-one "wow" no vendor tool can show |
| Head of Infrastructure / VP | **Pays.** Approves the budget | Two numbers only: engineers saved and downtime prevented |
| Security & Compliance | **Blocks.** Cannot say yes, can absolutely say no | Being boring: read-only start, everything signed, full audit trail, their own approval gates |

The sales motion is bottom-up: land with the engineer, arm the champion to sell the boss, clear security by being auditable.

---

## 4. Market and competitive landscape

Three camps exist today, and none occupies HarkenIQ's seat:

1. **Vendor fleet tools** (Dell CloudIQ, HPE InfoSight, Lenovo XClarity). Deep on their own hardware, structurally blind to everyone else's. Bundled with hardware, not a neutral party.
2. **Observability platforms** (Datadog, LogicMonitor, Dynatrace). Broad visibility, priced per host per month, but they watch and alert — humans still do the work. Public price anchors: infrastructure monitoring runs roughly $15–23 per host per month at the majors.
3. **AI SRE natives** (Resolve AI, Cleric, NeuBird, Traversal). The fastest-moving category — autonomous investigation of software incidents, enterprise contracts, sales-led premium pricing, and one player already valued above $1B. This validates that enterprises now budget for AI that operates production. **None of them touch physical hardware.**

**The empty seat HarkenIQ takes:** AI-native operations, applied to the layer the AI SRE wave skipped — the metal. The category's momentum is our tailwind; the hardware layer is our unclaimed ground.

Market-size figures for this space vary wildly between reports and should be treated as sanity checks only. The evidence that matters is five real operators confirming hardware toil is a top-three pain (Section 15).

---

## 5. Product principles

These decide arguments when features are debated. Every requirement below traces to one of them.

1. **Neutral by structure, not by promise.** Value must come from seeing across brands; honesty about limits is part of the brand.
2. **Trust is earned, never assumed.** The product watches before it suggests, suggests before it acts. Autonomy is the destination, not the demo.
3. **The complexity is ours to carry.** The customer experience is: install in minutes, ask questions in plain language, approve in seconds. Everything hard stays invisible.
4. **Escalation is the exception.** Routine problems are handled and recorded, not turned into incidents. We sell the removal of toil.
5. **Integrate, never replace.** Every tool the customer already runs is a reason to say yes. We speak their tools' language; they never come to us.
6. **Self-documenting fleet.** Every action and every human approval is recorded, signed, and reviewable. The moment a machine acts on its own, the first question is "prove what it did and why" — the product must answer before it is asked.
7. **Never say no to a requirement; always say no to a mechanism.** Customers' compliance needs are met inside our model, not by rebuilding their legacy process inside our product.

---

## 6. What the product does (capability view, by trust level)

### Level 1 — Observe (free, open-source agent)
- Discover every machine across every brand in one inventory
- One health picture for the whole mixed fleet
- Answer plain-language questions about the fleet no single vendor's tool can answer
- Connect one physical cause to its symptoms across layers — the "failing part explains the slow application" moment that is the day-one wow

### Level 2 — Approve (paid; where revenue lives for the first 2–3 years)
- Product proposes fixes with evidence; a named human approves in ~15 seconds from where they already work (chat or review queue)
- Bounded, reversible fix actions — starting deliberately boring (configuration corrections, evidence collection), never dramatic
- Credential custody and automatic password rotation for managed hardware — built-in by default, integrates with the customer's existing vault where one exists. (Rotation is itself toil nobody does well; it is a sellable feature, not just hygiene.)
- Full decision record: what was proposed, who approved, what happened

### Level 3 — Autonomy (post-MVP vision; not in this PRD's committed scope)
- Proven, repeated fix types run unattended within customer-set limits
- Safety budget: if outcomes degrade, the fleet automatically drops back to Approve mode
- One-command stop switch, always

**The trust ladder is the product roadmap and the pricing ladder and the adoption path — one structure, three uses.**

---

## 7. MVP definition

**MVP = Observe fully working across a genuinely mixed fleet, plus the first Approve-mode write actions.**

In scope:
- Multi-brand discovery, normalization, single fleet view
- Plain-language fleet Q&A
- Cross-layer cause-to-symptom correlation (the demo moment)
- First write actions: boring, reversible, human-approved
- Credential custody + rotation (built-in default)
- Recorded approvals and complete audit trail
- Works in connected and disconnected environments alike

Out of scope for MVP (deliberate):
- Firmware/patching orchestration (the hardest orchestration problem; earns its own phase)
- Autonomous (unattended) action
- Ticketing-system integration beyond simple outbound record-writing
- Any layer above the hardware except reading it for correlation

Out of scope for the product, permanently:
- Replacing the customer's monitoring, logging, or ticketing stack
- Becoming a hand-maintained asset database
- Rebuilding legacy change-management workflow inside the product

---

## 8. Lifecycle plan — 30 / 60 / 90

### Days 0–30 — Prove it (own lab)
- MVP running end-to-end on founder's own mixed-brand lab hardware
- The day-one wow demo is repeatable on request
- Open-source agent public: repository live, install path polished, first documentation
- Core technical members (2, US-based) validating and testing throughout
- Pricing and packaging finalized; design-partner terms written
- Exit gate: a stranger can install Observe in minutes on hardware we've never seen

### Days 31–60 — Validate it (real customers)
- 3–4 design partners live on real fleets: first month free, chargeable from day 31
- Weekly toil-hours-recovered number per partner — this becomes the sales evidence
- First Approve-mode actions running in a customer environment with recorded human approvals
- Security review dry-run with at least one partner's security team (find the objections before sales does)
- Case-study material gathered continuously, not at the end
- Exit gate: at least one partner converts to paying; at least one written "before/after" toil story

### Days 61–90 — Sell it (GTM + raise)
- Pre-seed raise opens (target close by day 90); pitch is built on partner evidence, not architecture
- GTM pair (2 people, on board from day one) converts pipeline built during validation
- Public launch motion: open-agent community push, founder-led content on the toil story
- Pricing live on real deals; first non-design-partner customer targeted
- Exit gate: pre-seed closed or term-sheet in hand; ≥3 paying customers; seed narrative drafted

### Post-90 (next phase, seed motion)
- Expand write-action catalog by evidence: automate what partners approved most often
- Regulated/sovereign segment entry (compliance mapping document as sales asset)
- Firmware/patching orchestration as the first major post-MVP capability
- Autonomy earns its first pilot only where a customer's own approval history justifies it

---

## 9. Pricing and packaging

**Anchor: premium, per-node platform fee, annual contract. Price high, sell few.**

Research-based rationale:
- The observability majors have trained the market to pay ~$15–23 per host per month for *watching*. HarkenIQ acts — with approval, on the physical layer they can't see. Acting is worth more than watching.
- The AI SRE natives validated premium, sales-led enterprise pricing for AI that operates production — and reached unicorn valuations on it. We inherit that willingness-to-pay, aimed at the hardware layer.
- Per-investigation pricing (one AI SRE model) is rejected: it punishes the customer for having problems and makes budgeting unpredictable. Per-node is flat, predictable, and scales with the thing the customer actually values — fleet size under management.

Packaging:

| Tier | Price shape | What it buys |
|---|---|---|
| Observe | Free, open source | Full mixed-fleet visibility. The distribution engine, never monetized |
| Approve | Per node per month, annual commit, premium anchor | Proposed fixes, human approval flow, credential rotation, audit trail |
| Enterprise | Approve + platform fee | Disconnected-site support, compliance reporting pack, priority product-defect SLA |

Working anchor for validation: a mid-size fleet should land in low-six-figures annual — consistent with the $100K+ contract sizes the category has established. Exact per-node number is set during days 31–60 against real deal feedback; the design partners' conversion is the pricing test.

Support model: the customer's own team operates the product day-to-day. Paid tier covers product-defect response — we are a product company, not an outsourced ops desk.

---

## 10. Go-to-market

**Motion: open-source bottom-up, converted by founder-led sales, aimed at US and Europe.**

- **Distribution:** the free Observe agent spreads engineer-to-engineer. It is the lead-generation machine; the funnel is install → wow → champion → conversation with the budget owner.
- **Sales:** founder-led for the first customers, GTM pair (on from day one) building pipeline in parallel during validation and converting from day 61. US-based technical members give the venture in-market presence and timezone coverage for US customers from the start.
- **Evidence over claims:** every sales asset is a design-partner number — toil hours recovered, downtime avoided, approval-to-fix time. No architecture slides in front of buyers.
- **Security-first collateral:** the objection-handling pack for the blocker persona (what it can touch, what it can't, what's recorded, how to stop it) ships as standard sales material from day 60.
- **Community:** open-agent contributions become the moat's flywheel — operators extend coverage for hardware we will never own.

---

## 11. Team and operating model

| Who | Role | From |
|---|---|---|
| Founder + Technical co-founder | Product, vision, founder-led sales | Day 0 | 20 yrs Datacenter management + identity-management development | Core build leadership; owns the trust/credential capability 
| Senior technical staff — 20 yrs | Build, validation, customer-environment work | Day 0 |
| GTM / distribution pair | Pipeline, community, launch motion | Day 0 |

Build model: agent-driven development with the two senior technical members validating and testing every increment. This is a stated operating advantage — small headcount, senior judgment, machine leverage — and part of the pitch narrative.

The co-founder's identity-management depth is a deliberate strategic match: the single hardest objection this product faces is "you want the keys to every machine we own" — and the person answering it has spent twenty years on exactly that problem.

---

## 12. Funding plan

- **Days 0–60:** self-funded. Milestones are the currency.
- **Day 60:** pre-seed raise opens, pitched on live design-partner evidence.
- **Day 90:** pre-seed closed (target); proceeds fund the seed-stage motion — expanded GTM, regulated-segment entry, security certification track (long lead item, started early).
- **Pre-seed → seed evidence bar:** 3–5 active paying customers, first ~$100–250K annual revenue signed, retention and expansion signals from the earliest accounts.

---

## 13. Success metrics

**North star (day 90): seed-ready — a fundable evidence pack plus real paying customers.**

| Phase | Metric | Target |
|---|---|---|
| 30 | Time-to-installed for a stranger | Minutes, not hours |
| 30 | Wow-demo repeatability | On demand, any mixed lab |
| 60 | Design partners live | 3–4 |
| 60 | Toil hours recovered per partner per week | Measured and rising (the number itself is the deliverable) |
| 60 | Free-to-paid conversion | ≥1 by day 60, all by day 90 |
| 90 | Paying customers | ≥3 |
| 90 | Pre-seed | Closed or term sheet in hand |
| Ongoing | Open-agent installs | Tracked from day 30 as the top of funnel |
| Ongoing | Approval-to-fix time | The product's own promise: seconds to approve, minutes to fixed |

Counter-metric watched deliberately: **time from install to first approved action.** If champions install but never let it act, the trust ladder is broken and pricing collapses to a monitoring commodity — this is the single most important early-warning signal.

---

## 14. Risks and mitigations

| Risk | Reality check | Mitigation |
|---|---|---|
| Problem isn't top-three pain | The one assumption everything rests on | Section 15 validation before scaling spend; design partners are the test |
| 30-day POC is aggressive | It is | Scope ruthlessly to the wow demo + first write action; everything else slips before these do |
| Simple to try ≠ simple to trust | Champions install in minutes, won't let it act for weeks | Approve-mode designed for 15-second decisions; toil evidence gathered even in Observe mode so value shows before trust arrives |
| "Keys to every machine" objection | The deal-killer if fumbled | Co-founder owns this answer personally; security collateral standard from day 60; credential rotation positioned as a feature customers gain, not a risk they take |
| Vendors squeeze hardware access or copy the approach | Real, medium-term | Speed to the community flywheel and the accumulated cross-brand knowledge; neutrality itself is the uncopyable position |
| Selling US/Europe from a distributed base | Founder in India, team in US | US members are in-market from day 0; founder-led sales runs on US hours; entity/registration decision due before first paid contract (open question) |
| Revenue lags the vision | Nobody flips autonomy on in year one | Priced and packaged for Approve-mode as the revenue product; autonomy is roadmap, not promise |
| Open vs commercial line drawn wrong | Giving away the moat or starving the funnel | Free tier = visibility only, forever; everything that acts or learns is commercial; line reviewed at each phase gate |

---

## 15. Validation plan (the honest section)

Everything above is hypothesis until real operators confirm it. Two structured checks:

1. **Problem validation (days 0–30, parallel to build):** conversations with 5 mixed-fleet operators. Not a pitch. Three questions: what broke last quarter, what did you do, how long did it take. **Kill criterion:** if hardware toil is not a top-three pain for at least 3 of 5, the beachhead is wrong — revisit segment before day 31, not after.
2. **Value validation (days 31–60):** the design partners. **Kill criterion:** if no partner converts to paying by day 60, the willingness-to-pay assumption fails — pricing, packaging, or problem needs rework before any raise conversation.

---

## 16. Open questions

1. **Open agent naming** — does the free agent carry its own name or ship as "HarkenIQ Agent"? Decision needed before the day-30 public repository launch.
2. **US/Europe legal entity** — where is the company registered, and is a US entity required before the first paid contract or the pre-seed? Needs an answer by day 45.
3. **Exact per-node price point** — set during validation against real deals (framework in Section 9; number pending evidence).
4. **What makes a fix "verified good"** — the definition that gates every expansion of what the product is allowed to do; owned as a product decision, not delegated.
5. **Design-partner sourcing** — warm names vs. cold outreach split for the 3–4 partners; pipeline owner is the GTM pair, list due by day 21.
6. **Compliance certification timing** — the certification track is a long-lead item the regulated segment will demand; start date is a budget decision at pre-seed close.

---

## 17. Document trail

- Product framing (strategy + architecture rationale): *OpsForge Fleet — Product Framing v0.1* (predecessor document; product since renamed HarkenIQ)
- This PRD: the product lifecycle contract for the team — reviewed at each 30-day gate, updated only through agreed change
- Next artifacts: design-partner one-pager (by day 21), security objection pack (by day 60), seed narrative (by day 90)
