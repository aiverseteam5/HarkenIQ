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

| Item | Gate |
|---|---|
| NETCONF real-device validation | Access to enterprise SONiC / Cisco / Juniper target; before production claim |
| Vendor NOS normalization (Arista/Cisco) | Follow-on slice after R6 |
| Router device class | Follow-on; simulator models switches first |

## 6. Amendment A9 summary (copy to spec §9 on approval)

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
