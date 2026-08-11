# Market, Comparative Analysis and Build Plan — Use Case 1: Network Hardware Fault Diagnosis

**Document 2 of 3**
**Date:** 2026-07-27
**Status:** Draft for review
**Scope:** The evidenced problem, the competitive landscape, where the genuine gap is, market sizing, and the complete set of slices and categories to be built for use case 1.

**Deliberately out of scope:** technology stack.

---

## 1. Executive summary

**The problem is well-evidenced.** Hardware is the dominant failure mode in modern infrastructure, a documented class of hardware fault is structurally invisible to any tool reasoning over logs, and the human cost of triage is quantified from several independent directions.

**The market is more crowded than the original premise assumed.** Cross-vendor hardware monitoring exists. Single-vendor fabric analytics exists and is mature. Grey-failure localization is a decade-old research field with production deployments and claimed near-zero false-positive rates. Vendor-native credential and hardware management exists. Several claims in the original positioning do not survive contact with the evidence and are retired in §5.

**The gap that survives is narrow and real:** nobody performs *cross-vendor diagnostic reasoning* on physical infrastructure, nobody combines path-level inference with on-box physical-layer evidence, and nobody does *peer-to-peer diagnosis* — nodes exchanging evidence to jointly reach a conclusion. The systems that come closest are internal to hyperscalers or locked to a single vendor's fabric.

**The market is adequate but concentrated.** An estimated 500–1,000 realistic accounts. Enough for a substantial business; not obviously enough for a venture-scale one on this use case alone. Growth is concentrated in the segment least able to build the alternative themselves.

**The consistent finding across every line of research:** the differentiation is cross-vendor coverage, not the specific capability. That should be the centre of the pitch, not a supporting point.

---

## 2. The problem, with evidence

### 2.1 Hardware dominates modern failure

| Finding | Source |
|---|---|
| 419 unexpected interruptions across 16,384 accelerators in 54 days — roughly one every three hours. 58.7% accelerator-related; 30.1% device failures, 17.2% memory failures | [Tom's Hardware](https://www.tomshardware.com/tech-industry/artificial-intelligence/faulty-nvidia-h100-gpus-and-hbm3-memory-caused-half-of-the-failures-during-llama-3-training-one-failure-every-three-hours-for-metas-16384-gpu-training-cluster), [DCD](https://www.datacenterdynamics.com/en/news/meta-report-details-hundreds-of-gpu-and-hbm3-related-interruptions-to-llama-3-training-run/) |
| **Over 66% of training interruptions** trace to hardware — memory, processing fabric, and network switch hardware | [Engineering at Meta, July 2025](https://engineering.fb.com/2025/07/22/data-infrastructure/how-meta-keeps-its-ai-hardware-reliable/) |
| IT and networking issues rose to 23% of impactful outages; staff failure to follow procedure grew as a cause | [Uptime Annual Outage Analysis 2025](https://uptimeinstitute.com/resources/research-and-reports/annual-outage-analysis-2025) |

### 2.2 The strongest single argument

**Silent data corruption occurs at approximately one fault per thousand devices, and these errors "do not leave any record or trace in system logs."** Detection "can take weeks or months." ([Meta 2025](https://engineering.fb.com/2025/07/22/data-infrastructure/how-meta-keeps-its-ai-hardware-reliable/), [Meta 2021](https://engineering.fb.com/2021/02/23/data-infrastructure/silent-data-corruption/), [arXiv](https://arxiv.org/pdf/2102.11245))

This is documented proof, from the engineers who found it, that a real class of hardware fault cannot be caught by anything reasoning over logs and traces. It is materially stronger than asserting that application-layer tools "don't touch hardware."

The networking equivalent is **pre-correction bit error rate**. Error correction masks a degrading optical link completely — the post-correction error count reads zero, and every monitoring tool reports the link as healthy — until correction is exhausted and the link fails abruptly. Published practice puts laser bias drift 30–60 days ahead of the error-rate threshold. ([MapYourTech](https://mapyourtech.com/operational-metrics-that-predict-optical-network-failures/))

### 2.3 The human cost

| Finding | Source |
|---|---|
| Toil capped at 50% of an engineer's time; teams above it are "unsustainable." Top sources are interrupts and on-call | [Google SRE Book](https://sre.google/sre-book/eliminating-toil/) |
| The NOC "concentrates toil with hundreds of alerts per hour, dozens of monitoring tools"; triage "frequently remains unfinished" | [BigPanda](https://www.bigpanda.io/blog/triage-agent/) |
| Manual escalation — forwarding, chasing on chat, calling voicemail — named as a direct resolution-time contributor | [OnPage](https://www.onpage.com/what-is-network-operations-center-noc/) |
| 80% of teams actively consolidating observability tools; 73% lack full-stack observability; 97% struggle to realize full value | [Grafana Observability Survey 2025](https://grafana.com/observability-survey/2025/) |
| 46% of operators cannot find qualified candidates; 37% struggle to retain; operations management is the #1 skills gap at 39% | [Uptime Global Survey 2025](https://uptimeinstitute.com/resources/research-and-reports/uptime-institute-global-data-center-survey-2025) (n=800+) |

### 2.4 Multi-vendor is the norm

85% of organizations report multi-vendor equipment; 92% use some combination of pre-owned equipment, third-party maintenance, or multi-vendor sourcing. ([Curvature](https://www.curvature.com/assets/upload/trends-in-data-center-procurement-support-benchmark-report.pdf))

**Caveat:** obtained via search summary; the source PDF would not yield text, so sample size and date are unverified. Directional only until re-sourced. See §10.

---

## 3. Competitive landscape

Seven categories. For each: what it does, and what it does not.

### 3.1 Vendor-native management

**Players:** Dell OpenManage, HPE OneView / InfoSight, Lenovo XClarity, Cisco Intersight.

Deep access to their own hardware, mature, and already deployed at most customers. InfoSight applies predictive analytics across a large install base — but that install base is only ever the vendor's own, since systems "only send device telemetry data" to their own vendor's platform. ([HPE](https://support.hpe.com/hpesc/public/docDisplay?docId=sd00001312en_us&page=GUID-1E54DFBF-A72C-4440-8BCD-507F46C2026D.html))

**Does not:** see or learn from other vendors' equipment. Structurally cannot, without abandoning its own economics.

### 3.2 Cross-vendor hardware monitoring

**Players:** LogicMonitor, ScienceLogic, Zabbix, Checkmk, PRTG, ManageEngine, Dynatrace, plus open collector-and-standardized-API patterns published as reference architecture by cloud providers.

**This category invalidates the original "no tool sees another vendor's boxes" claim.** LogicMonitor ships out-of-the-box hardware health monitoring for major server platforms; equivalents exist across the category. ([LogicMonitor](https://www.logicmonitor.com/support/monitoring/server-operations-hardware/dell-hardware-health-monitoring), [Paessler](https://blog.paessler.com/get-alerted-on-physical-server-health-state-via-idrac-ilo-irmc-and-ipmi), [AWS](https://docs.aws.amazon.com/prescriptive-guidance/latest/bare-metal-hardware-monitoring/introduction.html))

**Does not:** reason. These render a sensor into a dashboard row and fire a threshold alert. None produce a diagnosis, a named part, a trajectory, or a recommended action. **The gap is reasoning and action, not visibility.**

### 3.3 Single-vendor fabric analytics

**Players:** Cisco Nexus Dashboard Insights, Arista CloudVision, Juniper Paragon Insights / Apstra, NVIDIA NetQ.

Genuinely capable within their own fabric — streaming telemetry rather than polling, flow analysis, anomaly detection. Juniper Apstra is claimed to be the only one of its kind with multivendor support, which needs independent verification since the source was vendor-adjacent.

**Does not:** span vendors. A mixed estate is nobody's happy path here.

### 3.4 AI SRE and AIOps

**Players:** Datadog Bits AI SRE, BigPanda, Resolve.ai, Selector, Augtera, LogicMonitor Edwin AI.

Bits AI SRE "only works with Datadog data, has no independent infrastructure graph, and cannot see data outside the Datadog ecosystem." ([atlas](https://xdevops-ai.github.io/devops-sre-ai-atlas-2025/platforms/datadog-bits-ai/)) BigPanda "collects no telemetry of its own" — if nothing upstream understands hardware, neither does it. Augtera explicitly claims grey-failure detection and auto-mitigation.

**Trajectory risk:** HPE OpsRamp is moving directly into this space — predictive analytics for data center operations, an agentic root-cause engine, remediation copilots, and accelerator-cluster observability, connected to networking, compute and operations management. ([HPE, June 2026](https://www.hpe.com/us/en/newsroom/press-release/2026/06/hpe-brings-agentic-ai-into-production-with-nvidia-delivering-security-governance-scale-and-sovereignty.html), [SiliconANGLE](https://siliconangle.com/2026/06/16/hpe-expands-self-driving-networking-strategy-ai-moves-production/))

"Nobody is here" will not be true for long. The defensible version is **"nobody vendor-neutral is here."**

### 3.5 Probing overlays

**Players:** ThousandEyes, Kentik, Catchpoint.

Path-level loss and latency measurement from outside.

**Does not:** read the device. They infer that a path is bad; they cannot say which optic, in which slot, with how long to failure.

### 3.6 Hyperscaler internal systems — the real prior art

| System | What it does | Architecture |
|---|---|---|
| **Pingmesh** (Microsoft) | Latency probe mesh across servers, racks, data centers. 4+ years, tens of TB/day | Agents on **servers**, central analysis |
| **NetNORAD** (Meta) | Loss and latency probing, with parallel path tracing to localize | Agents on **servers**, central aggregation |
| **NetBouncer** (Microsoft) | Active probing plus inference to localize **device and link** failures; ML with domain knowledge; resilient to inconsistent data. 3 years in production, claimed no false positives | Agents on **servers**, **central controller** generates the probing plan and runs inference |
| **FBAR / Cyborg / HALO** (Meta) | On-server fault detection, automated remediation, escalating repair, ML over repair history | Detection **per server**, remediation **central** |
| **Narya** (Microsoft) | Predictive host-failure mitigation; **26% reduction in VM interruptions** | Fleet telemetry, central |

Sources: [Pingmesh](https://www.microsoft.com/en-us/research/publication/pingmesh-large-scale-system-data-center-network-latency-measurement-analysis/), [NetNORAD](https://code.fb.com/core-data/netnorad-troubleshooting-networks-via-end-to-end-probing/), [NetBouncer](https://www.usenix.org/system/files/nsdi19-tan.pdf), [Meta infrastructure](https://engineering.fb.com/2020/12/09/data-center-engineering/how-facebook-keeps-its-large-scale-infrastructure-hardware-up-and-running/), [Narya](https://www.microsoft.com/en-us/research/wp-content/uploads/2020/10/osdi20_mitigation.pdf). Related: 007, deTector, Flock, Canary, SprayCheck.

**Two conclusions, and they pull in opposite directions.**

The mechanism is validated. Observe, reason, act on hardware produces measured returns, and the people who proved it kept the systems.

But **NetBouncer is centralized and it works.** Distributed probes, central inference, three years in production, claimed zero false positives. That is real counter-evidence to the necessity of distributed decision-making — specifically for grey failure. The dead-and-dying-device case holds up far better, because that one genuinely requires local witnesses.

**And the hyperscalers deliberately moved intelligence off the switch.** Centralized network control planes were the entire point of the last architectural generation. HarkenIQ swims against that current. Not fatal — that centralization was about *routing decisions*, where a global view beats local greedy choices, which is a different problem from fault diagnosis — but it must be answerable on demand.

### 3.7 On-switch agent products

**Aviz Networks ONES** — an agent installed on each switch collaborating with a central controller. Multi-vendor across open and proprietary network operating systems, 250+ metrics per device, an anomaly detection engine. ([Aviz](https://aviznetworks.com/products/ones), [Network World](https://www.networkworld.com/article/971728/sonic-builds-muscle-for-enterprise-network-service-in-2023.html))

**This is the closest existing product, and it means on-switch agents are not novel.** No evidence was found of agents communicating with each other — the framing is controller-centric — but that is an inference from published material, not a confirmed fact. See §10.

### 3.8 What already exists that resembles the peer mesh

**Routing protocol adjacencies and bidirectional forwarding detection are already a peer-to-peer liveness mesh on every switch in every data center.** Sub-second dead-neighbour detection, on-box, peer-to-peer, no controller, acting locally by rerouting. There is also published work and shipping intellectual property on switches signalling link failures directly to each other to bypass the controller. ([SDN data-plane fault recovery survey](https://www.sciencedirect.com/science/article/pii/S1319157823000307))

**"My neighbour stopped answering and I act on it" is thirty years old and universal.** This must never be presented as novel — a network engineer will recognize it immediately and stop listening.

The distinction that survives: those mechanisms answer one binary question, *can I still forward through this peer*, and respond only by rerouting. No cause, no evidence, no recommended repair, and nothing said to a human. They route around the wound and stay silent about it. **The diagnosis is the novel part.**

---

## 4. Where the gap actually is

Three findings, ordered by confidence.

**G1 — Cross-vendor diagnostic reasoning does not exist.** Every capable system is vendor-locked (§3.1, §3.3) or reasoning-free (§3.2). Highest confidence; consistent across every line of research.

**G2 — Nobody joins path inference to on-box physical evidence.** Probing systems infer that a path is bad. Optical telemetry pipelines observe that an optic is drifting. The combination — path-level evidence plus physical-layer evidence plus a named replaceable part plus a window — is where the least prior art was found.

**G3 — Peer-to-peer diagnostic reasoning does not exist.** Peer *signalling* exists (§3.8). On-switch *agents* exist (§3.7). Central *inference* exists (§3.6). **Nodes exchanging evidence to jointly reach a conclusion** was not found anywhere, hyperscaler or commercial.

### 4.1 Why an empty space may be empty for good reasons

An honest counter-case, to be argued against rather than ignored:

1. **Forwarding detection already removed the urgency.** Once the network reroutes in tens of milliseconds, nothing is on fire, and diagnosis can be leisurely and central — which is exactly what everyone built.
2. **Switch control-plane capacity is politically expensive.** Vendors and network teams resist anything that could destabilize forwarding. A cultural barrier, not only a technical one.
3. **Central inference demonstrably works** (§3.6), at a quality bar that was cleared without any peer reasoning at all.

The *need* is well-evidenced. It is the *architecture* that is unproven.

---

## 5. Claims retired

Removed from the positioning as unsupported or wrong:

| Original claim | Status | Replacement |
|---|---|---|
| "No vendor's tool sees another vendor's boxes" | **Half wrong** — §3.2 | "Cross-vendor tools display hardware state; none diagnose or resolve across vendors" |
| "AI SRE tools never touch physical machines" | **True today, narrowing** — §3.4 | "No *vendor-neutral* platform reasons about physical infrastructure" |
| Centralized systems "structurally cannot see" physical-layer signals | **Overstated** | The data is exportable and some teams do export it. It is under-used and un-correlated, not invisible |
| Peer detection of a failed neighbour is novel | **False** — §3.8 | The *diagnosis* is novel; the detection is thirty years old |
| Grey failure is unsolved | **False** — §3.6 | Solved if you are a hyperscaler or single-vendor. Unsolved for a mixed estate |
| "10+ hops between fault and fix" | **Unsourced** | Plausible and consistent with how NOC work is described, but no external source documents it. Requires primary interviews before external use |

---

## 6. Market

### 6.1 Sizing

| Figure | Value | Source |
|---|---|---|
| Data centers worldwide, end 2026 | ~8,821 (~3,207 hyperscale) | [ABI Research](https://www.abiresearch.com/blog/data-centers-by-region-size-company) |
| Standing capacity split | 45% colocation, 33% hyperscale, 22% enterprise | [US Data Center Census 2026](https://www.mmcginvest.com/post/the-u-s-data-center-census-2026) |
| New construction pipeline | 81.6% hyperscale, 17.8% colo, **0.5% enterprise** | same |
| Data center switches shipping, 2026 | ~22 million, +14.2% YoY | [Dell'Oro / Next Platform](https://www.nextplatform.com/2026/01/08/pushed-by-genai-and-front-end-upgrades-ethernet-switching-hits-new-highs/) |
| Data center networking market | $55.6B (2025) → $139B (2031), 16.5% CAGR | [MarketsandMarkets](https://www.marketsandmarkets.com/PressReleases/data-center-networking.asp) |

Facility counts vary widely by definition; treat as order-of-magnitude.

### 6.2 Facility count is the wrong denominator

A colocation facility may run a dozen switches of its own. An accelerator cluster runs thousands, where one degrading link stalls a synchronous job across the entire fabric.

**The denominator is switches under management, weighted by incident cost.** 22 million switches a year is not scarcity. Combined with §2.1 — hardware causing two-thirds of training interruptions — the value per incident in an accelerator fabric is in a different bracket entirely. Fewer customers, far higher value each.

### 6.3 Segments, ranked

**1 — Neoclouds and GPU cloud providers. Primary target.**
Named operators including Nscale, Civo, CoreWeave, Crusoe, Lambda, Nebius, Vultr, plus 60+ smaller providers. ~$20B revenue in 2026 trending toward ~$180B by 2030; several growing 200–500% annually. ([ABI](https://www.abiresearch.com/blog/leading-neocloud-companies), [Signisys](https://www.signisys.com/blog/the-neocloud-revolution-how-20-billion-in-gpu-focused-providers-are-reshaping-the-cloud-market/))

Exact profile match: large mixed-vendor fabrics, small operations teams, brutal incident economics, no capacity to build a Pingmesh. Perhaps 60–100 accounts globally, high value each, urgent need.

**2 — Government and sovereign cloud. Underrated.**
Procurement rules frequently *mandate* multi-vendor and commonly prohibit vendor-hosted control planes. Structurally cannot be served by single-vendor fabric analytics or hyperscaler-hosted tooling. Directly motivates the sovereign deployment shape in document 1 §7.

**3 — Colocation. Most numerous, partial fit.**
They run their own fabric, but their acute pain is power and cooling, and much of the equipment in the building belongs to tenants they do not monitor.

**4 — Enterprise. Large installed base, wrong trajectory.**
22% of standing capacity and raw capacity remains stable; 67% report repatriating some workloads. But 0.5% of new construction. Sell opportunistically; do not build for it.

**Unresearched: telcos and edge.** Large distributed footprints, heavily multi-vendor, long history of buying this class of tooling. Gap in the analysis — see §10.

### 6.4 Addressable estimate

Counting operators rather than facilities — single operators run hundreds of sites — the estimate is **1,000–2,000 meaningful non-hyperscale operators**, of which perhaps half have the network scale and vendor mix to need this and no ability to build it. **500–1,000 realistic accounts.**

At $50k–150k annual contract value: **roughly $25M–150M** for network hardware diagnosis alone.

A real company. Probably not a venture-scale one on this scope alone. Two expansion paths: widen from switches to the full hardware estate, or extend from diagnosis into the operational workflow. **The estimate is far more sensitive to contract value than to account count, so pricing should be validated before anything else.**

---

## 7. What to build — slices and categories

Three slices. Every category ranked; build P0 across all three before adding P1.

### 7.1 Slice 1 — Placement

> **AMENDED 2026-07-27 (office hours, premise P1).** The P0 ranking below is correct for the *diagnosis* use case but is **wrong for release one**. Both demand sources run server hardware — a twenty-year production support source on a Dell stack, and an inbound credential rotation request. The switch-first ordering came from installability, not demand. **Release one targets server management controllers.** The ranking below stands for the diagnosis release that follows. See the approved design doc in `~/.gstack/projects/HarkenIQ-harken/`.

*Where the node lives. Ranked by proximity to hardware.*

| | Category | Rationale |
|---|---|---|
| **P0** | **On the device's own control plane** | The switch *is* the edge compute. No hardware ask, no procurement, no rack space, no power. Open network operating systems provide sanctioned hosting. Modern control planes have real spare capacity — nothing like the constrained management-controller case. |
| **P1** | **On an adjacent processing unit** | Own OS and power domain; survives host failure. Extends coverage to the server side. |
| **P2** | **Dedicated in-rack node** | For closed-platform estates. Optional, customer-elected, never a precondition. |
| **✗** | **Inside a virtualization cluster** | Excluded. Defeats the fidelity premise. |

**Clarifying note:** a container running on a switch's own control plane is not the excluded case. It runs on that switch's silicon, adjacent to the forwarding hardware — that is how open network operating systems package all software, including their own routing daemons. The exclusion is about distance from the hardware, not packaging.

**Why the networking focus makes this tractable:** every deployment difficulty in the original analysis came from servers and facilities — closed management controllers, microcontroller-based environmental equipment. Switches are the most installable class of device in the data center.

### 7.2 Slice 2 — Ingest

*How a node gets data. Ranked by fidelity.*

| | Category | What it provides |
|---|---|---|
| **P0** | **Local hardware interfaces** | Reading the box the node lives on: forwarding-silicon counters with drop reason codes, optical measurements including pre-correction error rate, queue and buffer occupancy, thermal, power, control-plane health. Sub-second where it matters. **This is the entire moat.** |
| **P1** | **Peer exchange** | Node-to-node: two-ended queries, liveness, shared conclusions. The layer that makes it a mesh rather than a collection of agents. |
| **P2** | **Neighbour observation** | Covering devices with no node — adjacency, link state, facing-port counters, light probing. How coverage exceeds installation. |
| **P3** | **Streaming telemetry receive** | Devices that push structured telemetry but cannot host a node. Good fidelity, zero install, cheap coverage extension. |
| **Last** | **Polling** | Fallback for closed platforms. Low fidelity. Building on this first would rebuild the system being replaced. |

**Why fidelity ranking matters concretely:** a microburst lasts microseconds and vanishes in any multi-second sample. Pre-correction error rate is masked entirely by error correction. Both are invisible to P3 and P-last, and both are the faults worth catching.

### 7.3 Slice 3 — Intelligence

*What the node computes. This is the build order.*

| | Category | Proves |
|---|---|---|
| **P0** | **Observe and baseline** | Per-entity learned normal, single node, no peers. That you see what nobody else sees. |
| **P1** | **Local diagnosis** | Deviation → named fault, named component, evidence, confidence, trajectory, recommendation. **This is where the hops collapse.** |
| **P2** | **Peer substrate and two-ended correlation** | Localizing faults that are ambiguous from one side. That the mesh does something structurally better, not merely faster. |
| **P3** | **Quorum liveness and witness** | Distinguishing device-down from link-down from node-down from self-isolated, with retained pre-failure evidence. |
| **P4** | **Threshold-triggered inference** | Progressive degradation with no transition event: continuous suspicion, minimal-explanation localization, healthy paths as exonerating evidence. |
| **P5** | **Fleet learning** | Central pattern extraction, knowledge distributed back to nodes. |
| **P6** | **Gated local action** | Small allow-list, signed, human-approved by default. |

**P1 is the value proposition.** Full autonomy is not required to go from ten hops to two — the incident needs to arrive already diagnosed. That also happens to be exactly what buyers said they would accept.

**P2 is where it stops being an agent and becomes a brain**, and it is the hardest capability to copy.

### 7.4 Priority summary

| | P0 | P1 | P2 |
|---|---|---|---|
| **Placement** | Device control plane | Adjacent processing unit | In-rack node |
| **Ingest** | Local hardware | Peer exchange | Neighbour observation |
| **Intelligence** | Observe and baseline | Local diagnosis | Peer correlation |

---

## 8. Phasing

**Phase 1 — See what others cannot.**
Nodes on open network platforms. Local hardware ingest at full fidelity. Per-entity baselines. Output is deviation, not yet diagnosis.
*Exit criterion:* demonstrate a signal, captured locally, that the customer's existing tooling did not surface.

**Phase 2 — Diagnose.**
Deviation becomes a named component, evidence, confidence, trajectory, recommendation. Cross-vendor normalization begins. Polling path for closed platforms, emitting the identical schema.
*Exit criterion:* an operator acts on a diagnosis **without re-deriving it**. This is the single most important test in the plan — if they re-derive it, the hops do not collapse and the value proposition fails.

**Phase 3 — Mesh.**
Peer substrate. Two-ended correlation. Then quorum liveness and witness, which follow cheaply once the substrate exists.
*Exit criterion:* a fault localized from peer evidence that single-sided observation could not resolve.

**Phase 4 — Site altitude.**
Site Manager correlation across fault domains. Parent-child incident consolidation.

**Phase 5 — Progressive faults.**
Threshold-triggered inference. Consider adapting the published inference approach rather than inventing one — that literature is mature and production-proven.

**Phase 6 — Learn and act.**
Fleet learning with knowledge distribution. Gated action, human-approved by default.

**Recommended first slice:** open network platforms only, telemetry path only, plus the closed-platform poller emitting the same schema. No command path. Skip closed platforms for node hosting entirely in the first release.

---

## 9. Risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | **Buyers reject autonomous action.** Operators accept AI analysing sensors and predicting maintenance; they reject configuration changes and equipment control | High | Lead with diagnosis. Human-approved default. Earn autonomy per action class |
| R2 | **A large incumbent closes the gap.** HPE OpsRamp is moving here with hardware, an operations platform and a silicon partnership | High | Vendor neutrality is the defensible position — one they cannot occupy without abandoning their own economics |
| R3 | **Network teams refuse third-party software on switches** | High | Read-only first. Reproducible builds, component inventory, hard resource ceilings. Tier P2 placement as fallback |
| R4 | **Central inference proves sufficient**, as demonstrated in production at a very high accuracy bar | Medium | Weight the architecture argument toward cases where distribution genuinely wins — peer witness, on-box physical layer, no server-side probe fleet required |
| R5 | **Cross-vendor normalization is less durable than assumed** | Medium | If it is weeks of work rather than a sustained moat, the entire strategy needs revisiting. Test early |
| R6 | **Market is too concentrated** — 500–1,000 accounts | Medium | Named-account go-to-market. Validate pricing before anything else; the estimate is far more sensitive to contract value than account count |
| R7 | **The 10-hop narrative does not generalize** | Medium | Source it with primary interviews before it appears in any external material |
| R8 | **False positives destroy trust faster than missed faults earn it** | High | Long observation windows, multiple independent signals, conservative thresholds, evidence always shown |

---

## 10. Validation backlog

Ordered by value.

| # | Action | Resolves |
|---|---|---|
| V1 | **6–10 primary operator interviews** to source the triage chain | R7; the narrative spine of the pitch |
| V2 | **Validate pricing** with 3–5 target accounts | R6; dominates the market estimate |
| V3 | **Aviz ONES trial or technical call** — establish exactly what the on-switch agent computes locally versus ships up | §3.7; closest competitor and possible partner |
| V4 | **Determine what share of network incidents are already auto-mitigated** by routing and forwarding detection before a human is involved | §3.8; sizes the residual problem |
| V5 | **Map the boundary of HPE OpsRamp's hardware-layer capability** | R2 |
| V6 | **Read the published localization paper in full** before designing the inference approach | Phase 5 |
| V7 | **Re-source the multi-vendor statistics** at primary source | §2.4 |
| V8 | **Retrieve two academic papers** that failed extraction — a six-month accelerator-datacenter trace, and a large-scale ML cluster reliability study. Both likely contain manual-diagnosis cost figures | §2 evidence depth |
| V9 | **Dedicated telco and edge segment pass** | §6.3 gap |
| V10 | **Verify the multivendor claim** for the one fabric platform asserting it, independently of vendor-adjacent sources | §3.3 |

---

## Related documents

- [01 — Platform Architecture](01-architecture.md)
- [03 — Credential Rotation](03-credential-rotation.md)
- [Research: premise evidence](../research/premise-evidence.md)
