# R6-P0 Reality Spike Report

Date: 2026-08-25. All evidence gathered live on this host (docker 29.6.1);
artifacts from the official sonic-net master pipeline
(`sonic-build.azurewebsites.net`, build 1201273). Raw captures in the session
scratchpad; the durable facts are recorded here. Per A3.1: nothing below is a
hardware dependency — every finding feeds the simulator fixture and the
deployment docs.

## (a) Resource budget — measured

| Measurement | Value |
|---|---|
| Agent container steady-state (Redfish poll vs mock, 5 skills, 10s interval) | **43.3 MiB RSS, ~0% CPU** |
| Same agent under an ENFORCED 50MiB cgroup cap | stable, no OOM, full state-machine cycles |
| Bare-import peak on dev host (incl. httpx, not an agent dep) | 59.7 MiB ru_maxrss |
| Agent image size (python:3.12-slim base) | **317 MB** |

Reading: the constrained profile (30 target / 40 soft / **50 hard**) is NOT
credible for the network agent — at idle the agent already sits in the
soft-throttle zone (43.3 > 40) with ~6.7 MiB headroom before the gNMI
subscribe cache, per-port rate state, and 64-port baselines exist.
The standard profile (50/75/**100**) has comfortable headroom. Switch control
planes carry GBs of RAM; A2.5's constrained profile was designed for GPU
servers where the agent shares a customer workload host.

**Decision (Vinod, P0 gate): network devices run the `standard` profile**
(existing profile, no A2.5 change; N0 container limit = 100MB hard).
The 317MB image is fine for SONiC app hosting (switches have GB-class flash);
image slimming is an optimization item, not a gate.

## (b) gNMI ground truth on real SONiC — the P2 fixture facts

1. **Topology:** `docker-sonic-vs` is the dataplane only (swss/syncd/FRR) —
   it has NO gNMI service. The gNMI server ships in `docker-sonic-gnmi`
   (service `gnmi-native`, binary `/usr/sbin/telemetry`), which must share the
   dataplane's **network namespace AND `/var/run/redis`** (unix socket). The
   R6 compose profile therefore runs sonic as a two-container pod with a
   shared redis volume.
2. **Server flags that matter:** `-port`, `-bind_address`,
   `-client_auth {none,cert,password}` (write RPCs are `Unauthenticated`
   without auth or `none`), `-gnmi_native_write` (default true),
   **`-gnmi_translib_write` (default FALSE — writes return
   `Unimplemented: Translib write is disabled` out of the box)**. TLS is
   effectively mandatory (`-noTLS` refuses non-loopback binds; the stock
   client tools are TLS-only).
3. **Capabilities (captured):** 11 models — `openconfig-interfaces`,
   `openconfig-platform`, `openconfig-lldp`, `openconfig-system` (1.0.2),
   `openconfig-acl`, `openconfig-mclag`, `openconfig-sampling-sflow`,
   `ietf-yang-library`, `sonic-db` (0.1.0). Encoding: JSON_IETF.
4. **Reads — two working surfaces:**
   - Native: target `COUNTERS_DB`, counters keyed by OID via
     `COUNTERS_PORT_NAME_MAP` (never by port name directly); **41 SAI
     counters/port** on vs (IF_IN_ERRORS, IF_IN/OUT_DISCARDS, ether stats…).
   - OpenConfig via translib:
     `/openconfig-interfaces:interfaces/interface[name=X]/state/counters`
     returns **14 normalized counters** (in-errors, in-discards, octets,
     pkts…). `state/oper-status` was NotFound in the bare harness (needs
     STATE_DB population a full system provides) — GNMIProtocol must tolerate
     per-path NotFound.
5. **Subscribe:** `SAMPLE` mode works (verified at 2s; 6 updates/12s).
   `TARGET_DEFINED` returns `InvalidArgument: unsupported subscription mode`.
   Counter freshness floor = the flexcounter interval (default **1s**), and
   **flexcounters are OFF until `counterpoll port enable`** — sampling faster
   than 1s buys nothing; the deployment doc must enable counterpoll.
6. **No CRC/FEC/optics counters on vs** (no ASIC) — confirms the §5
   fidelity gate exactly as recorded (partner-site demonstration).
7. **Writes (the decisive capture):** with translib write enabled + auth
   cleared, an OpenConfig Set
   (`.../config/enabled` with JSON_IETF payload) is **accepted
   (SetResponse op:UPDATE) but did not persist** to CONFIG_DB in the
   standalone two-container harness; raw DB-path writes are rejected
   (`Node PORT not found`) once translib owns the CONFIG_DB target. A full
   SONiC system runs the mgmt stack that persists translib writes; that
   persistence is exactly the §5 gNMI-Set gate (partner site).

## (c) NETCONF go/no-go — the evidence

- NETCONF does not exist in community SONiC at all (established at D7;
  enterprise distros only).
- gNMI Set is the ONLY write path that exists on the anchor, and (b)7 shows
  even it is disabled-by-default, auth-gated, and unverified-persistence in
  the dev harness.
- Building NETCONFProtocol in R6 would therefore be a second write path with
  strictly LESS reality behind it than gNMI Set — testable only against a
  NETCONF server we would write ourselves.

**Decision (Vinod, P0 gate): P5 (NETCONFProtocol) is DROPPED from R6** and
defers wholesale to the §5 real-device gate (first design partner or
enterprise-SONiC engagement brings a real NETCONF endpoint; the
CompositeProtocol seam from decision 8 is where it lands without redesign).
R6 action transport = gNMI Set, mirrored faithfully by the P2 simulator
INCLUDING the write-disabled default, auth behavior, and accepted-vs-persisted
distinction, so the agent's verification step (read-back after Set) is what
proves an action landed — never the SetResponse alone.

## Incidental fixes landed during the spike

- `deploy/full-stack/Dockerfile.simulator` CMD was a Python SyntaxError since
  R4-0 — the mock container never started. Replaced with the `harken mock
  start` CLI entry point.
- `harken mock start` gained `--host` (MockSimulator supported a bind address;
  the CLI never exposed it — required for any container use).
- `deploy/full-stack/Dockerfile.agent` now exists (the compose file referenced
  it; it was never written). Includes built-in skills at the config-default
  path. This image is the P8/N0 starting point.

## Consequences for the phase plan

- P1–P4, P6–P8 proceed unchanged; **P5 removed** (plan §7 renumbering not
  needed — P5 slot simply closes; its §5 gate row already exists).
- P2 simulator fixture = §(b) above, verbatim: OID-keyed counters + name map,
  SAMPLE-only subscribe, JSON_IETF, translib-write-disabled default, auth
  refusals, per-path NotFound.
- P3 GNMIProtocol: prefer OpenConfig `state/counters` for normalized reads
  with native COUNTERS_DB as the fallback surface; tolerate NotFound
  per-path; subscribe SAMPLE at ≥1s.
- P6 actions: Set + mandatory read-back verification; deployment docs carry
  the `-gnmi_translib_write` + auth + counterpoll requirements.
- P8/N0: standard profile (100MB hard) for switch deployments.
