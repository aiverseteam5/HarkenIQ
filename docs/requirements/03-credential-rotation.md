# Credential Validation and Rotation — Requirements

**Document 3 of 3**
**Date:** 2026-07-27
**Status:** Draft for review
**Scope:** Requirements for HarkenIQ to validate and rotate static credentials on physical infrastructure, integrating with external credential stores.
**Release:** **R2.** Credential rotation is deferred to R2 per engineering decision (2026-08-01). This document is the complete spec for R2 implementation. R1 uses local encrypted config (AES-256-GCM) for BMC credentials with no rotation capability. See Doc 06 §3.3 for R1 credential model.

**Deliberately out of scope:** technology stack.

**Revision note (rev 2):** §6.3 two-party attestation added. Requirement identifiers are stable, so R-CR39–R-CR47 appear in §6.2–§6.3 rather than at the end. §6.4–§6.7 renumbered; a `DISPUTED` outcome state was added.
**Revision note (rev 3, 2026-08-18):** Added release clarification header. This is an R2 spec; R1 does not implement credential rotation.

---

## 1. Positioning

**This is a capability of the platform, not a second product.**

Security mandates credential rotation. **Network operations has to execute it** — by hand, or not at all. The platform already holds credentialed access to every device in order to do its primary job; accepting a rotation instruction, applying it, verifying it and reporting back is a natural extension of that access, not a new system.

The buyer does not change. Network operations decides whether to enable the capability. If they do, the platform declares which credential stores and which device classes it supports. What changes is that operations can hand security a solved problem using a tool they already own — which makes the platform easier to justify, not harder.

### 1.1 The important reframe

**The product is not automation. It is making rotation safe.**

Rotating a credential is trivial. The reason static passwords persist across the industry is not ignorance and not a lack of scripting — it is that **a failed rotation on an out-of-band management interface permanently removes remote access to that device.** No console, no remote power control, no recovery without physically visiting the rack. Do that across a fleet in a single bad run and it is a company-ending incident, for the customer and for the vendor who caused it.

Every audit tells operators to rotate. They do not, because the downside of a bad rotation is worse than the risk they are currently carrying.

Whoever removes that fear wins this. The automation is the easy part.

### 1.2 Why the architecture helps

A node positioned at or beside the device can verify a new credential from close range and hold the old one until verification succeeds. That is a genuine reason for the platform's shape to exist here, not a retrofit — and it is precisely the mechanism that converts rotation from frightening to routine.

---

## 2. Problem and evidence

### 2.1 The exposure

| Finding | Source |
|---|---|
| Factory default credentials remain active on large numbers of deployed servers; well-known defaults for major server vendors are commonly left in place and sometimes exposed to the public internet | [ServeTheHome](https://www.servethehome.com/idracula-vulnerability-impacts-millions-of-legacy-dell-emc-servers/3/), [ColorTokens](https://colortokens.com/blogs/ipmi-security-risks-server-management-microsegmentation/) |
| "A startling number of [management] interfaces [are] directly addressable via a public IP," often with default logins | ServeTheHome |
| **JungleSec ransomware (2018)** targeted exposed management interfaces, entered via default passwords, used the remote console feature to reach the host operating system, encrypted data, and pivoted internally | ColorTokens |
| A compromised management controller enables hard power-off of a hypervisor host, or permanent denial of service by flashing malicious firmware | ColorTokens |
| **First management-controller vulnerability added to the CISA Known Exploited Vulnerabilities catalog, June 2025** | [Eclypsium](https://eclypsium.com/blog/bmc-vulnerability-cve-2024-05485-cisa-known-exploited-vulnerabilities/) |
| Root-of-trust bypasses allowing malicious firmware that **survives reboots and full OS reinstalls** | [The Hacker News](https://thehackernews.com/2025/09/two-new-supermicro-bmc-bugs-allow.html), [Binarly](https://www.binarly.io/blog/ghost-in-the-controller-abusing-supermicro-bmc-firmware-verification), [vendor advisory](https://www.supermicro.com/en/support/security_BMC_IPMI_Sept_2025) |

Management firmware "executes outside the scope of operating system controls and has access to all resources of the platform on which it resides." The exposure is severe and the regulatory attention is increasing.

### 2.2 What already exists

**A major server vendor's management platform ships native integration with a leading privileged access management product for rotating management-controller passwords.** ([vendor KB](https://www.dell.com/support/kbdoc/en-us/000219279/openmanage-enterprise-4-0-idrac-password-management-and-rotation), [user guide](https://www.dell.com/support/manuals/en-us/dell-openmanage-enterprise/ome_p_40_users_guide/enable-cyberark-integration-for-password-management))

This is the same pattern documented throughout [document 2](02-market-and-build-plan.md): the vendor solves it for its own equipment. It is the fourth independent instance of that pattern in this research.

The published limitations define the opening:

| Limitation | Consequence |
|---|---|
| Local accounts only; directory-backed accounts unsupported | Partial coverage even within that vendor |
| Single credential-store container supported | Does not fit segmented credential organization |
| One authentication mechanism only | Inflexible |
| **The vendor manages its own equipment** | Nothing spans a mixed estate |

Leading secrets-management platforms are oriented around dynamic, short-lived credentials for applications, databases and cloud services. Static credential rotation against physical devices is not their native territory — which makes them an **integration target rather than a competitor.**

**The gap: nobody rotates credentials across a mixed fleet of servers, switches, power distribution and storage from one place.** The same structural gap as the diagnosis use case, from the same cause.

---

## 3. Scope

### 3.1 In scope

- **Continuous credential validation** — confirming that recorded credentials actually work on the devices they claim to (§5).
- **Credential rotation** — generating, applying, verifying and recording new credentials (§6).
- **Integration with external credential stores** as the system of record.
- **Coverage across device classes**, including devices that cannot host a node.
- **Reporting and audit** sufficient for a compliance finding.

### 3.2 Out of scope

- Becoming the system of record for credentials. An external store always holds them.
- Human identity, single sign-on, session brokering, or session recording. That is the privileged access management market and the platform does not enter it.
- Directory-backed account lifecycle.
- Certificate lifecycle management (candidate for a later document).
- Application, database or cloud credentials.

### 3.3 Dependency

This capability requires the platform's write path. Per [document 1](01-architecture.md) §6.3, the platform must be deployable read-only, with write capability separately enabled, authorized and audited. **Validation (§5) requires no write capability at all** — which is why it leads.

---

## 4. Functional requirements — common

**R-CR1.** Every credential operation is executed under the platform's signed instruction framework: unique identifier for replay rejection, expiry, explicit action class, and verifiable authorization ([document 1](01-architecture.md) §6.2). Credential operations introduce no new command mechanism.

**R-CR2.** Credential operations are a separately enabled action class, independently authorized and independently revocable, disabled by default.

**R-CR3.** Every credential operation — including refusals, timeouts and indeterminate outcomes — is recorded in the audit trail with sufficient detail to satisfy an auditor.

**R-CR4.** Credential material is never written to logs, diagnostics, error messages, or persistent storage on any node. It is held in memory only for the duration of the operation and cleared immediately afterward.

**R-CR5.** The platform must never generate, hold or transmit credential material for any device outside the customer's declared scope.

---

## 5. Credential validation

**This is the recommended first capability.** It is read-only, carries no blast radius, requires no write authorization, and produces an immediately actionable compliance finding.

**R-CV1.** The platform continuously verifies that the credential recorded in the external store actually authenticates against the device it claims to.

**R-CV2.** The platform detects and reports, per device:
- credential recorded and working,
- **credential recorded but not working** (drift — the store and reality disagree),
- **device accepts known factory-default credentials**,
- **no credential recorded at all** for a discovered device,
- device unreachable, so status is indeterminate,
- **credential works but so does a previously-retired credential** — evidence of a past rotation that added rather than replaced.

**R-CV3.** Validation is non-disruptive. It must not trigger account lockout, generate authentication-failure alarms that pollute the customer's security monitoring, or consume management-interface session capacity that operations depends on. Validation frequency must be configurable and conservative by default.

**R-CV4.** Validation output is a fleet-level report suitable for presenting to an auditor:

> *4,200 devices discovered. 3,752 credentials validated. 312 recorded credentials do not work. 89 accept factory defaults. 47 have no recorded credential.*

**Nobody knows these numbers today, and every line of that report is an audit finding.** It is also the same deployment footprint the diagnosis capability requires, which makes it a plausible route in the door.

**R-CV5.** Validation must be available without rotation ever being enabled. Some customers will only ever want to know.

---

## 6. Credential rotation

### 6.1 The sequence

The order is the requirement. A sequence that applies before recording can lose a device permanently.

**R-CR6.** Rotation follows this order, without exception:

| Step | Action | Invariant |
|---|---|---|
| 1 | Central generates the new credential and records it as **pending** | Store now holds **both** old and new |
| 2 | **Witness assigned**; witness records pre-rotation device state (§6.3) | Independent observer in place before anything changes |
| 3 | Signed instruction issued to the executing component | Authorization verified at execution |
| 4 | New credential **applied** to the device | Old still recorded |
| 5 | Executing component **self-verifies** — both tests, §6.2 | Nothing discarded yet |
| 6 | **Independent verification** from outside the device (§6.3) | Performed by a different component than applied it |
| 7 | Witness confirms device liveness across the rotation window (§6.3) | Witness holds no credential |
| 8 | Both attestations **sealed** and recorded (§6.3) | Two independent signatures in the audit trail |
| 9 | Outcome reported | |
| 10 | New marked **active**; old retired after a grace period | |

**R-CR7.** At no point may the only record of a working credential exist solely on the device or solely in transit. If any step fails, both credentials remain recorded and the device remains reachable.

### 6.2 Verification — two tests

**R-CR8 — Test 1: the new credential works.** Verification must **not** be based on the success response of the change operation. Devices apply changes asynchronously, apply them to a different account than requested, and silently truncate values exceeding undocumented limits.

Verification requires: **tear down the existing authenticated session entirely, establish a completely new authentication using the new credential, and perform a real read.**

**R-CR9 — Test 2: the old credential no longer works.** Without this test, *rotated* and *added a second credential* are indistinguishable. That failure presents as success and leaves the old credential live indefinitely — a silent security hole that an audit will eventually surface, on a device the platform reported as compliant.

**R-CR10.** A rotation is successful only when both tests pass. Test 1 passing and Test 2 failing is a **distinct partial-failure state** requiring human attention, not a success.

**R-CR39.** Both tests are performed twice: once by the component that applied the change, and once independently per §6.3. The independent result is authoritative.

### 6.3 Two-party attestation

#### Rationale

A component verifying its own work is producing a self-report, and [document 1](01-architecture.md) principle P4 holds that a self-report is evidence rather than a verdict. The applying component may hold a cached session, a stale connection, or a defect in the very code path being tested. Rotation therefore requires **two independent parties** before an outcome is accepted.

#### Why the witness cannot be the credential verifier

Proving a credential works requires authenticating with it. There is no mechanism in standard device management interfaces to test a credential without presenting it.

A peer node acting as credential verifier would therefore have to **hold the credential for a device it does not own**, placing that credential on a second device with no legitimate need for it and doubling the exposure surface of every credential in the fleet. This directly violates R-CR33 and [document 1](01-architecture.md) R-X12.

The two jobs are therefore split across two different components.

#### Role split

| Role | Performed by | Holds credential? | Verifies |
|---|---|---|---|
| **Applier** | The component nearest the device — node where one exists, Site Manager otherwise | Yes, transiently | Self-check, both tests of §6.2 |
| **Independent verifier** | **Site Manager** | Yes — it already does, for the polling path | Both tests of §6.2, from outside the device. Authoritative |
| **Witness** | **A peer node** | **No** | Device liveness across the rotation window |

**R-CR40 — Independent verification is performed by the Site Manager, not by a peer node.** It is already outside the device, already trusted with device credentials, already the single site egress point (R-S8), and — critically — it is a **different component from the one that applied the change**, which is where the independence actually comes from: different process, different session, different code path, no new credential exposure.

There is a second reason this is the correct verifier. The failure being guarded against is loss of **remote** access. A node running on a device's own control plane sits inside that device and can confirm a credential locally while remote authentication is already broken. Only a component outside the device tests the property that matters.

**R-CR41 — The witness is a peer node holding no credential.** Its role requires none, and it is genuinely valuable:
- it records the device's operational state immediately before the change,
- it confirms from outside that the device remains alive and forwarding after it,
- it **detects the device going dark during the rotation window**, which is the fastest possible trigger for the `UNKNOWN` state of R-CR14.

A witness reporting *"the device I watch went silent four seconds into its rotation"* is exactly the signal required, at zero credential exposure.

#### Witness assignment

**R-CR42.** The witness is assigned by the Site Manager or derived deterministically. **A node must not select its own witness** — a defective or compromised node would select a defective or colluding one.

**R-CR43.** The witness must not reside in the same physical fault domain as the subject device, and must not itself be undergoing rotation in the same wave.

**R-CR44.** Where no eligible witness exists, the rotation **does not proceed**. A rotation with no independent observer is the one most likely to be regretted. Operators may override per-run, and the override is recorded.

#### Seals

**R-CR45.** Each party emits a **seal**: a signed attestation, recorded in the audit trail, stating what it observed and when. A seal is not a status message; it is a durable, independently verifiable record.

A completed rotation therefore carries two independent signatures — one from the component that applied the change, one from the component that verified it — plus the witness's liveness attestation. This is a materially stronger compliance artifact than a success flag.

#### Disagreement

**R-CR46.** Where the applier and the independent verifier disagree, the outcome resolves to `DISPUTED` per §6.5, requiring human attention with both credentials retained.

**A disagreement is never resolved in favour of success.** The tempting default is the opposite and it must be foreclosed in the specification rather than left to implementation.

#### Relationship to break-glass

**R-CR47.** Two-party attestation reduces the probability that a bad rotation goes **undetected**. It does not restore access when one occurs. The break-glass requirements of §6.4 remain mandatory and are not satisfied, weakened, or substituted by this section.

### 6.4 Break-glass

**R-CR11.** Every device under management carries a second, independently managed account that is **never rotated in the same operation or the same run** as the primary. Different schedule, and where the device supports it, reached by a different mechanism.

**R-CR12.** The platform refuses to rotate a device's primary credential if that device has no verified working break-glass account.

**This single mechanism is what makes the capability acceptable to an operations team.** Without it, the platform is asking them to bet fleet access on its correctness.

### 6.5 Outcome states

**R-CR13.** Every rotation resolves to exactly one state:

| State | Meaning | Handling |
|---|---|---|
| `SUCCESS` | Both tests passed, **both seals present and in agreement** | Retire old after grace period |
| `FAILED_SAFE` | Change did not apply; old credential verified still working | Safe to retry |
| `PARTIAL` | New works, old also still works | Human attention. Both recorded |
| `DISPUTED` | Applier and independent verifier disagree (R-CR46) | **Human attention. Both credentials retained. Never resolved toward success** |
| `REJECTED` | Device refused the value — policy, length, character set | Regenerate under corrected policy |
| **`UNKNOWN`** | Device unreachable between apply and verify, or witness reports the device went dark in the window | **Human attention. Both credentials retained. Never silently retried** |

**R-CR14.** `UNKNOWN` is a first-class state, distinct from failure. When the device becomes unreachable between apply and verify, which credential is live is genuinely undetermined. Marking it failed and retrying is how a fleet is lost.

### 6.6 Device policy

**R-CR15.** The platform maintains a per-device-class credential policy — maximum and minimum length, permitted character set, prohibited sequences, reuse constraints — and generates within it.

Embedded device credential rules are inconsistent and frequently undocumented. Some management protocols cap credential length at a fixed small size. Devices vary in which characters they accept, and some silently truncate rather than rejecting. Without policy known **before** generation, the platform will fail continuously across the long tail of device types.

**R-CR16.** Where a device's policy is unknown, the platform uses the most restrictive known policy for its class and records that it did so.

**R-CR17.** Silent truncation must be detected. This is caught by R-CR8, which is why verification cannot be optional or inferred.

### 6.7 Concurrency and fleet operations

**R-CR18.** One rotation in flight per device, enforced by an exclusive lock. Two concurrent rotations on one device is a mechanism for losing it.

**R-CR19.** Fleet rotation runs staged: canary, then expanding waves, with health gates between waves.

**R-CR20.** Hard cap on concurrent rotations, independent of wave size.

**R-CR21.** **The entire run aborts automatically when the failure rate crosses a threshold.** Discovering at device 3,000 that a firmware revision behaves differently is unacceptable.

**R-CR22.** No wave may include all devices within a single physical fault domain, and no wave may include both a device's primary and break-glass credentials.

**R-CR23.** An operator-accessible immediate stop for any in-flight run.

---

## 7. Credential store integration

### 7.1 Initiation model

**R-CR24.** The platform supports both initiation models, and **the store-driven model is the default**:

- **(a) Store-driven** — the privileged access management system schedules rotation and instructs the platform to execute. The security team retains governance and the store remains the system of record.
- **(b) Platform-driven** — the platform schedules, executes and writes back.

Model (a) is far more likely to pass a security review, fits how these programmes are already governed, and avoids asking to become the system of record for credentials on day one. Note that at least one major platform's own model expects its rotation manager to drive the operation, so registering as a target within that framework may be a cleaner fit than driving it externally. Model (b) exists for customers with no such programme.

### 7.2 Store requirements

**R-CR25.** Supported stores at minimum: the two named in the original requirement, plus the other leading enterprise privileged access platforms and the major cloud secret services. The declared support list is customer-facing.

**R-CR26.** The platform supports **multiple credential containers**, not one. Single-container support is a documented limitation of the existing vendor-native integration and a differentiator to claim.

**R-CR27.** The platform must record both pending and active credential state, as required by R-CR6. A store integration that cannot represent a pending credential cannot be used for rotation — only for validation.

**R-CR28.** Where a store supports multiple authentication mechanisms, the platform supports more than one. Certificate-only support is a documented limitation of the existing integration.

**R-CR29.** Loss of connectivity to the store aborts new rotations cleanly. In-flight rotations complete their verification and report; they do not proceed to retire an old credential without a confirmed store write.

---

## 8. Device class coverage

**R-CR30.** Coverage spans, at minimum:

| Class | Reached via |
|---|---|
| Server management controllers | Standardized management API; legacy management protocol; vendor-specific utility |
| Open network operating systems | Node-local, or the platform's own management interface |
| Proprietary network operating systems | Vendor programmatic interface |
| Power distribution | Vendor API where present; management protocol otherwise |
| Storage | Vendor API |

**R-CR31.** Where a device class can only be reached by a mechanism the platform cannot safely automate, that class is declared **unsupported for rotation** and remains supported for **validation**. Silent partial coverage is prohibited — a device the platform cannot rotate must never appear as compliant.

**R-CR32.** The device class list is the harder and more valuable of the two support lists, and it is the list on which customers will evaluate the capability. It is the same cross-vendor normalization work as the diagnosis use case.

---

## 9. Security requirements

**R-CR33 — A node never holds credentials to the credential store.** It receives credential material over the platform's signed instruction channel and nothing more. A node able to authenticate to the credential store turns every device into a path to it — and is the single finding a security reviewer will most want to find. This restates [document 1](01-architecture.md) R-X12 in the context where it matters most.

**R-CR34.** Credential material in transit is protected end to end between the authorizing component and the executing component, and is not readable by intermediate components that do not need it.

**R-CR35.** Credential material is cleared from memory immediately after use and is never persisted, logged, or included in diagnostics or crash output.

**R-CR36.** Rotation authority is scoped: which device classes, which sites, which accounts, initiated by whom. Scope is enforced at the point of execution, not only at the point of request.

**R-CR37.** The audit trail records who or what initiated each rotation, the authorization presented, the outcome, and the verification result — but never the credential.

**R-CR38.** The capability is subject to independent security assessment before general availability. A component with write access to credentials on every device in a customer's estate will attract the hardest review of anything the platform ships.

---

## 10. Sequencing recommendation

| Stage | Capability | Write access | Risk |
|---|---|---|---|
| **1** | Validation and drift reporting (§5) | **None** | None |
| **2** | Rotation, single device, operator-initiated | Yes | Contained |
| **3** | Rotation, staged fleet runs (§6.7) | Yes | Managed |
| **4** | Store-driven scheduled rotation (§7.1a) | Yes | Governed externally |

**Lead with validation.** It is safe, immediately valuable, produces findings nobody currently has, requires no write authorization, and establishes deployment and trust against every device in the estate — which is the same footprint the diagnosis capability needs.

Earn rotation rights afterward.

---

## 11. Open questions

| # | Question | Blocks |
|---|---|---|
| Q1 | Where no node exists, the Site Manager is both applier and independent verifier. Does that collapse the independence guarantee of §6.3, and what compensates? | §6.3, §8 coverage |
| Q8 | Can a peer witness meaningfully observe a device on a segmented management network it has no route to? If not, witness eligibility is topology-bound and R-CR44 fires more often than expected | R-CR41, R-CR44 |
| Q9 | Should the witness role be held by more than one peer for high-value devices, and does that change the disagreement rule? | R-CR41, R-CR46 |
| Q10 | What is the throughput cost of witness assignment and sealing at fleet scale, given the concurrency caps in §6.7? | R-CR20, R-CR42 |
| Q2 | What is the grace period before an old credential is retired, and is it customer-configurable? | R-CR6 step 6 |
| Q3 | How does the platform establish the break-glass account initially, on a device where it does not yet exist? | R-CR12 |
| Q4 | Does registering as a target within an existing rotation framework fit better than driving rotation externally? Needs a technical conversation with the vendor | R-CR24 |
| Q5 | How is per-device-class credential policy sourced — documentation, empirical discovery, or customer declaration? | R-CR15 |
| Q6 | What is the recovery procedure when a device reaches `UNKNOWN` and neither credential works? | R-CR14 |
| Q7 | Does any customer require rotation on a device the platform cannot verify afterward, and is that ever acceptable? | R-CR10 |

---

## Related documents

- [01 — Platform Architecture](01-architecture.md)
- [02 — Market, Comparative Analysis and Build Plan](02-market-and-build-plan.md)
- [Research: premise evidence](../research/premise-evidence.md)
