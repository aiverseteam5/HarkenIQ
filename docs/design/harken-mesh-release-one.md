# Harken Mesh — Release One Design

**Date:** 2026-07-27
**Status:** Draft
**Supersedes for release one:** the Trust Layer / credential-first plan in the CEO plan of the same date. Release one is now Harken Mesh.

**Deliberately out of scope:** technology stack. No languages, buses, or storage engines named.

---

## 1. What release one is

Harken Mesh: nodes on devices that observe their own hardware, exchange evidence with their neighbours, reach conclusions locally, and act on a bounded set of known faults.

**Goal:** prove that diagnostic capability degrades predictably with topology density, and that a node with peers produces answers a node without peers structurally cannot.

**Acknowledged:** neither current demand source asked for this. Both point at servers and credentials. This release is a bet on the architecture rather than a response to a request, and it is being made deliberately.

---

## 2. The tiered capability model

**The node binary is identical everywhere. What it can conclude depends on its neighbourhood.**

| Tier | Condition | Mechanism | Best available answer | May act? |
|---|---|---|---|---|
| **T1** | Node present, ≥2 reachable peers | Peer mesh: quorum, witness, two-ended queries | **Root cause with corroboration**, seconds, locally | **Yes**, allow-listed |
| **T2** | Node present, 0–1 reachable peers | Node streams local detections upward; Site Manager correlates | **Root cause, uncorroborated**, slower, central | **Propose only** |
| **T1-W** | Node down, ≥2 peers present | Neighbours witness on its behalf | **Root cause plus pre-failure evidence** the dead node could not send | Peers report; no action on a dead device |
| **T3** | No node, or node down with no peers | Site Manager health ping | **Up/down only. No root cause obtainable** | No |

### 2.1 Tier is a property of the moment, not the deployment

A node computes its own tier continuously. Losing neighbours drops it from T1 to T2 and **withdraws its authority to act**. Tier is stamped on every conclusion and every refusal.

```
  reachable_peers = peers I can currently exchange state with

  |reachable_peers| >= 2   ->  T1   (quorum possible, action permitted)
  |reachable_peers| in 0,1 ->  T2   (stream upward, propose only)
  node absent/unreachable  ->  T3   (Site Manager ping, liveness only)
    and no peers observing
```

Two peers, not one. Quorum needs three independent observers including self; two observers cannot distinguish "you are broken" from "I am broken."

### 2.2 Honesty is a feature

**R-MD1.** Every conclusion carries the tier that produced it and what that tier can and cannot establish.
**R-MD2.** The Site Manager publishes a live coverage map: which devices sit in which tier, and therefore which classes of answer are available for each.
**R-MD3.** A device must never be reported as better-understood than its tier permits. A silent T3 device is not a healthy device; it is an unobserved one, and the report says so.

---

## 3. Node design

### 3.1 The loop

```
  OBSERVE ──▶ BASELINE ──▶ DETECT ──▶ CONSULT ──▶ CONCLUDE ──▶ ACT ──▶ REPORT
     │            │           │          │            │          │        │
  full-rate    per-entity  deviation   peers      diagnosis   gated    always,
  local        learned     from own    (T1 only)  + evidence  by tier  incl. refusals
  hardware     normal      normal                 + tier
```

**Observe.** Full-fidelity local hardware state at a rate that captures what remote polling erases. Sub-second where the fault class requires it.

**Baseline.** Normal for *this entity on this device*, learned over time, surviving restart. Never a global threshold.

**Baseline confidence is part of the baseline.** A baseline learned while the device was already degrading encodes the degradation as normal. Each baseline carries an age, a sample count, and a stability measure. **A node may not act on a conclusion derived from a low-confidence baseline** — it proposes instead.

**Detect.** Deviation from own baseline, with load-correlated behaviour separated from hardware degradation. The discriminator that matters: does it persist when traffic falls away.

**Consult.** T1 only. Query peers before concluding anything a single side cannot resolve.

**Conclude.** A named fault, a specific component, evidence with its time span, a confidence level, contradicting evidence, and the tier.

**Act.** Section 5.

**Report.** Conclusions, refusals, precondition failures, and tier changes. All of them.

### 3.2 Constraints

**R-MD4.** Hard resource ceilings, enforced and observable. A node may not compete with the device's primary function.
**R-MD5.** No sustained writes to device flash. Bounded memory ring first; spill rate-limited; audit records never dropped, telemetry may be.
**R-MD6.** Node failure is independently detectable and must never be reported as device failure.
**R-MD7.** A node survives loss of the Site Manager without losing local detection. It buffers and reconciles on reconnection. **It does not gain authority it did not already hold** — see §5.4.

---

## 4. Peer protocol

### 4.1 Discovery

Peer sets come from topology where the platform provides it, and from Site Manager assignment where it does not. A node knows, for each peer, which local interface faces it.

### 4.2 Heartbeat and state exchange

Periodic bidirectional heartbeat. On-demand state queries. Peers hold a bounded window of each neighbour's recent state — this is what makes T1-W possible.

**R-MD8.** A node retains enough of each neighbour's recent state that, if that neighbour dies, the retained window explains why. Evidence the dead node could not send is the entire value of witnessing.

### 4.3 Claim and lease

Unchanged from doc 1 R-M15–R-M19, restated for implementation:

**R-MD9.** A detecting node broadcasts a claim. First claim observed wins. Claims crossing in flight resolve on lowest stable node identity — **never on timestamp**, because clocks drift and time sync fails precisely when the network is impaired.

**R-MD10.** When a device becomes unreachable the claim subject is always **the device**, never the link. Whether the fault is the device or the link is the conclusion, reached later. Mixed subjects mean deduplication fails and duplicate incidents ship anyway.

**R-MD11.** Ownership is a renewable lease. A lapsed lease returns the incident to claim, inheriting gathered evidence. This covers the first detector dying mid-investigation.

**R-MD12.** Claims are signed with node identity. A node whose claims repeatedly contradict quorum is flagged in the coverage map and loses action authority pending review.

### 4.4 Quorum disambiguation

The four-way distinction, as a decision rule. `A` loses contact with `X`:

```
  A queries all reachable peers about X
      │
      ├─ ≥2 others also lost X, and those others reach each other
      │        └──▶  X IS DOWN            (corroborated, action-eligible)
      │
      ├─ others still reach X
      │        └──▶  LINK A–X IS DOWN     (X is healthy)
      │
      ├─ X's link is up and forwarding, but no agent heartbeat
      │        └──▶  X's NODE FAILED      (device is fine — never report device down)
      │
      └─ A lost every peer simultaneously
               └──▶  A IS ISOLATED        (A reports on itself)
```

**R-MD13.** A node with fewer than two reachable peers cannot execute this rule. It drops to T2, streams its observation upward marked uncorroborated, and does not act.

**R-MD14.** The third branch is load-bearing. Reporting a device down when only the monitoring software fell over is the fastest way to lose an operator's trust permanently.

---

## 5. Action path

Autonomous within an allow-list. The safety machinery is the design, not a wrapper around it.

### 5.1 Autonomy gates on tier

**This is the central safety property.**

| Tier | Authority |
|---|---|
| T1, corroborated by ≥2 witnesses | **May execute** an allow-listed action |
| T1, uncorroborated | Propose only |
| T2 | Propose only |
| T3 | Report only |

**R-MD15.** Autonomous action requires a corroborated T1 conclusion. The system can only act where it can prove what is wrong. Nothing acts on a single node's unverified opinion.

This is not policy layered on top. It falls out of the tier model, which means it cannot be disabled by configuration error.

### 5.2 Containment

**R-MD16.** **A node may act only on its own device. Never on a peer.** Without this, one compromised node has fleet-wide reach. Peers may witness, corroborate, and report about a neighbour; they may never touch it.

**R-MD17.** Per-device action allow-list, enforced locally at execution. The authority to refuse lives on the node. Central-only authorization is one defect from a fleet-wide event.

**R-MD18.** Every action carries: verifiable authorization signed by a key distinct from the transport credential, an idempotency identifier, an expiry, and an explicit action class.

**R-MD19.** Local rate limiting. Repeated action on the same fault escalates rather than repeats — a node that has restarted the same service three times in ten minutes refuses the fourth and escalates to a human.

**R-MD20.** Local preconditions re-checked immediately before execution. A conclusion reached ninety seconds ago may no longer hold.

**R-MD21.** Local kill switch rendering a node inert without physical access.

**R-MD22.** Every action, refusal, rate-limit trip, expired instruction and precondition failure is audited with equal weight.

### 5.3 Blast radius

**R-MD23.** Concurrent autonomous actions are capped per fault domain and per site. Simultaneous action across a domain is a self-inflicted outage regardless of whether each individual action was correct.

**R-MD24.** Correlated conclusions across multiple devices suppress autonomous action and escalate. Many devices concluding the same fault at the same moment usually means a shared upstream cause, and acting on each independently makes it worse.

### 5.4 Partition fencing

**The failure mode autonomy introduces.** A network partition splits the mesh. Each fragment has internal quorum. Each concludes the other side is down. Both act.

**R-MD25.** Action authority requires a currently valid authorization from the Site Manager. A fragment that cannot reach the Site Manager retains **observation and diagnosis** but loses **action authority** when its authorization lease expires.

**R-MD26.** Authorization leases are short relative to partition detection time. Diagnosis degrades gracefully under partition; action fails closed.

This is the direct consequence of R-MD7 — a node isolated from its Site Manager does not gain authority, it loses it.

---

## 6. Site Manager

Present in release one, because tier 2 requires it.

**R-MD27.** Receives corroborated conclusions from T1 and uncorroborated detections from T2.
**R-MD28.** Correlates T2 detections into diagnoses. This is where a node without peers gets its answer.
**R-MD29.** Health-pings T3 devices. Liveness only, and reported as liveness only.
**R-MD30.** Maintains and publishes the coverage map (R-MD2).
**R-MD31.** Issues and renews action authorization leases (R-MD25).
**R-MD32.** Enforces site-level blast radius (R-MD23) independently of node-local limits.
**R-MD33.** Consolidates claims about one subject into one incident.
**R-MD34.** Site Manager loss does not silence the mesh. T1 continues diagnosing and buffering; action authority expires per R-MD25.

---

## 7. Failure modes

| Failure | Consequence | Mitigation |
|---|---|---|
| Node dies, peers present | Peers witness with pre-failure evidence | R-MD8, T1-W |
| Node dies, no peers | Device becomes T3 — liveness only | R-MD3 reports it honestly |
| Network partition | Fragments both hold quorum | **R-MD25 fencing** |
| Baseline learned while degraded | "Normal" encodes the fault | Baseline confidence; low confidence cannot act |
| Peer lies or is compromised | False corroboration | Signed claims; quorum of 3 tolerates 1; R-MD12 flags repeat dissent |
| Claim storm | Duplicate incidents | R-MD9/R-MD10 subject key and tiebreak |
| Owner dies mid-investigation | Orphaned incident | R-MD11 lease |
| Action succeeds, fault persists | Repeated action | R-MD19 escalation |
| Shared upstream cause | Many simultaneous actions | R-MD24 suppression |
| Clock skew | Claim resolution fails | R-MD9 uses identity, not time |

---

## 8. Success criteria

1. **The tier comparison.** The same fault, injected at T1 and at T2, produces a demonstrably better answer at T1 — a different conclusion class, not merely higher confidence. This is the falsifiable claim and the product demo.
2. **The witness case.** A node dies with peers present; the peers report it with its pre-failure evidence attached, before existing monitoring notices.
3. **No false device-down** caused by node failure (quorum branch three).
4. **The coverage map is accurate.** No device silently reported as better-understood than its tier permits.
5. **Fencing holds.** Under an induced partition, no fragment executes an action after its authorization lease expires.

Criterion 1 is the one that decides whether the mesh is worth building. Criterion 5 is the one that decides whether it is safe to ship with autonomy enabled.

---

## 9. Open questions

| # | Question | Blocks |
|---|---|---|
| Q1 | Which action classes are in the release-one allow-list? Smaller is better; each needs its own precondition set | §5 |
| Q2 | What is the authorization lease duration, against expected partition detection time? | R-MD25/26 |
| Q3 | How is a baseline's confidence computed, and what is the threshold below which action is refused? | §3.1 |
| Q4 | Where do peer sets come from on platforms without native topology discovery? | §4.1 |
| Q5 | How is a fault injected at both T1 and T2 for criterion 1, on real hardware, without causing a real outage? | Success criterion 1 |
| Q6 | Node identity: how is it established, and how is a node's key rotated or revoked? | R-MD12, R-MD18 |
| Q7 | Does the two-device correlation from the prior plan fold in here, or stay a release-two item? | Scope |

Q5 is the one with no obvious answer, and it is the same class of problem as testing destructive credential operations. It should be solved before the build starts, not during it.

---

## Related

- [01 — Platform Architecture](../requirements/01-architecture.md) — doc 1 §3 is the source for the node requirements restated here
- [02 — Market and Build Plan](../requirements/02-market-and-build-plan.md)
- [TODOS.md](../../TODOS.md)
