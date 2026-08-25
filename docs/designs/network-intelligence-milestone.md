# Network Intelligence Milestone — Design + Amendment A9

Date: 2026-08-25. Decisions below were made by Vinod via AskUserQuestion on
2026-08-25; this document assembles them into the dated amendment required by
spec §0 change control. **Status: APPROVED 2026-08-25 (Vinod).** The A9
summary (§6) is recorded in `00-platform-spec.md` §9.

## 1. What this milestone is

The OQ-16 remainder, promoted to its own architectural milestone by Amendment
A8: bring the full Observe → Reason → Act → Verify loop to **network devices**
(switches first). This returns the platform to the doc 01 founding vision —
per-port learned baselines, pre-FEC BER as the leading indicator, microburst
visibility, peer exoneration logic (R-M21) — which R1–R5 proved on servers via
BMC. Proposed slice name: **R6 — Network Intelligence**.

## 2. Decisions (made 2026-08-25, Vinod)

### D3 — Anchor device: community SONiC

- Dev loop runs against the community SONiC container image (free, no vendor
  account, CI-friendly) — the same role MockSimulator + real Dell/HPE played
  for Redfish.
- SONiC is vendor-neutral (matches "they bolt on, we build in" positioning),
  ships a native gNMI server in the `sonic-telemetry` container, and its
  sanctioned container hosting is the N0 placement path from doc 01 §3.2.
- Vendor-specific NOSes (Arista EOS, Cisco NX-OS) are explicitly follow-on
  normalization targets, not v1.

### D4 — Protocol: both gNMI and NETCONF behind DeviceProtocol

- **gNMI is primary** (telemetry): Subscribe-based streaming is the only way
  to meet R-M3's fidelity bar — microbursts and pre-FEC BER are structurally
  erased by polling. OpenConfig paths give the cross-vendor normalization
  seam (doc 08 pattern, new device class).
- **NETCONF is secondary** (config operations): implemented against the same
  DeviceProtocol abstraction.
- **Evidence-based constraint (probed 2026-08-25):** community SONiC ships a
  working gNMI server; NETCONF servers exist only in enterprise SONiC
  distributions (e.g. AsterNOS V6.1R1, Q3 2025) and are not exercisable in
  our dev loop. Therefore (per D7): **NETCONF exit-gates against the
  simulator only, with an explicit open gate — real-device validation
  against a NETCONF-capable target (enterprise SONiC, Cisco, or Juniper) is
  required before any production claim for the NETCONF path.** This gate is
  recorded in §6 below and must not be silently dropped.

### D5 — Placement: N0 on-switch from day one

- The agent runs **as a container on the switch itself** (SONiC application
  container), using the existing `constrained` resource profile (A2.5:
  30/40/50 MB, 2/3/5% CPU) with a systemd/container hard-limit outer layer.
- This directly tests doc 01 §8 claim 5 ("buyers will deploy software onto
  network devices") and delivers the fidelity claim (claim 1) rather than
  drifting into the collector-product failure mode.
- Off-box operation (agent on an adjacent host speaking gNMI to the switch)
  falls out of the same code for free — it is the N1/N2 fallback, not a
  separate deliverable.

### D6 — Action scope: real remediation, fully gated

Network write actions carry outage risk (R4 risk register: read-only by
default; writes require T1 quorum + SM + CC approval). v1 ships the full
A2.1 action pipeline for a network allow-list:

| Action | Risk | Preconditions (draft) | Corroboration | Blast radius (draft) |
|---|---|---|---|---|
| LED locate | Low | — | None | Unlimited |
| Clear interface counters | Low | Counters snapshotted pre-clear | None | Unlimited |
| Interface reset (bounce) | High | Redundant path VERIFIED via peer evidence (LAG member up, or peer confirms alternate path); not an uplink carrying the agent's own SM session; no in-flight config op | T1 quorum + SM + CC approval | 1 per fault domain per 30min; never 2 ports of one LAG |
| Interface disable (shutdown) | High | Same as reset PLUS diagnosis confidence ≥0.8 AND hardware-degradation classification (R-M5), not load-correlated | T1 quorum + SM + CC approval | 1 per fault domain per 30min; never isolates a device |

- Self-preservation invariant (new, network-specific): **an agent must refuse
  any action that would sever its own management path or its host switch's
  last redundant uplink.** Precondition-checked locally and at SM.
- Rollback: disable/reset record pre-state; disable has explicit re-enable as
  rollback action (playbook-grade, per A4.3 executor).
- Exact thresholds are implementation-time tuning within this approved shape;
  widening the allow-list requires a new amendment.

### D7 — NETCONF grounding

Interface + simulator implementation now; real-device validation explicitly
gated (see D4). Keeps the both-protocols architecture without claiming
untested-against-reality coverage.

## 3. Scope of work

1. **NetworkDevice model** — new device class alongside servers: normalized
   interfaces/ports (speed, admin/oper state), optics (Tx/Rx power, temp,
   pre-FEC BER where exposed), error/drop counters with reason codes, queue
   occupancy, control-plane health. Extends doc 08 normalization to a second
   device family.
2. **GNMIProtocol** — DeviceProtocol implementation: Subscribe (streaming,
   sample + on-change), Get, capability discovery; OpenConfig path set for
   interfaces/platform/qos; maps into NetworkDevice. Meets R-M3 sampling.
3. **NETCONFProtocol** — DeviceProtocol implementation: get / get-config /
   edit-config with candidate+commit where supported; used for config-class
   operations and CONFIG_RESTORE parity on network devices. Simulator-gated
   per D7.
4. **Switch simulator** — network analogue of MockSimulator: gNMI server +
   NETCONF server, fault injection (link flap, CRC ramp, optic Rx-power
   decay, pre-FEC BER ramp, congestion/microburst pattern, control-plane
   stall). Drives tests and the exit gate.
5. **N0 packaging** — agent container for SONiC application hosting;
   constrained profile enforcement; install/upgrade story documented.
6. **Baselines + detection on ports** — per-entity (per-port) Welford
   baselines and trending (reuse R1 engines); load-correlated vs
   hardware-degradation split per R-M5; two-device correlation probe
   (R3b-2 CorrelationProbe) extended with real port counters from gNMI.
7. **Network action executor path** — the D6 allow-list through the existing
   ActionExecutor / preconditions / audit / approval chain; directives
   transport (R5-1) reused unchanged.
8. **SM/CC surfaces** — network devices in site model + fleet views; port-level
   incident detail; correlation rules for TOR/link fault domains (the
   Connectivity row of A2.6 becomes fully real).
9. **Full loop proof** — Observe→Reason→Act→Verify on a switch, end-to-end.

Out of scope (explicit): routers beyond what the simulator models, SNMP
legacy fallback, vendor NOSes other than SONiC, NETCONF production claim
(gated), network topology auto-discovery beyond existing LLDP-adjacent site
model inputs.

## 4. Exit gate (draft)

Two agents on two simulated switches + one real community-SONiC container:

1. gNMI streaming from the real SONiC container into NetworkDevice model,
   per-port baselines learned, visible at SM.
2. Injected optic-degradation ramp (simulator) → agent classifies hardware
   degradation (not congestion) → diagnosis with peer exoneration (one optic
   convicted, switch exonerated, R-M21).
3. LINK_DOWN two-sided probe returns CABLE/LOCAL_PORT/REMOTE_PORT correctly
   on injected faults.
4. Interface disable executes ONLY through T1 quorum + SM + CC approval with
   redundant-path precondition proven; self-preservation refusal test passes
   (action targeting own uplink is refused).
5. Agent runs inside the SONiC container (N0) within constrained profile.
6. Full suite green; NETCONF paths green against simulator, real-device
   NETCONF gate recorded as open.

## 5. Open items created by this amendment

**Per the A3.1 convention (spec §9): no R6 phase depends on physical
hardware. Real-hardware validation is a design-partner-site event, never a
build dependency.** All code builds now against the switch simulator +
`docker-sonic-vs` (real SONiC software on virtual hardware); each gate below
closes when the already-built capability is validated at the first design
partner site with network devices.

| Item | Gate |
|---|---|
| NETCONF real-device validation | Validated at first design partner site with a NETCONF-capable device (enterprise SONiC / Cisco / Juniper); before production claim |
| gNMI Set write actions on real SONiC | Set support is partial/version-dependent on community SONiC; P0 captures actual capability; validated at first design partner site before production claim (T5) |
| Optics / pre-FEC BER / microburst observability | docker-sonic-vs has no ASIC, optics, or FEC — these detections are simulator-proven until demonstrated at a design partner site with real switch silicon (T5); this demonstration IS the doc 01 §8 claim-1 test |
| N0 on a real switch | Virtualized SONiC does not test ARM CPUs, flash budget, app-extension packaging/signing, or mgmt-VRF mesh reachability; validated at first design partner site (claim-5 test) (T5) |
| Vendor NOS normalization (Arista/Cisco) | Follow-on slice after R6 |
| Router device class | Follow-on; simulator models switches first |

## 4a. Exit gate — RESULTS (run 2026-08-25)

| §4 item | Result | Evidence |
|---|---|---|
| 1. Real SONiC streams into NormalizedDevice | **PASS (live)** | GNMIProtocol vs docker-sonic-vs + docker-sonic-gnmi pod (TLS, TOFU-pinned, CN override): identity sonic/Force10-S6000/switch, 32 interfaces, Subscribe SAMPLE streamed counters on all 32 ports, rates derived, 32 config keys. Fidelity finding folded back: the real server resolves the DB target from the request PREFIX (path-level targets NOT_FOUND) — protocol and simulator both corrected |
| 2. Optic decay → hardware degradation w/ exoneration | PASS (suite) | test_network_detection: injected decay classified CRITICAL hardware; healthy port stays HEALTHY (R-M21 spirit); congestion negative branch proves R-M5 |
| 3. Two-sided probe verdicts | PASS (suite) | LOCAL_PORT / CABLE / INCONCLUSIVE on real interface counters |
| 4. Gated disable + self-preservation refusal | PASS (suite) | executor+protocol disable with read-back verify; refusals: self-preservation (incl. fail-closed resolution), redundant-path, quorum propose-only, LAG blast radius, D16 denial finality |
| 5. Agent in container within profile | **PASS (live)** | 43.3MiB steady-state under an ENFORCED 50MiB cap (server workload); network agents run standard per D12; containerized gnmi agent full Observe loop proven (compose smoke = CI smoke) |
| 6. Suite green; NETCONF honesty | PASS | 2155 passed / 2 expected skips; P5 dropped by D13 — §5 partner-site gates recorded |

**R6 exit gate: GREEN.** Simulator-scoped items are labeled per §5; the
partner-site gates (gNMI Set persistence, optics/BER/microburst on silicon,
N0 on a real switch) remain open by design and close at the first design
partner with network devices.

## 6. Amendment A9 summary (recorded in spec §9, 2026-08-25)

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

## 7. Implementation plan (drafted 2026-08-25)

### Grounding — the seams R6 plugs into (verified in code)

- `src/harkeniq/protocols/device.py` — DeviceProtocol already names gNMI/NETCONF
  as intended implementations; factory at `create_device_protocol()` takes new
  branches. Contract: `poll_sensors()` returns a `NormalizedDevice`; everything
  above the boundary is protocol-agnostic (verified 63% in R4-0, exercised by
  IPMI in R4-1).
- `src/harkeniq/redfish/normalize.py:168` — `NormalizedDevice` is the canonical
  container (fans/disks/memory/psus/thermals + logs + rollup).
- `src/harkeniq/skills/engine.py` — skills evaluate ANY per-sensor dataclass via
  `dataclasses.asdict`; `skills/loader.py:32 VALID_TARGETS` and
  `agent.py _TARGET_COLLECTIONS` are the only two registration points for a new
  collection. Baselines/trending are per-entity by sensor id — per-port works
  unchanged.
- `src/harkeniq/actions/executor.py` — protocol-dispatched actions with agent-side
  allow-list/preconditions/audit (R4-1); directives transport (R5-1) reused as-is.
- `src/harkeniq/mock/` — MockSimulator (Redfish) + MockIPMIBMC set the pattern
  for the switch simulator.

### Engineering decisions (reviewed and locked, /plan-eng-review 2026-08-25)

1. **Model shape: extend `NormalizedDevice` additively** — add
   `interfaces: list[NormalizedInterface]` (+ optics data nested per interface,
   + `interface` field on `HealthRollup`, + `device_class: "server"|"switch"` on
   `DeviceIdentity`). NOT a parallel NetworkDevice container. Rationale: the
   DeviceProtocol contract, skills engine, baselines, state machine, and SM
   ingest all consume NormalizedDevice; an additive collection reuses every
   layer unchanged (empty list on servers = zero behavior change). "NetworkDevice
   model" from A9 = these new collections + the device_class marker.
   **Review 6A:** the canonical model moves OUT of `redfish/normalize.py` into a
   neutral `protocols/model.py` as the first P1 step, with a re-export shim left
   at the old path so existing imports keep working; `NormalizedInterface` is
   born in the neutral module.
2. **Streaming-to-poll bridge in GNMIProtocol** — the Poller stays pull-based.
   GNMIProtocol runs a background gNMI Subscribe stream into an internal cache;
   `poll_sensors()` snapshots the cache. R-M3 fast-sampling is preserved by
   computing **stream-derived features inside the protocol** between polls
   (e.g. `queue_occupancy_max`, `crc_err_rate_max`, pre-FEC BER trend over the
   window) and exposing them as interface fields — fast events are captured via
   queue watermark / peak counters **where the platform exports them** (verified
   at the P0 spike; docker-sonic-vs has no ASIC, so true microburst fidelity is
   a real-hardware open gate per §5), surfaced at poll cadence, without
   re-architecting the agent loop.
   **Review 2A (staleness — silent = unobserved, never healthy):** cache entries
   carry last-update timestamps; `poll_sensors()` raises `TimeoutError` when the
   stream is stale past a threshold (default 3× the subscribed sample interval),
   so a dead stream surfaces as device-unreachable through the existing poller
   paths. The protocol reconnects with exponential backoff.
   **Review 9A (bounded cost):** every stream-derived feature is a constant-space
   running accumulator (max, EWMA, Welford), reset at poll snapshot — never a
   sample buffer. P3 includes a load test streaming a simulated 64-port switch
   at high rate and asserting RSS/CPU stay inside the constrained profile.
3. **gNMI client (review 4A):** raw `grpcio` + a compiled `gnmi.proto` via the
   repo's existing proto toolchain — no pygnmi dependency. Stream lifecycle
   (staleness, reconnect, backoff) is custom per decision 2 regardless.
4. **Self-preservation mechanism (review 3A — fail-closed):** at precondition
   time the agent resolves its SM address → route table → egress interface, then
   expands LAG membership; the action is refused if the target is in that set OR
   if resolution fails or is ambiguous. Refusals are recorded with reason
   (R-M11). SM independently re-checks from its topology model. A safety check
   that cannot prove safety refuses — same posture as every A2.1 precondition.
5. **Per-port upstream policy (review 10A — ship conclusions, not telemetry):**
   SM receives per-port health rollups and periodic baseline digests on the
   normal cadence; FULL per-port metrics flow upstream only for ports that are
   anomalous or under active diagnosis, plus on-demand via the probe path. Raw
   per-port streams never leave the agent. This keeps the doc 01 §8 claim-1
   posture: the node reasons locally; SM gets conclusions and evidence.
6. **INTERFACE_ENABLE is a first-class action (review 7A):** rollback runs
   through the same allow-list/preconditions/audit as everything else (R5-1
   no-bypass rule), so enable gets its own `ActionType`: risk LOW only when a
   recorded HarkenIQ pre-state exists for that port (restore semantics);
   an enable with no pre-state classifies HIGH like disable.
7. **Counter→rate derivation layer (outside voice T4):** interface error/drop
   counters are monotonic; Welford baselines and trending assume gauges.
   Baselines, skills, and the probe consume RATES (delta over wall-clock
   interval) computed in the protocol layer, with counter-wrap detection and
   known-reset suppression. `CLEAR_COUNTERS` emits a baseline-suppression
   event to the trending engine — a zeroed counter must never read as
   recovery. Stream feature windows are keyed by wall-clock interval, never
   reset-on-read, preserving the `poll_sensors()` idempotency contract.
8. **CompositeProtocol (outside voice T3):** a switch speaks gNMI (telemetry)
   and NETCONF (config ops) simultaneously, but the factory hands a device one
   protocol. `CompositeProtocol(telemetry=..., config=...)` implements
   DeviceProtocol; poll/collect route to the telemetry leg, config-class
   actions to the config leg via an explicit per-action-type routing table
   (data, not code). Factory shape: `bmc.protocol: gnmi+netconf`.
9. **T1 quorum corroboration gate (outside voice T2):** tier gating exists
   (`autonomy/tier.py`) but NO action precondition consumes it today — the
   gate is new machinery, designed explicitly in P6: T1 = ≥2 peers whose
   witness/suspicion evidence is consistent with the diagnosis, evaluated as
   an executor precondition; degraded topologies fall to propose-only.
10. **Network fault domains defined in P1 (outside voice T7):** containment
    hierarchy port → parent LAG → switch → site segment lives in the model;
    blast-radius enforcement keys on the LAG and switch levels in v1. The
    minimal site-model support lands BEFORE P6 (P7 keeps the full UI/site
    work) — the safety limiter never ships before its vocabulary.

### Phases

**R6-P0 — Reality spike (outside voice T1; ~1–2 days, evidence not code).**
(a) **Resource budget:** run the agent under load in a container and measure
RSS/CPU. Measured fact motivating this: bare import of
`grpc+httpx+cryptography+yaml+harkeniq.agent` already peaks at **59.7MB**,
above the constrained profile's 50MB hard limit — N0-on-constrained is
arithmetically false today. Outcome = a decision from data: trim/lazy-load,
revise the profile by amendment, or assign switches a different profile.
(b) **gNMI ground truth:** run `docker-sonic-vs`, capture its ACTUAL gNMI
capabilities — paths, encodings, Subscribe modes, Set support — and make that
capture the P2 simulator's fixture (never our reading of the OpenConfig spec).
(c) **NETCONF go/no-go (T8, decided by Vinod):** if the captured gNMI Set
support covers R6's admin-state + CONFIG_RESTORE needs, NETCONF defers
wholesale to the §5 real-device gate by amendment and P5 is dropped from R6;
otherwise P5 proceeds as planned. *Gate: written spike report with the three
outcomes recorded.*

**R6-P1 — NetworkDevice normalization model.**
Step 1 (decision 1/6A): move all `Normalized*` classes to `protocols/model.py`;
leave a re-export shim at `redfish/normalize.py`. Step 2: `NormalizedInterface`
(name, admin/oper state, speed, counters {rx/tx errors, CRC/FCS, drops with
reason class}, queue occupancy, optics {tx/rx power dBm, temp, pre-FEC BER},
stream-derived feature fields), `device_class` on DeviceIdentity, `interface`
in HealthRollup + rollup computation. Register `interface` in `VALID_TARGETS`
(field list) and `_TARGET_COLLECTIONS`.
Tests: model round-trip, rollup, skill-engine evaluation against an interface
reading; **shim test — every pre-move import path still resolves**. Per
decisions 7 and 10: interface fields are **rate-typed where the source is a
monotonic counter**, and the model carries the fault-domain containment
hierarchy (port → LAG → switch → segment). No protocol yet. *Gate: existing
2049 tests untouched and green.*

**R6-P2 — Switch simulator.**
`mock/switch_sim.py`: in-process switch state model (N ports, LAGs, optics,
counters, queues) + fault injection API (link_flap, crc_ramp, optic_rx_decay,
prefec_ber_ramp, congestion_burst, control_plane_stall) + **gNMI server**
(grpcio, OpenConfig-path Subscribe/Get/Set over the state model). Mirrors
MockIPMIBMC: in-process for tests, runnable standalone for compose. NETCONF
endpoint deferred to P5 (arrives with its client). Tests: fault injections
visible through gNMI Subscribe; **malformed/unsupported gNMI path request
returns a proper error, never a crash or silent empty**.

**R6-P3 — GNMIProtocol.**
`protocols/gnmi.py` implementing the full DeviceProtocol surface: connect
(gRPC channel, TLS + credentials), identity via OpenConfig platform,
Subscribe-cache + `poll_sensors()` snapshot + stream-derived features (decision
2), `collect_config()` via Get(config), `collect_firmware_inventory()` via
platform components, `execute_action()` mapping (P6 wires the new types).
Factory branch `gnmi`. Client: **grpcio + compiled gnmi.proto** (matches the
"existing compiled gRPC proto" convention; avoids a pygnmi dependency).
Tests vs P2 simulator; **opt-in live probe vs a `docker-sonic-vs` community
SONiC container** (same pattern as the R4-1 LLM live probe). Named tests
(review 8A/2A/9A): stale stream → `TimeoutError`; stream drop → reconnect with
backoff and cache invalidation; feature-window resets on poll (no
double-counting); auth failure → `ConnectionError`; LED unsupported → refused,
never faked; **resource load test** — simulated 64-port stream at high rate,
RSS/CPU asserted within the constrained profile. *Gate: real SONiC streams
into NormalizedDevice.*

**R6-P4 — Per-port baselines, detection, probe integration.**
Per-interface Welford baselines + trending through the existing engines;
R-M5 load-vs-degradation classifier for ports (load-correlated counters vs
monotonic one-directional physical errors); built-in network skills
(optic-degradation, crc-ramp, congestion) in skill YAML; `CorrelationProbe`
(R3b-2) fed by real gNMI counters instead of stubs. Tests: injected optic
decay classified as hardware degradation with congestion exonerated (R-M21
smallest-set logic on ports); **congestion pattern classified load-correlated
with NO action proposed** (the negative branch is first-class).

**R6-P5 — NETCONFProtocol: DROPPED from R6 (P0 go/no-go, decided by Vinod
2026-08-25, D13).** Evidence in `docs/designs/r6-p0-spike-report.md` §(c):
NETCONF does not exist on community SONiC; gNMI Set is the anchor's only
write path. NETCONF defers wholesale to the §5 real-device gate and lands
later behind the CompositeProtocol seam (decision 8) without redesign. R6
action transport = gNMI Set with **mandatory read-back verification** (a
SetResponse is never proof — the spike showed accepted-but-not-persisted).
The phase text below is retained for that future slice:
`protocols/netconf.py` via **ncclient** (get / get-config / edit-config,
candidate+commit where advertised); NETCONF endpoint added to the P2 simulator;
CONFIG_RESTORE parity for network devices. Every test marked with the
simulator-only caveat; the open real-device gate is asserted in docs, not
silently passed. Named tests (8A): auth failure; malformed response; **both
capability branches — candidate+commit AND writable-running**. *Gate: NETCONF
suite green vs simulator; no production claim.*

**R6-P6 — Network actions.**
`ActionType` additions: `CLEAR_COUNTERS` (low), `INTERFACE_RESET` (high),
`INTERFACE_DISABLE` (high), `INTERFACE_ENABLE` (first-class per decision 6/7A:
LOW with recorded pre-state, HIGH without); IDENTIFY_LED mapped where the NOS
exposes it, else refused (never faked). Preconditions per §2-D6 including
**redundant-path verification** split per outside voice T6 — agent-local: LAG /
port-channel redundancy from the agent's OWN gNMI data; cross-device: SM
verifies an alternate path against the confirmed site model at approval time
("peer confirms" is an SM check, not a mesh message); switch↔server links are
**probe-blind in v1** and the plan says so — and the **self-preservation
invariant** implemented per decision 4/3A (live route lookup, LAG expansion,
fail-closed) enforced agent-side AND SM-side. The **T1 quorum corroboration
gate is designed and built here** per decision 9 (executor precondition
consuming peer witness/suspicion evidence; ≤1 peer ⇒ propose-only; named
tests for the degraded-topology branches). Depends on P1's fault-domain
hierarchy (decision 10).
High-risk chain: T1 quorum + SM + CC approval through the existing approval/
directive machinery. Blast radius: 1/fault-domain/30min, never 2 ports of one
LAG. Rollback: disable→enable playbook-grade with pre-state. Named tests (8A):
self-preservation refusal incl. the resolution-failure fail-closed branch;
redundant-path unverifiable → refuse; **second port of same LAG in window →
refused**; **approval DENIED → action final, never re-queued (D16)**; approval
lease expires mid-flight; enable-without-pre-state classifies HIGH.

**R6-P7 — SM/CC/Console surfaces.**
`device_class` through AgentRegistration/FleetDevice proto (additive tags, as
R4-2 did); SM site model + fleet views show switches; port-level incident
detail; A2.6 Connectivity correlation row (TOR/segment, 3 devices/15s) becomes
fully live with switch fault domains; CC fleet + Console fleet list render
device_class. **Upstream policy per decision 5/10A:** per-port health rollups +
periodic baseline digests on cadence; full per-port metrics only for
anomalous/diagnosed ports and on-demand (probe path). Tests: mixed
server+switch site correlates a TOR event into one parent incident; **proto
compat matrix — old agent ↔ R6 SM and R6 agent ↔ old SM both register and
poll cleanly (regression-class)**; **mixed-fleet Console rendering — existing
server rows byte-identical behavior (regression-class)**; digest-vs-full
upstream branches both exercised.

**R6-P8 — N0 packaging + exit gate.**
Agent container image with `constrained` profile enforcement (A2.5 hard
limits); documented SONiC app-container install; compose profile
`network-sim` (agent + switch sim + optional docker-sonic-vs).
**Distribution (review 5B — full pipeline, decided by Vinod):** versioned
Dockerfile; CI workflow (GitHub Actions) building multi-arch (amd64+arm64)
images and publishing to GHCR on version tag; image tag = agent version;
install docs reference the registry image. The §4 exit gate runs against the
PUBLISHED image, not a local build. Run the §4 exit gate end-to-end; record
results; ledger row + A9 closure notes.

### Dependencies and risks

| Item | Note |
|---|---|
| grpcio (present) + gnmi.proto compile | New proto compile step for gNMI; same toolchain as existing proto |
| ncclient (new dep, P5) | NETCONF client; simulator-gated scope contains the risk |
| docker-sonic-vs image | Community SONiC virtual switch for the live probe + exit gate item 1; if its gNMI surface lags OpenConfig paths, the probe narrows to what it serves — recorded, not papered over |
| Proto changes (P7) | Additive tags only, both ends tolerant of absence (R4-2 precedent) |
| Stream cadence vs constrained profile | Subscribe volume must fit 30–50MB / 2–5% CPU; P3 measures and tunes sample intervals per path class |

Order is strict P0→P8; each phase lands with the full suite green (spec §7
rule). P5 (NETCONF) is conditional on the P0 go/no-go (T8) and can slide
after P6/P7 without breaking anything — the one permissible resequencing.

### What already exists (reuse inventory)

| Existing component | R6 reuse |
|---|---|
| Welford baselines + OLS trending (R1) | Per-port baselines unchanged — fed RATES per decision 7 |
| Skills engine generic dataclass eval | `interface` target = pure registration (2 seams) |
| CorrelationProbe (R3b-2) | Fed real gNMI counters instead of stubs |
| ActionExecutor + allow-list/audit + R5-1 directives | Network actions ride the identical chain |
| Resource profiles + degradation ladder (A2.5) | N0 container enforcement, P0-validated |
| `autonomy/tier.py` calculate_tier + witness/suspicion | Composed into the P6 quorum gate (new gate, existing evidence) |
| MockIPMIBMC in-process double pattern | Switch simulator follows it |
| Proto compile toolchain + grpcio | gnmi.proto client (decision 3) |
| Approval brokering SM→CC (R2a/R2b) | High-risk network approvals unchanged |

Nothing in R6 rebuilds an existing flow; every phase attaches to a verified seam.

### NOT in scope (considered and explicitly deferred)

- **Routers** — simulator and model target switches first; router class is a follow-on.
- **SNMP legacy fallback** — not needed for the anchor device; revisit with vendor NOS breadth.
- **Vendor NOSes (Arista EOS, Cisco NX-OS)** — normalization follow-on after SONiC proves the model.
- **NETCONF production claim** — barred until real-device validation at a design partner site (§5); P5 itself conditional on P0 evidence.
- **Server-side NIC interface collection** — TODOS.md N1 (post-R6); switch↔server links are probe-blind in v1 and the plan says so.
- **Real-hardware demonstration of fidelity claims** — design-partner-site event per A3.1; never a build dependency.
- **Network topology auto-discovery beyond existing site-model inputs** — LLDP-adjacent inference only.

### Failure modes (new codepaths)

| Codepath | Realistic failure | Test? | Handled? | User sees |
|---|---|---|---|---|
| Subscribe stream | Silent gRPC half-open death | 2A test | TimeoutError → unreachable | Device unobserved, correct |
| Stream reconnect | Flapping channel | 8A test | Backoff + cache invalidation | Gap marked, no stale-healthy |
| Rate derivation | Counter wrap / device reset | 7 tests | Wrap detect + reset suppression | No false CRC storm |
| CLEAR_COUNTERS | Baseline poisoning | 7 test | Suppression event to trending | No false recovery |
| Self-preservation | Route lookup fails | 3A test | Fail-closed refuse + audit | Refusal with reason |
| Blast radius | 2nd LAG port in window | 8A test | Refused | Refusal with reason |
| Approval | Denied / lease expiry | 8A tests | Final per D16 / propose-only | Audited terminal state |
| Quorum gate | ≤1 peer topology | T2 tests | Propose-only | Human decides |
| Proto compat | Old agent ↔ new SM | 8A matrix | Additive tags tolerant | No fleet breakage |

**Critical gaps (no test AND no handling AND silent): 0** — every candidate
acquired a named test or handler during review.

### Worktree parallelization

| Lane | Steps | Depends on |
|---|---|---|
| A (agent core) | P0 → P1 → P3 → P4 → P6 | sequential; P6 also needs P1 fault domains |
| B (simulator) | P2 | P0 fixture capture; parallel with P1 |
| C (SM/CC/Console) | P7 | P1 proto fields; parallel with P4–P6 |
| D (packaging) | P8 | all lanes merged |

Launch A and B in parallel worktrees after P0; C forks after P1 lands; P8 is
the merge point. Conflict flag: lanes A and C both touch the proto — land
P1's proto change first, then no overlap.

### Implementation Tasks

Synthesized from review findings; checkbox as shipped.

- [ ] **T1 (P1, CC: ~30min)** — P0 spike: containerized RSS/CPU measurement + sonic-vs gNMI capability capture + NETCONF go/no-go — *outside voice #1/#6/T8*
- [ ] **T2 (P1, CC: ~10min)** — Move Normalized* to `protocols/model.py` + shim — *finding 6A*
- [ ] **T3 (P1, CC: ~40min)** — NormalizedInterface with rate-typed fields + fault-domain hierarchy — *findings 8A/T4/T7*
- [ ] **T4 (P1, CC: ~20min)** — Staleness threshold → TimeoutError in GNMIProtocol — *finding 2A*
- [ ] **T5 (P1, CC: ~40min)** — Counter→rate derivation (wrap/reset) + CLEAR_COUNTERS suppression + wall-clock windows — *outside voice #4/#11*
- [ ] **T6 (P2, CC: ~20min)** — CompositeProtocol + action routing table — *outside voice #3*
- [ ] **T7 (P2, CC: ~40min)** — T1 quorum corroboration gate as executor precondition — *outside voice #2*
- [ ] **T8 (P2, CC: ~30min)** — Self-preservation route lookup, LAG expansion, fail-closed — *finding 3A*
- [ ] **T9 (P2, CC: ~10min)** — INTERFACE_ENABLE first-class w/ pre-state risk split — *finding 7A*
- [ ] **T10 (P2, CC: ~20min)** — Redundant-path split: local LAG check + SM site-model check — *outside voice #7*
- [ ] **T11 (P2, CC: ~20min)** — O(1) accumulator rule + 64-port load test — *finding 9A*
- [ ] **T12 (P2, CC: ~20min)** — Per-port digest upstream policy in P7 — *finding 10A*
- [ ] **T13 (P2, CC: ~30min)** — GHCR multi-arch publish pipeline; exit gate runs published image — *finding 5B*
- [ ] **T14 (P2, CC: ~1h)** — The 11 named test gaps across phases — *finding 8A*

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | scope set by Amendment A9 (Vinod) instead |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | codex not authed |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 9 issues + 12 outside-voice findings, all resolved; 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | minor UI surface only (P7) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | not run |

**CROSS-MODEL:** Outside voice (Claude subagent, fresh context) surfaced 12
findings incl. 3 verified-by-measurement (59.7MB import RSS vs 50MB hard cap;
no quorum action gate; gauge-only baselines). One disagreement (cut NETCONF)
resolved by Vinod as evidence-gated: P0 spike decides (T8). Hardware framing
corrected to the A3.1 design-partner convention after Vinod flagged the
deviation (D11).

**VERDICT:** ENG CLEARED — ready to implement (P0 first).

NO UNRESOLVED DECISIONS
