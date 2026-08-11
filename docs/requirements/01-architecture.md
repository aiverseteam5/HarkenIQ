# HarkenIQ Platform Architecture — Requirements

**Document 1 of 3**
**Date:** 2026-07-27
**Status:** Draft for review
**Scope:** What HarkenIQ is, and the definition, responsibilities and behaviour of its three layers — Harken Mesh, Harken Site Manager, Harken Central Command.

**Deliberately out of scope:** technology stack. No languages, message buses, databases, or storage engines are named here. Where a capability requires something of its transport or storage, the *requirement* is stated so the technology choice can be made against it later. Industry protocols (Redfish, LLDP, BFD, gNMI, IPMI, Modbus) are named where they define the problem, not as implementation choices.

---

## 1. What HarkenIQ is

HarkenIQ is an autonomous operations platform for physical data center infrastructure.

It occupies a layer that is currently empty. Below it, vendor management tools see their own hardware and nothing else. Above it, AI SRE platforms reason about applications and software and never touch a physical machine. Between the two sits a gap filled by people: watching multiple consoles, carrying symptoms between layers by hand, and performing the same repairs repeatedly.

**The problem is not that hardware state is invisible.** Cross-vendor tools that display sensor readings already exist. The problem is that nothing *reasons* across vendors and nothing *acts*. A degrading optic produces a number on a dashboard; turning that number into "replace this specific part, in this specific slot, within this window" is human work, performed hours later, by someone who has to log back into the device to recover the detail that was discarded on the way out.

### 1.1 The core thesis

The current path from hardware fault to fix is roughly ten hops long, and the reason is structural: **a thin summary is carried away from the device, and then hours later a human travels back to the device to recover the detail that summary dropped.** The round trip is the waste.

HarkenIQ removes the round trip by doing the reasoning where the data already is. A fault should leave the device as a **diagnosis with evidence**, not as a symptom that needs interpreting. A diagnosis does not need a level-1 analyst to route it or a level-2 engineer to decode it — it goes directly to whoever can approve the fix.

### 1.2 What this is not

- **Not a monitoring tool.** Monitoring displays state. HarkenIQ produces conclusions.
- **Not a dashboard consolidation play.** Centralizing more data into one pane does not shorten the chain; it lengthens the pipe.
- **Not a replacement for routing protocols.** BFD and routing protocol adjacencies already detect dead neighbors and reroute in milliseconds. HarkenIQ does not compete with that and must not interfere with it. Those mechanisms answer *"can I still forward traffic?"* and respond by routing around the wound. HarkenIQ answers *"what is actually broken, and what should be done about it?"* — a question nothing on the box asks today.

### 1.3 Design principles

These constrain every requirement that follows.

| # | Principle | Consequence |
|---|---|---|
| P1 | **Decide locally, learn globally** | Detection and correlation are distributed. Learning is centralized and pushed back down. |
| P2 | **Diagnosis before autonomy** | The platform's default output is a conclusion for a human to approve. Autonomous action is earned per action class, per customer. |
| P3 | **Every layer degrades independently** | A node works without its Site Manager. A site works without Central Command. Loss of an upper layer reduces capability; it never causes silence. |
| P4 | **A device's self-report is evidence, not a verdict** | Neighbour-observed data outranks a device's own claim of health. The platform must be able to convict a device that insists it is fine. |
| P5 | **Never destabilize the host** | A component that degrades the device it monitors is worse than no component. Resource ceilings and blast-radius limits are functional requirements, not tuning. |
| P6 | **Install coverage ≠ observation coverage** | Devices that cannot host a node are still observed, by the nodes around them. Universal installation is explicitly not required. |
| P7 | **Show the work** | Every conclusion carries the evidence that produced it, including dissenting evidence and refusals to act. |

---

## 2. The three layers

```
┌──────────────────────────────────────────────────────────────┐
│  HARKEN CENTRAL COMMAND          fleet · learning · approval │
│  "What is wrong across the fleet, and what have we learned?" │
└──────────────────────────────────────────────────────────────┘
                              ▲
                    conclusions up · knowledge and policy down
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  HARKEN SITE MANAGER               facility · fault domains  │
│  "What is wrong in this building?"                           │
└──────────────────────────────────────────────────────────────┘
                              ▲
                   claims and evidence up · commands down
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  HARKEN MESH                        device · peers · seconds │
│  "What is wrong with this device?"                           │
└──────────────────────────────────────────────────────────────┘
```

**Each layer answers a question the layer below it structurally cannot.** A node cannot know that twelve devices across four racks share a failing power feed. A site cannot know that this optic part number fails early across the whole fleet. That separation, not the box diagram, is the architecture.

---

## 3. Harken Mesh

### 3.1 Definition

The Mesh is the layer of **nodes** — units of local intelligence running on or immediately adjacent to physical devices, which observe their own hardware at full fidelity, exchange evidence with their neighbours, and reach conclusions locally.

A node is not an agent that ships data upward. That distinction is the entire product. An agent that collects and forwards has moved the transport, not the reasoning, and leaves the ten hops intact.

### 3.2 Node placement

Placement is ranked by proximity to the hardware. Lower tiers are used only where higher tiers are unavailable.

| Tier | Location | Notes |
|---|---|---|
| **N0** | The device's own control plane | Primary target. Open network operating systems provide sanctioned application-hosting paths. Full-fidelity access, zero additional hardware, no procurement. |
| **N1** | An adjacent processing unit with its own OS and power domain | Survives failure of the host it is attached to. |
| **N2** | A dedicated in-rack node | For estates where N0 is unavailable. Optional and customer-elected; never a precondition for deployment. |

**Explicitly excluded:** any placement inside a general-purpose virtualization cluster. A node separated from its subject by a virtualization layer and multiple network hops cannot observe at the fidelity the design depends on, and defeats the premise.

**R-M1.** The platform must operate with partial node coverage. There is no minimum install percentage below which the Mesh does not function.
**R-M2.** A node must never be a precondition for a device being observed. Devices without nodes are covered under R-M12.

### 3.3 Node responsibilities

**R-M3 — Observe.** A node continuously reads the hardware state of its device at a fidelity unavailable to any remote collector: physical-layer optical measurements, error and drop counters with their reason codes, queue and buffer occupancy, thermal and power state, and control-plane health.

Sampling must be fast enough to capture events that remote polling structurally erases. Two reference cases define the requirement:
- **Microbursts** last microseconds and are completely averaged out of any multi-second sample, yet cause real loss.
- **Pre-correction bit error rate** on optical links rises through orders of magnitude while error correction hides it, leaving the link looking flawless until correction is exhausted and the link fails abruptly. This is the single most valuable leading indicator available and it is only readable at the device.

**R-M4 — Baseline.** A node learns what normal looks like for *this specific device and this specific port*, not against a global threshold. A port that has always run warm is not a fault. Baselines must be per-entity, learned over time, and must survive node restart.

**R-M5 — Detect.** A node identifies deviation from its own learned baseline, distinguishing:
- **load-correlated behaviour** (congestion — tracks traffic volume, buffer-class drop reasons, follows a daily cycle, disappears in the overnight trough), from
- **hardware degradation** (independent of load or worse at low load, physical-class error reasons, frequently one-directional, monotonic over days).

The decisive discriminator is persistence when traffic falls away. This distinction determines whether the platform is trusted; a dispatch triggered by a busy link is more damaging than a missed fault.

**R-M6 — Consult.** On detecting a condition that is ambiguous from one side, a node queries its adjacent nodes for their view before concluding anything. A node must not alert on a single-sided observation where a two-sided one is available.

*Reference case:* receive-side errors on a port are ambiguous — the cable, the local optic, or the far-end optic. The neighbour's view of its own transmit path collapses three candidates to one.

**R-M7 — Conclude.** A node produces a **diagnosis**, not a reading. A diagnosis must contain: the affected component identified specifically enough to be actioned (a part in a slot, not a chassis), the supporting evidence including its time span, a confidence level, contradicting or absent evidence, a predicted trajectory where the fault is progressive, and a recommended action.

**R-M8 — Witness.** A node maintains liveness awareness of its adjacent nodes and **retains their recent state**. When a device fails, the observations it made in the seconds before failing are the most valuable evidence available and the device itself cannot report them. Its neighbours must be holding them.

**R-M9 — Own.** Incident ownership follows a first-claim rule, detailed in §3.5.

**R-M10 — Act.** A node executes actions only from a locally-enforced allow-list, only under signed instruction, with idempotency and expiry. The authority to *refuse* resides on the node. Central-only authorization is one defect away from a fleet-wide event.

**R-M11 — Report refusals.** A refused, rate-limited or expired action is reported with the same weight as a completed one.

**R-M12 — Cover neighbours.** A node observes adjacent devices that host no node of their own — through link state, adjacency data, facing-port counters, and light probing. This is how coverage exceeds installation.

### 3.4 What makes the Mesh intelligent

Four capabilities that no centralized system can reproduce, in ascending order of value.

**(a) Fidelity.** The node sees data that never leaves the box in usable form. Not because it is inaccessible in principle — much of it can be exported — but because exporting it at the resolution that makes it diagnostic is impractical at fleet scale, and nobody does. The node reasons over the raw signal and exports only the conclusion.

**(b) Two-ended correlation.** Faults on a link are ambiguous from either end and unambiguous from both. Two nodes resolve this by asking each other, in milliseconds, with no round trip to a central system.

**(c) Quorum disambiguation.** A central monitor observing a silent device sees exactly one thing: *"it stopped talking to me."* Four materially different situations produce that identical signal. Only peers can separate them:

| Observation | Conclusion |
|---|---|
| All neighbours lost the device; neighbours still reach each other | **The device is down** |
| One neighbour lost it; others still reach it | **That link is down.** The device is healthy |
| Link is up and traffic forwards, but the node is silent | **The node failed. The device is fine** |
| One node lost every neighbour simultaneously | **That node is the isolated one** — it reports on itself |

Row three prevents the fastest possible way to destroy trust: reporting a device down when only the monitoring software fell over. Row four requires a node to be able to conclude that it is the broken party.

**(d) Retained pre-failure evidence.** Peer witnesses convert *"the device stopped responding"* into *"the device is down, and here is what it was doing for the forty seconds before it stopped."*

**R-M13.** A node must not raise an incident on single-node evidence where corroboration is obtainable within the detection window.
**R-M14.** Liveness conclusions require corroboration from at least two independent observers where the topology provides them.

### 3.5 Incident ownership

Distributed detection creates duplicate reports. A device failing in view of three neighbours must produce one incident, not three.

**R-M15 — Claim.** A node that detects a condition broadcasts a claim to its peers. The first claim observed wins. Claims crossing in flight are resolved by a deterministic tiebreak on stable node identity — **not on timestamp**, because clocks on embedded devices drift and time synchronization is unreliable precisely when the network is impaired.

**R-M16 — Subject key.** When a device becomes unreachable, the claim subject is always **the device**, never the link. Otherwise one node claims "the link is down" while another claims "the device is down," the subjects never match, deduplication fails, and duplicate incidents result anyway. Whether the fault is the device or the link is a *conclusion*, reached later; it is never the subject.

**R-M17 — Lease.** Ownership is held under a lease that the owner must renew while investigating. A lapsed lease returns the incident to claim, inheriting the evidence already gathered. This covers the real case where the first detector is itself failing — it claims, then dies, and without a lease the incident is orphaned.

**R-M18 — Boundary.** The owner owns the **investigation**. Resolution workflow — approval, dispatch, closure — belongs to Central Command. Distributed workflow state is not a problem worth having.

**R-M19 — Isolation exception.** A node that cannot reach any peer cannot claim, because claiming requires witnesses. It reports on itself via any remaining path, or buffers and reports on reconnection.

### 3.6 Threshold-triggered claims

Not all faults have a moment. A device that is degrading silently — forwarding, answering health checks, and dropping a small percentage of traffic — never transitions, so there is nothing to claim on.

**R-M20.** Nodes maintain and exchange continuous suspicion state per component and per path, in addition to event-triggered detection. A claim is raised when accumulated cross-node evidence crosses a confidence threshold. The evidence is assembled collectively over hours; the claim marks the moment it became conclusive.

**R-M21.** Where a fault is inferred rather than observed, the platform must identify the **smallest set of components that explains every degraded path while remaining consistent with every healthy path.** Healthy paths carry as much diagnostic weight as degraded ones — the clean paths through a device are what exonerate the device and convict a single port. Without them the recommendation is "replace the switch" when the fault is one optic.

**R-M22.** Where synthetic measurement is used to detect degradation, it must cover every member of a load-balanced bundle. Measurement that follows a single path will validate a healthy member while traffic fails on a broken one, and report everything as fine — failing at exactly the case it exists to catch.

### 3.7 Node constraints

**R-M23.** A node operates under hard resource ceilings, enforced and observable. A node may not compete with the device's primary function.
**R-M24.** A node must not write to device storage at a rate that materially consumes its service life. Embedded devices boot from modest flash with finite write endurance.
**R-M25.** Node failure must be independently detectable and must never be reported as device failure (§3.4 row three).
**R-M26.** A node must survive disconnection from its Site Manager without loss of local detection or diagnosis. It buffers conclusions and reports on reconnection. Telemetry may be shed under sustained buffer pressure; audit records may not.
**R-M27.** Node upgrade is a staged fleet operation with health gates between waves and automatic rollback — not a self-update. A node able to replace its own executable is a node that can be made to install anything.

---

## 4. Harken Site Manager

### 4.1 Definition

The Site Manager is the **facility-level intelligence and coordination layer**. One logical instance per site.

It exists for four reasons that the Mesh cannot satisfy and Central Command cannot satisfy well:

1. **Reach.** A large share of infrastructure cannot host a node — closed management controllers, closed network platforms, power distribution, environmental and facilities equipment. Something inside the site must reach these over their own interfaces.
2. **Altitude.** Fault domains are physical. A failing power feed, a cooling loss, a shared upstream — these present as many simultaneous device faults and are invisible from any single device.
3. **Survivability.** A site must continue diagnosing when its connection to Central Command is lost. Wide-area connectivity is exactly what fails during a significant incident.
4. **Containment.** Commands from outside the site must pass through a point that enforces site-local policy and blast-radius limits before reaching any device.

### 4.2 Responsibilities

**R-S1 — Poll what cannot host a node.** The Site Manager collects from devices that cannot run a node, over whatever interface each exposes — standardized management APIs where available, legacy management protocols, streaming telemetry where supported, and industrial or facilities protocols for environmental and power equipment.

**R-S2 — Emit one schema.** Data from the poll path and data from the node path must be normalized into a single internal representation. If the two paths produce different shapes, every downstream consumer pays for it permanently. This normalization is also where the cross-vendor moat is built: the standardized management API is implemented inconsistently across vendors — differing versions, omitted functions, and the same physical measurement represented differently — and reconciling that is unglamorous, real, and difficult to replicate.

**R-S3 — Hold the site model.** The Site Manager maintains the authoritative picture of the site: physical topology and adjacency, rack and row placement, power and cooling fault domains, which nodes exist and what each covers, and which devices are covered only by neighbour observation. This model is what makes §4.3 possible and what tells the Mesh who should be watching whom.

**R-S4 — Correlate across fault domains.** The Site Manager reaches conclusions that require the site view:
- Many devices degrading together in one physical domain is **one infrastructure fault**, not many device faults.
- Environmental cause and device symptom, correlated — a cooling fault and the thermal responses it produced are one incident.
- Correlated failures spanning separate peer groups, which no single peer group can see.

**R-S5 — Consolidate incidents.** Claims arriving from multiple nodes about one subject are collapsed into a single incident. Where a set of device incidents share a common cause under R-S4, the Site Manager raises the parent incident and subordinates the children — the output is *"power feed A is failing, affecting eleven devices,"* not eleven device alerts.

**R-S6 — Broker commands.** All instructions bound for devices at this site pass through the Site Manager, which:
- verifies authorization before distribution,
- enforces site policy, including domain-aware limits — never all devices in one fault domain in one wave,
- caps concurrency,
- aborts a run when the failure rate crosses a threshold,
- records every instruction and outcome.

**R-S7 — Operate disconnected.** On loss of Central Command, the Site Manager continues to collect, correlate, diagnose and record. It buffers outbound conclusions and reconciles on reconnection. It must **not** grant itself authority it did not already hold — a site in isolation may not self-authorize actions that require central approval.

**R-S8 — Be the only egress.** Nodes do not communicate outside their site directly. This preserves a single enforcement and audit point, and is what makes the credential requirement in §6.3 achievable.

**R-S9 — Survive its own failure.** Site Manager loss must not silence the Mesh. Nodes continue detecting and buffering. Site Manager unavailability is itself an alertable condition, and where availability warrants it the role may be held redundantly.

### 4.3 Decision altitude

| Question | Answered at |
|---|---|
| Is this port's optic degrading? | Node |
| Is the fault my port, the cable, or the far end? | Node pair |
| Is that device down, or just the path to it? | Node quorum |
| Are these nine simultaneous faults one power problem? | Site Manager |
| Did this rack's thermal excursion cause those device errors? | Site Manager |
| Does this optic part number fail early across the fleet? | Central Command |

---

## 5. Harken Central Command

### 5.1 Definition

Central Command is the **fleet-level intelligence, learning, governance and human interface layer**. One logical instance per customer, spanning all sites.

It is deliberately *not* the place where device faults are diagnosed. Its purpose is the set of things that require either a global view or a human.

### 5.2 Responsibilities

**R-C1 — Learn.** This is the layer's primary reason to exist and the one capability that cannot be distributed. A single node might observe a given failure mode twice a year; the signature only emerges across thousands of components and hundreds of failures. Central Command:
- ingests confirmed diagnoses together with what was actually done and whether it worked,
- derives detection and discrimination improvements from the fleet population,
- identifies population-level patterns — component batches, firmware revisions, environmental correlations — that no site can see,
- **distributes improved detection knowledge back down to every node.**

That last item closes the loop. Nodes get better because the fleet taught them, then apply that knowledge locally in milliseconds. This is the concrete meaning of *decide locally, learn globally*.

**R-C2 — Correlate across sites.** Faults spanning facilities, shared upstream dependencies, and the same defect appearing across the estate.

**R-C3 — Present and approve.** The human interface: incidents with their evidence, recommended actions, and the approval gate. Approval must be attributable, revocable, and recorded.

**R-C4 — Govern authorization.** Central Command is the source of authority for every action the platform can take: which action classes are permitted, on which devices, by whom, under what conditions, and which require human approval versus running autonomously. Authorization must be cryptographically verifiable at the point of execution — the node verifies the instruction was authorized, not merely that it arrived over a trusted channel.

**R-C5 — Maintain autonomy posture.** Autonomy is granted per action class, per scope, and is independently revocable, with a global stop. The default posture for every new action class is human-approved. This is a direct response to buyer evidence: operators accept AI analysing sensor data and predicting maintenance, and reject it making configuration changes or controlling equipment. The platform must be sellable to an operator who will never enable autonomous action, and must let one who will enable it do so incrementally.

**R-C6 — Audit.** A complete, tamper-evident record of every conclusion, instruction, execution, refusal and approval, retained to the customer's compliance requirement.

**R-C7 — Integrate.** Outbound to incident and change management, notification, and inventory systems. Inbound from configuration sources of record. The platform must fit an existing operational workflow rather than requiring a new one.

**R-C8 — Hold fleet inventory.** What hardware exists, where, running what firmware, with what history — the substrate for R-C1 and for lifecycle and warranty reasoning.

**R-C9 — Degrade safely.** Central Command unavailability must not stop detection or diagnosis anywhere. It stops *learning distribution*, *cross-site correlation*, and *new authorization* — the last deliberately: an unreachable authority means no new autonomous action, not unsupervised action.

---

## 6. Cross-cutting requirements

### 6.1 Trust and evidence

**R-X1.** Every conclusion carries its evidence, including contradicting evidence and the fact that a device asserted its own health.
**R-X2.** Confidence is explicit and calibrated. A stated confidence that does not match observed accuracy is a defect.
**R-X3.** Where a fault is progressive, output includes a predicted trajectory and a recommended window.
**R-X4.** False positives are more expensive than delayed detection for progressive faults. Where a fault develops over days, the platform should prefer a longer observation window and multiple independent signals over early alerting. Telling an operator that a healthy-looking device is sick, and being wrong twice, ends the relationship.

### 6.2 Action safety

**R-X5.** Every instruction carries: a unique identifier for replay rejection, an expiry after which it must not execute, an explicit action class, and verifiable authorization.
**R-X6.** Local allow-list enforcement at the point of execution.
**R-X7.** Local rate limiting and blast-radius capping, independent of any central limit.
**R-X8.** A local disable mechanism that renders a node inert without requiring physical access.
**R-X9.** Fleet-wide operations run staged with health gates, concurrency caps, fault-domain awareness, and automatic abort on threshold breach.
**R-X10.** Outcomes are explicit. `UNKNOWN` — where the platform genuinely cannot determine whether an action took effect — is a distinct state from success or failure, requires human attention, and must never be silently retried.

### 6.3 Security posture

**R-X11.** The platform must be deployable read-only. Write capability is separately enabled, separately authorized, separately audited.
**R-X12.** A node holds no credential to any system other than its own upward channel. It must never hold credentials to a customer's credential store, identity system, or central data platform. A node that can authenticate to the credential store turns every device into a path to it.
**R-X13.** Node executables are reproducibly built with a published component inventory.
**R-X14.** All inter-layer communication is mutually authenticated and encrypted.
**R-X15.** Compromise of any single node must not yield authority over any other node or over the Site Manager.

### 6.4 Coverage

**R-X16.** The platform reports its own coverage honestly and continuously: which devices have nodes, which are observed only by neighbours, which are polled, which are unreachable, and what capability each level implies. Silent gaps read as clean estates.
**R-X17.** Where the platform bounds its own work — sampling, capping, truncating — it must say so in the output.

---

## 7. Deployment shapes

| Shape | Mesh | Site Manager | Central Command |
|---|---|---|---|
| **Evaluation** | Few nodes, one rack | One, local | Vendor-hosted |
| **Single site** | Full coverage where supported | One, on-premises | Vendor-hosted or on-premises |
| **Multi-site** | Per site | One per site | Central, customer-elected hosting |
| **Sovereign / air-gapped** | Per site | One per site | Fully on-premises; learning distribution operates on a manual cycle |

The sovereign shape is a requirement, not an edge case. Multi-vendor estates are frequently multi-vendor because procurement rules require it, and those same rules commonly prohibit vendor-hosted control planes — a segment structurally unservable by any hyperscaler-hosted or single-vendor offering.

---

## 8. What must be true for this architecture to be right

Stated as falsifiable claims, for review:

1. **Local fidelity produces diagnoses that remote collection cannot.** If a central collector taking high-resolution streaming telemetry reaches the same conclusions with the same lead time, the Mesh is unnecessary and this is a collector product.
2. **Peer evidence resolves ambiguity that single-sided observation cannot.** If two-ended correlation and quorum disambiguation can be reconstructed centrally from both endpoints' exported data at acceptable cost, the peer protocol is an optimization rather than an architecture.
3. **A pre-diagnosed incident materially shortens the human chain.** If operators re-derive the diagnosis before acting on it, the hops do not collapse and the value proposition fails.
4. **Cross-vendor normalization is a durable advantage.** If it turns out to be a few weeks of work, this is a feature that an incumbent adds.
5. **Buyers will deploy software onto network devices.** If the answer is categorically no, only tier N2 remains, and the economics change materially.

Claims 1 and 3 are the ones to test first, and neither requires a complete product to test.

---

## 9. Open questions

| # | Question | Blocks |
|---|---|---|
| Q1 | Where exactly is the boundary between node-local detection and Site Manager correlation? Stated as altitude here; needs to be specified as a rule. | Detailed design |
| Q2 | How is learned knowledge represented such that it can be distributed to constrained nodes and applied locally? | R-C1 |
| Q3 | What is the minimum node coverage, by topology class, for quorum disambiguation to hold? | Deployment guidance |
| Q4 | Does the Site Manager run on customer-provided compute in all shapes, or is an appliance required for some? | Go-to-market |
| Q5 | How does the platform behave in the first days at a site, before baselines exist? | Onboarding |
| Q6 | What is the escalation path when a node's diagnosis and the Site Manager's correlation disagree? | Conflict resolution |

---

## Related documents

- [02 — Market, Comparative Analysis and Build Plan](02-market-and-build-plan.md)
- [03 — Credential Rotation](03-credential-rotation.md)
- [Research: premise evidence](../research/premise-evidence.md)
- [Design: agent feasibility](../design/agent-feasibility.md)
