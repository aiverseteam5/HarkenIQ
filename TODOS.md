# HarkenIQ — Open Items

> **2026-08-19: superseded as release plan by [docs/requirements/00-platform-spec.md](docs/requirements/00-platform-spec.md).**
> R1 shipped as Diagnostic Foundation; the mesh design feeds slice R3. Open items M1–M10
> are carried into spec §8 with owning slices. This file is retained as historical record.

Items with a decision behind them. Vague intentions are lies; if it is not here, it does not exist.

**Release one is Harken Mesh.** Reversed 2026-07-27, superseding the credential-first plan produced earlier the same day. Design: [docs/design/harken-mesh-release-one.md](docs/design/harken-mesh-release-one.md).

Sources: `/office-hours`, `/plan-ceo-review`, and the mesh scoping that followed.

---

## Release one — Harken Mesh

Tiered model: capability degrades with topology density. Autonomy gates on tier — only a corroborated tier-1 conclusion may act.

### P1 — Blocking. Resolve before code.

**M1. How is a fault injected at both T1 and T2 on real hardware without causing an outage?**
Success criterion 1 is the whole point of the release — showing that a node with peers produces a better answer than a node without. It requires inducing the same fault twice under different topology conditions, on hardware someone cares about. No approach currently exists. Same class of problem as testing destructive operations, and it has no obvious answer.

**M2. Define the release-one action allow-list.**
Autonomy was chosen, so this list is the blast radius. Smaller is better. Each action class needs its own precondition set, its own rate limit, and its own escalation rule. Start with the smallest set that demonstrates the loop closes.

**M3. Authorization lease duration versus partition detection time.**
R-MD25 fences a partitioned fragment by expiring its authority to act. If the lease outlives partition detection, both fragments act during a split. If it is too short, normal operation flaps between authorized and unauthorized.

**M4. Baseline confidence: computation and refusal threshold.**
A baseline learned while a device was already degrading encodes the degradation as normal. Needs an age, a sample count, a stability measure, and a threshold below which the node proposes rather than acts.

**M5. Node identity, key issuance, rotation and revocation.**
Claims are signed (R-MD12) and actions require signed authorization distinct from the transport credential (R-MD18). None of that works without an identity story, and revocation is the part usually left until it is needed urgently.

### P2 — Before release.

**M6. Peer set source on platforms without native topology discovery.** Where adjacency is not free, the Site Manager assigns it. Needs a model for how, and what happens when the assignment is wrong.

**M7. Node resource ceilings, enforced and observable.** R-MD4. A node that degrades its host is worse than no node.

**M8. Correlated-conclusion suppression.** R-MD24. Many devices concluding the same fault simultaneously usually means one shared upstream cause; acting on each independently makes it worse.

**M9. Coverage map presentation.** R-MD2 and R-MD3. A silent T3 device is unobserved, not healthy, and the interface must not let anyone read it the other way.

**M10. Does the two-device correlation probe fold into release one or stay for release two?** It is cheap once the peer substrate exists, but it proves a different claim than quorum and bundling them means neither is proven cleanly.

---

## Deferred — credential validation and rotation

**Was release one until the 2026-07-27 reversal. All findings below remain valid for whenever this work happens.** Full requirement detail in [doc 3](docs/requirements/03-credential-rotation.md).

### Would be P1 when this work resumes

| # | Item | Source |
|---|---|---|
| C1 | Device identity reconciliation before the rotation state machine. IP reuse, replaced controllers and stale inventory can bind the right secret to the wrong device | outside voice #7 |
| C2 | Durable operation journal plus restart reconciliation. Store and controller cannot share a transaction; instruction IDs do not make a password change idempotent | outside voice #11 |
| C3 | Break-glass provisioning is unsolved, and R-CR12 refuses rotation without it. **Blue-green account rotation — create, verify, then disable the old — is fundamentally safer than mutating the only primary account** | outside voice #12 |
| C4 | Per-cohort first-device gates replace the fleet-wide abort threshold. R-CR21 is wrong as written: a rare firmware cohort is fully stranded before the aggregate rate moves | outside voice #14 |
| C5 | Confirm the credential store supports a pending state. R-CR27 — without it, rotation is impossible and only validation is available | doc 3 |
| C6 | Factory-default probe guardrails: cohorts, rate limits, exclusions, customer-owned response procedure. Generations with unique per-chassis passwords have no shared default to test | outside voice #9, #10 |

### Would be P2

C7 bound the support matrix (#6) · C8 unknown password policy means validation-only, replacing R-CR16 (#8) · C9 split credential and hardware schemas behind a shared identity envelope (#16) · C10 threat model for a tier-zero credential gateway; R-CR35 "cleared from memory" is not achievable and must be restated (#15) · C11 specify the audit claim (#17) · C12 resolve Central Command's role (#3) · C13 runbook for `UNKNOWN`.

### Strategic

C14 validate that demand supports the scope — two anecdotes, and market sizing was inherited from the diagnosis thesis (#18) · C15 test whether normalization compounds or is pure maintenance burden, given sovereign customers may prohibit data pooling (#19) · **C16 evaluate the PAM target-connector shape as an alternative product (#20)** — worth a serious hour.

---

## Deferred — other

| Item | Why worth doing | Why deferred |
|---|---|---|
| Firmware inventory | CVE exposure across vendors in seconds | Version-string normalization is real work |
| Never-logged-into detection | Every estate has forgotten devices | Last-login exposure varies by vendor |
| Warranty and lifecycle | Opens a procurement budget line | Needs external vendor lookups |
| Shareable redacted artifact | Tools spread when the buyer looks good forwarding them | Report already specified; delta is presentation |
| Grey failure / threshold inference | The hardest and highest-value fault class | Needs the peer substrate first; borrow published inference approaches rather than inventing |

---

## Accepted risks

**AR1 — Release one serves neither demand source. Severity: HIGH.**
Both known sources point at servers and credentials. The mesh is a bet on the architecture, made deliberately with the cost understood.

**AR2 — Autonomous action in release one. Severity: HIGH.**
Uptime Institute 2025 (n=800+): operators accept AI analysing sensors and reject it controlling equipment. Chosen anyway. Mitigated structurally by autonomy gating on tier (R-MD15) rather than by policy — the system can only act where it can prove what is wrong.

**AR3 — Simulator-only testing.** Carried from the credential plan and still unresolved for the mesh. See M1.

---

## Unanswered

- Is the twenty-year source employed **at** Dell, or supporting Dell equipment at an operator?
- Is the "hyperscaler vendor" a hyperscaler, or a vendor selling into hyperscalers?
- **No 12-month target exists.** Flagged three times now.
- The 10-hop triage chain remains unsourced. Keep it out of external material until primary interviews are done.

## Document amendments

- **Doc 2 §7.1** — amended for the earlier servers-first reversal. **Now stale again** — the mesh needs topology adjacency, which pushes back toward switches. Needs a third pass.
- **Doc 1 §1 / doc 2 §4** — only if the "ground truth is the moat" reframing is adopted. Proposed, not adopted, and less relevant now that the mesh is release one.
- **The CEO plan** describes a credential-first release one and is superseded.

## Housekeeping

- `docs/design/`, `docs/requirements/`, `docs/research/` untracked. Everything from this session is uncommitted.
- gstack 1.60.1.0 available; running 1.58.5.0.

---

## Post-R6

**N1. Server-side NIC interface collection — two-sided switch↔server correlation.**
*(added 2026-08-25, from R6 /plan-eng-review outside-voice T6)*
- **What:** servers populate the R6 `interfaces` collection too — NIC error
  counters via OS signals (ethtool/sysfs) and/or Redfish NetworkAdapters —
  so the two-device correlation probe works on switch↔server links.
- **Why:** R6 v1 is probe-blind on switch↔server links (the majority of real
  links) because only switches carry interface data. Two-sided evidence on
  those links is the doc 02 G2 wedge (path inference + on-box physical
  evidence) applied to the most common link type.
- **Pros:** closes the biggest correlation blind spot; reuses the R6
  NormalizedInterface model and probe both-sides logic unchanged.
- **Cons:** touches OS-signal collectors and the server agent path; its own
  testing surface; belongs after R6 proves the port model.
- **Context:** NormalizedInterface (R6-P1, `protocols/model.py`) is device-
  class-agnostic by design; CorrelationProbe (R3b-2) already handles
  both-sides evidence. Start at the OS-signal layer (R3b device mapper).
- **Depends on:** R6-P1 shipped.


---

## 2026-08-28 review follow-ups (tenant-plane separation, PRs #9–#11)

Recorded per the review close-out (decided: Vinod). Fixes for everything
objectively broken shipped on the stack; these are the undecided or
deferred remainders. Open questions with architectural weight live in spec
§8 (OQ-23..OQ-26); these are the work items.

- [ ] **Revoke UI for active support grants** — approval queue shows only
  pending requests; cutting a live 24h grant short is curl-only today.
  Surface active grants + Revoke on the Support Access page. (api-contract
  pass; owner: next Console slice)
- [ ] **New-tenant placement onboarding** — a created tenant's
  infrastructure pages 503 (correctly, fail-closed) until a placement is
  registered; there is no in-product prompt. Wire a "register Central
  Command" CTA or a placement step into tenant creation. (adversarial
  pass; owner: next Console slice)
- [ ] **Rolling-deploy note for support-access cutover** — an old replica's
  grants created between migration 0003 and code cutover land as
  `requested` and die silently at cutover (fails safe). Single-replica
  Console today; document in the deploy runbook if replicas ever ship.
  (data-migration pass)
- [ ] **Proxy streaming** — the CC proxy buffers request+response bodies in
  full (pre-existing); switch to streaming if large exports/firmware
  payloads ever transit it. (performance pass)
- [ ] **Compose-gate tenant-user path** — the gate's scenario runs on a
  super-admin token whose break-glass bypasses membership + support-access
  gates; those paths are unit-pinned but not e2e. Needs a seeded
  tenant-realm user with a password in the demo Keycloak. (red team)
- [ ] **SPA realm discovery** — the login flow bakes one realm
  (`VITE_KEYCLOAK_REALM`); a real multi-tenant deployment needs tenant-slug →
  realm resolution at the login page. (Recorded with A12; owner: next Console
  slice)
- [ ] **A13 read-gate follow-ups (auditor scope, OQ-24):** (a) role-bundle
  listing readable to user.view holders; (b) CC approvals-history read gate
  (today action.approve-only — R-C3 evidence unreadable by the auditor);
  (c) policy read path (Console + CC both require site.manage; no read-only
  governance review exists for non-admins). All read-only. (owner: next
  Console slice)
