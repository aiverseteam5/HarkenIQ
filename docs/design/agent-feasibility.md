# Rust Edge Agent — Feasibility and Design Notes

**Date:** 2026-07-27
**Scope:** A Rust agent installed on data center devices that (a) pushes telemetry to Kafka and (b) receives and executes instructions (restart, config change).
**Status:** Design assessment. Companion to [premise-evidence.md](../research/premise-evidence.md).

---

## 1. The blocking constraint: "install on anything" is not available

The plan assumes a deployment target that mostly does not exist. Data center devices fall into three tiers, and only one of them accepts a binary.

### Tier A — You can install a binary today

| Target | How | Notes |
|---|---|---|
| Server host OS (Linux) | systemd unit, package, or container | Trivial. Dies when the host dies — which is when you need it most. |
| SONiC switches | Debian base, Docker containers | Best-in-class target. Community NOS, no gatekeeper. |
| NVIDIA Cumulus Linux | Debian, `apt install` | Open by design. |
| Arista EOS | `.swix` extensions, RPMs, container support | Officially sanctioned app hosting. |
| Cisco NX-OS | Guest Shell (LXC), Docker on some platforms | Officially sanctioned, resource-capped. |
| Cisco IOS-XE / IOS-XR | IOx app hosting, LXC/Docker | Catalyst 9000 and similar. Supported path. |
| NVIDIA BlueField DPU / SuperNIC | Runs Ubuntu on ARM cores (DOCA) | Excellent target — survives host failure, sits on the data path. |
| OpenBMC servers | Yocto recipe baked into firmware image | Only if you control the firmware build. Rare outside hyperscalers and ODM deals. |

### Tier B — Closed firmware, API access only

| Target | Reality |
|---|---|
| Dell iDRAC, HPE iLO, Lenovo XCC, Supermicro BMC | **No third-party code execution path. At all.** Signed vendor firmware. Redfish/IPMI/SNMP is the entire surface. |
| Junos (most platforms) | Limited app hosting; JET APIs are the practical interface. |
| Storage arrays | Vendor API only. |

Given that BMC firmware "executes outside the scope of operating system controls," and that CISA added its first BMC CVE to the Known Exploited Vulnerabilities catalog in June 2025, the closed posture here is not going to loosen. Assume Tier B is permanently API-only.

### Tier C — Not computers in the sense you need

| Target | Reality |
|---|---|
| Temperature controllers, CRAC/CRAH | Proprietary firmware, often bare-metal microcontrollers. **No OS, no filesystem, no place to put a binary.** Speak Modbus RTU/TCP or BACnet/IP. |
| PDUs, UPS | SNMP, occasionally a limited REST API. Some Vertiv/Raritan units have scripting hooks; none accept arbitrary binaries. |
| BMS / building controls | BACnet. Usually a different network *and a different department*. |

**The organizational blocker matters as much as the technical one.** Tier C is operational technology — owned by facilities, not IT, frequently on a segmented or air-gapped network, and governed by a separate change-control regime. "We'd like to install our agent on your chiller controller and let it accept restart commands" is a conversation that ends quickly.

### Consequence

The architecture has to be **agent where you can, poller where you can't** — and the poller tier is not a fallback or a phase-two item. It is how you reach the majority of device classes, permanently. Design it as a first-class citizen from day one, with one normalized internal schema that both paths emit. If the agent path and the poll path produce different data shapes, every consumer downstream pays for it forever.

---

## 2. Revised architecture

```
Tier A devices ──[agent]──┐
                          │
Tier B devices ──[Redfish/IPMI poll]──┤
                          ├──> collector/gateway tier ──> Kafka ──> processing
Tier C devices ──[Modbus/BACnet/SNMP poll]──┘         (per site/row)
                          │
                          └──< command path (see §4)
```

The gateway tier is the load-bearing addition. Justification in §3.

---

## 3. Do not have devices talk to Kafka directly

Kafka is the right backbone. It is the wrong device-facing protocol. Four reasons, roughly in order of how badly each will hurt:

**Network topology.** A Kafka client bootstraps, fetches metadata, then opens direct connections to the partition leader for every partition it writes to. That means **every device needs routable reachability to every broker.** Management networks are segmented per row or per rack, sit behind jump hosts, and are firewalled off from the data plane by deliberate policy. You will either punch holes through that segmentation — undoing a security control that exists for good reasons — or fight NAT and metadata-advertised-listener problems on every site. This alone is usually decisive.

**Connection fan-in.** Kafka is built for a modest number of high-throughput clients, not a fan-in of very many low-throughput ones. Tens of thousands of producers each emitting a trickle is an anti-pattern: you pay broker memory and file-descriptor cost per connection, and metadata refresh across the fleet becomes a synchronized load spike.

**Credentials.** Direct-to-Kafka means every device holds broker credentials. A single extracted mTLS key from a switch in a remote site is now write access to your telemetry backbone. Certificate rotation across a large fleet of embedded devices is its own ongoing project.

**Client weight.** `rust-rdkafka` wraps `librdkafka` (C) — capable, but a C dependency that complicates static musl cross-builds and costs binary size. `rskafka` is pure Rust and minimal, but deliberately omits consumer groups, which matters for §4.

### Recommended

Device → **NATS** (or MQTT) → gateway → Kafka.

NATS is the better fit than MQTT for this specific shape:
- **Outbound-only connections from devices** — solves the firewall and NAT problem outright, since devices dial out rather than being reachable.
- **Leaf nodes** map cleanly onto per-site/per-row segmentation; each site runs a leaf that aggregates locally and forwards upstream.
- Native **request/reply**, which the command path in §4 needs and Kafka handles awkwardly.
- Small pure-Rust client (`async-nats`).

MQTT (`rumqttc`) is a reasonable alternative if you expect to inherit existing IoT broker infrastructure. Avoid inventing a bespoke protocol.

---

## 4. The command path is the hard part

Telemetry is a solved shape. "Accept `restart` and `apply config` from a bus" is where this design either earns a security sign-off or dies in one.

### Why not Kafka for commands

A per-device consumer group means as many consumer groups as devices, which puts serious pressure on the group coordinator. The workarounds — a compacted topic keyed by device ID, or partition-per-device — are all fighting the tool. Kafka's delivery model is also a poor match: you want *at-most-once with explicit idempotency* for a reboot, and Kafka's natural mode is at-least-once.

Use NATS request/reply or MQTT QoS 1 with application-level dedup. Keep Kafka for the telemetry firehose and the audit log of what was commanded.

### Non-negotiables for the command envelope

1. **Sign every command.** The agent must verify a cryptographic signature over the command payload, against a key that is *not* the transport credential. Transport auth proves the message came off the bus. It does not prove the command was authorized. If bus write access equals data center control, one compromised gateway owns everything.
2. **Idempotency keys.** Every command carries a UUID. The agent keeps a bounded seen-set and refuses replays. A `restart` delivered twice must execute once.
3. **Expiry.** Commands carry a `not_valid_after`. A reboot instruction that surfaces from a queue backlog four hours later is an incident, not a fix.
4. **Per-device allow-list of action types, enforced locally.** The agent refuses actions outside its own compiled/provisioned policy — the authority to say no lives on the device, not in the control plane. Central-only authorization is one bug away from a fleet-wide event.
5. **Rate limit and blast-radius cap locally.** An agent that has restarted a service three times in ten minutes should refuse the fourth and escalate instead.
6. **Local kill switch.** A file, a GPIO, a signed disable command — something an operator can use to make an agent inert without physical access to the box.
7. **Audit every decision, including refusals**, to Kafka.

This directly addresses the buyer-trust finding in the research: operators told Uptime Institute they are comfortable with AI analyzing sensor data, and *not* comfortable with it making configuration changes or controlling equipment. The command path needs to be inspectable and gated enough to argue with that objection. Ship it with human approval as the default mode and autonomy as an opt-in per action class.

---

## 5. Rust specifics

Rust is a genuinely good call here — no GC pauses on a device with a real-time-ish job, small static binaries, and memory safety on a privileged agent deployed at scale.

### Targets

Build static musl binaries; do not link against the device's glibc. Switch NOS images run old and varied userlands.

```
x86_64-unknown-linux-musl      # SONiC/Cumulus/EOS on x86 switches, servers
aarch64-unknown-linux-musl     # BlueField DPU, ARM switches, OpenBMC
armv7-unknown-linux-musleabihf # older ARM switch CPUs, BMC-class
```

Check for MIPS targets before promising coverage of older switch platforms — several are still in production fleets and Rust's support there is tier 3.

### Crate choices

| Need | Pick | Why |
|---|---|---|
| TLS | `rustls` | Pure Rust. Avoids OpenSSL cross-compile misery entirely. Worth it for this reason alone. |
| Async runtime | `tokio` with `rt` (current-thread), not `rt-multi-thread` | On a 2-core BMC-class CPU, the multi-threaded scheduler is overhead you don't want. |
| Transport | `async-nats` or `rumqttc` | Both pure Rust, small. |
| Kafka (gateway only) | `rskafka` or `rust-rdkafka` | Gateway is a normal Linux box; C deps are fine there. |
| Serialization | `serde` + msgpack/CBOR | JSON on the wire at fleet scale is a bandwidth tax you'll regret. |

### Footprint budget

Set a hard binary size and RSS ceiling and enforce it in CI. On an OpenBMC-class target — dual Cortex-A7 at 1.2 GHz sharing roughly 1 GB with the BMC's actual job — your realistic budget is single-digit MB of RSS and low tens of MB of binary. Cisco Guest Shell and Arista extensions are similarly capped.

```toml
[profile.release]
opt-level = "z"
lto = true
codegen-units = 1
panic = "abort"   # see caveat in §6
strip = true
```

---

## 6. Operational requirements that will bite

**The agent must never take down the device.** This is the absolute constraint — a monitoring agent that causes an outage is worse than no agent. Cap CPU and memory via cgroups where the platform allows. `panic = "abort"` shrinks the binary, but pair it with a supervisor that restarts with backoff; an agent in a tight crash-loop on a switch CPU is itself an outage.

**Flash wear is real.** Switches and embedded controllers boot from modest eMMC or NOR flash with finite write cycles. Buffer in a bounded in-memory ring first, spill to disk only under sustained backpressure, and rate-limit spill writes. Do not write logs to flash by default.

**Buffering and backpressure.** Devices will be partitioned from the gateway. Bounded ring buffer, drop-oldest for telemetry, never drop audit records. Decide the policy explicitly.

**Clock skew.** Embedded devices have unreliable clocks and may have never synced NTP. Timestamp at the gateway on ingest as well as at the device, and carry both.

**Upgrades are the hardest unsolved problem here.** Upgrading agents across a large fleet is a rollout system, not a feature: staged waves, health gates between waves, automatic rollback, and A/B slots where the platform supports them. Self-update is both a security surface and a bricking risk — an agent that can replace its own binary is an agent that can be made to install anything.

---

## 7. Gates that are not technical

- **Support contracts.** Arista and Cisco sanction app hosting; installing unsanctioned code on other platforms can void support. Get this in writing per vendor before committing to a target list.
- **Security review.** A privileged agent on network gear that accepts remote execution commands will get the hardest review of anything you ship. Signed commands, local policy enforcement, reproducible builds, and an SBOM are table stakes for passing it.
- **Change control.** In regulated environments, "agent applies a config change" may itself require a change ticket — which reintroduces the exact hop you set out to remove. Worth confirming early with a design partner.

---

## 8. Honest note on the thesis

As specified — ship telemetry to Kafka, wait for instructions — this agent is a **transport layer**, not distributed intelligence. Telegraf plus Salt/Ansible occupies roughly this space already.

More importantly, it doesn't collapse the 10-hop chain; it replaces the transport in the middle of it. If the device emits data and waits to be told what to do, every reasoning hop still happens centrally, and the chain is the same length with a faster bus. The compression only materializes when some decision is made *locally* — even something modest, like the agent correlating a thermal reading with a fan-speed trend and a workload signal, and either self-clearing the alert or escalating with a diagnosis attached rather than a raw symptom.

That is also the answer to the BMC footprint question from the research: Meta's split is the right precedent. `MachineChecker` runs on each server for **detection**; FBAR and Cyborg hold the **remediation logic** centrally. Local detection plus local first-response, central reasoning and learning — that fits both the hardware budget and the security posture, and it is still a real architecture rather than a dashboard with extra steps.

Suggested framing for the agent's job, in priority order:

1. Collect and normalize (the unglamorous moat — vendors implement Redfish inconsistently).
2. Detect locally, so a fault produces a *diagnosis* rather than a *symptom*.
3. Execute gated, signed, idempotent actions from a small allow-list.
4. Report everything, including refusals.

---

## 9. Recommended first slice

Prove the thing that's actually uncertain, not the thing that's easy.

1. **SONiC and BlueField first.** Both are open, both are strategically interesting, neither has a vendor gatekeeper. Skip closed switch platforms in v1.
2. **A Redfish poller for Tier B in the same release**, emitting the identical normalized schema. This is where the multi-vendor normalization moat gets built, and it covers the server fleet that agents can't reach.
3. **Telemetry path only.** No command path in v1.
4. **Command path in v2, human-approved by default**, with signing and local policy from the first commit — not retrofitted.

The open question worth burning early cycles on is not "can Rust push to Kafka." It's whether local detection can produce a diagnosis good enough that a human approves the proposed fix without re-deriving it. Build the smallest thing that tests that.
