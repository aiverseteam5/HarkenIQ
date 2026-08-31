"""S6 campaigns: governed capability orchestration across an estate.

Pure. Every input is fetched by the router or the runner and handed in,
so the whole of the judgement is unit-testable without a database --
the same shape as `autonomy.build_autonomy` and
`capabilities.build_capability_registry`, deliberately.

WHAT A CAMPAIGN IS
------------------
One governed action class, run across a scoped set of devices, under
every governance contract the platform already has. It is **generic
capability orchestration, not firmware campaigns moved to Central
Command**: all fourteen ActionTypes ride the same machinery, and the
differentiation between them comes from ACTION_RISK, the autonomy
contract, approval policy and the node's own gates -- never from a
special case in here.

WHAT IT MAY NOT DO
------------------
It owns campaign lifecycle, targeting, preflight, governance, approval
and SITE ordering. It does **not** own blast radius: fault domains live
at the Site Manager, which plans device waves with `plan_waves()`
against real domain data. Central Command must never invent or
approximate that information, so nothing here computes a device wave.

It creates no capability catalogue (the Capability Registry is the only
one), no approval model (the E0.1 records ledger is the only one), no
authorization model (E1.2 scope is the only one) and no execution engine
(`DispatchAction` onto the existing directive transport and node funnel
is the only one).

THE RULE THAT KEEPS PREFLIGHT HONEST
------------------------------------
Capability truth decays. A preflight taken on Monday can be wrong by
Wednesday: an allow list changes, an agent is rolled back, a device is
re-protocolled. So capability and policy are revalidated immediately
before every site-wave dispatch, and that revalidation may only ever
**narrow** the approved set. Widening it would let a device nobody
approved be executed on, which is the whole reason approval bounds a
target set in the first place.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from harkeniq.capabilities import effective_actions
from harkeniq_cc.capabilities import implemented_actions

# -- campaign lifecycle ------------------------------------------------------
#
# `acknowledged` sits between preflight and approval on purpose (D2): a
# campaign carrying warned targets cannot be put in front of an approver
# until a named human has either excluded them or accepted them.

STATUS_DRAFT = "draft"
STATUS_PREFLIGHTED = "preflighted"
STATUS_ACKNOWLEDGED = "acknowledged"
STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_HALTED = "halted"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_HALTED, STATUS_CANCELLED)
#: Statuses in which the configuration may still be edited. An edit
#: after this point would invalidate a decision someone already made.
EDITABLE_STATUSES = (STATUS_DRAFT, STATUS_PREFLIGHTED, STATUS_ACKNOWLEDGED)

# -- applicability (what PREFLIGHT decided) ----------------------------------

APPLICABILITY_ELIGIBLE = "eligible"
#: Implemented, but this node's allow list does not currently permit it.
#: Included, never silently dropped, and requires an explicit human
#: acknowledgement before the campaign may seek approval (D2).
APPLICABILITY_WARN = "warn_not_permitted"
#: The device has not declared. Never fabricate incapability from
#: silence -- it is surfaced, and handled like a warned target.
APPLICABILITY_UNKNOWN = "unknown"
#: No protocol on this device implements the class. Excluded before
#: dispatch, with the reason recorded.
APPLICABILITY_EXCLUDED = "excluded_unimplemented"
#: A named human removed it during the acknowledgement step.
APPLICABILITY_EXCLUDED_BY_OPERATOR = "excluded_by_operator"

#: The verdicts that put a campaign in front of a human before approval.
NEEDS_ACKNOWLEDGEMENT = (APPLICABILITY_WARN, APPLICABILITY_UNKNOWN)
#: The verdicts that are dispatched.
DISPATCHABLE = (APPLICABILITY_ELIGIBLE, APPLICABILITY_WARN, APPLICABILITY_UNKNOWN)

# -- revalidation verdicts (what DISPATCH TIME decided) ----------------------

REVAL_OK = "ok"
#: The capability disappeared between approval and dispatch (agent
#: downgraded, protocol changed). Skipped -- this is not a device
#: failure and must not halt the wave.
REVAL_LOST_CAPABILITY = "lost_capability"
#: Policy closed after approval, on a target nobody acknowledged.
REVAL_NEWLY_DENIED = "newly_policy_denied"
#: The device is gone from the fleet entirely.
REVAL_ABSENT = "absent"


def target_applicability(
    device: Any,
    action_type: str,
    platform_implemented: bool,
) -> tuple[str, str]:
    """What preflight decides about one device. Returns (verdict, reason).

    The three states this must keep apart are the entire point of the
    Capability Registry: no code anywhere, no code on THIS device, and
    code that this node does not currently permit. Only the first two
    are capability facts; the third is policy, and policy never means
    the capability is absent.
    """
    if not platform_implemented:
        return (
            APPLICABILITY_EXCLUDED,
            f"no executor in this platform implements {action_type}",
        )
    declaration = getattr(device, "capabilities", None)
    implemented = implemented_actions(declaration)
    if implemented is None:
        return (
            APPLICABILITY_UNKNOWN,
            "this device has not declared its capabilities; reach is "
            "unknown, which is not the same as incapable",
        )
    if action_type not in implemented:
        return (
            APPLICABILITY_EXCLUDED,
            f"this device's protocol does not implement {action_type}",
        )
    permitted = effective_actions(declaration)
    if permitted is not None and action_type not in permitted:
        return (
            APPLICABILITY_WARN,
            f"{action_type} is implemented here but the node's allow list "
            f"does not currently permit it; the node decides at execution "
            f"time and its refusal becomes evidence",
        )
    return (APPLICABILITY_ELIGIBLE, "")


def revalidate_target(
    device: Optional[Any],
    action_type: str,
    platform_implemented: bool,
    acknowledged: bool,
) -> tuple[str, str]:
    """Re-check one target immediately before dispatch. (verdict, reason).

    Called at the site-wave boundary, never once per campaign: the point
    is that the answer may have changed since approval.

    `acknowledged` says whether a human accepted policy-denied targets
    for this campaign version. A target that was already warned and
    acknowledged proceeds to the node, which remains the final policy
    authority and whose refusal becomes evidence (A17.7). A target that
    became policy-denied AFTER that acknowledgement was never accepted
    by anybody, so it is skipped and surfaced rather than dispatched on
    an assumption nobody made.
    """
    if device is None:
        return (REVAL_ABSENT, "device is no longer in this tenant's fleet")
    if not platform_implemented:
        return (
            REVAL_LOST_CAPABILITY,
            f"no executor in this platform implements {action_type} any more",
        )
    declaration = getattr(device, "capabilities", None)
    implemented = implemented_actions(declaration)
    if implemented is not None and action_type not in implemented:
        return (
            REVAL_LOST_CAPABILITY,
            f"this device no longer implements {action_type}; it was "
            f"capable when the campaign was approved",
        )
    permitted = effective_actions(declaration)
    if permitted is not None and action_type not in permitted and not acknowledged:
        return (
            REVAL_NEWLY_DENIED,
            f"the node's allow list stopped permitting {action_type} after "
            f"this campaign was approved, and no acknowledgement covers it",
        )
    return (REVAL_OK, "")


def narrow_only(approved_ids: Iterable[str], revalidated_ids: Iterable[str]) -> set[str]:
    """The set that may actually be dispatched.

    Revalidation may REMOVE a target and may never ADD one. A device
    that became capable after approval is not swept into the run: the
    approved target set is the blast radius a person signed off, and
    widening it at dispatch time would turn revalidation into a way to
    execute on devices nobody approved.

    Enforced as an intersection rather than trusted to callers, because
    "we only ever remove" is exactly the kind of invariant that quietly
    stops being true.
    """
    return set(approved_ids) & set(revalidated_ids)


def acknowledgement_valid(campaign) -> bool:
    """Is the stored acknowledgement still the one this campaign needs?

    Bound to the version it was given for. Editing a campaign bumps the
    version, which invalidates it -- otherwise a person acknowledges v1
    and the estate runs v2.
    """
    if not campaign.acknowledged_by:
        return False
    return int(campaign.acknowledged_version or 0) == int(campaign.version)


def acknowledgement_required(targets: Iterable[Any]) -> list[Any]:
    """Targets a human must accept or exclude before approval (D2)."""
    return [
        t for t in targets
        if t.applicability in NEEDS_ACKNOWLEDGEMENT
    ]


def can_seek_approval(campaign, targets: Iterable[Any]) -> tuple[bool, str]:
    """May this campaign be put in front of an approver?

    Preflight is mandatory, and warned or unknown targets must have been
    settled by a named human first. An approver should never be the
    first person to discover that a third of the estate will refuse.
    """
    targets = list(targets)
    if campaign.status not in (STATUS_PREFLIGHTED, STATUS_ACKNOWLEDGED):
        return False, f"a campaign in status {campaign.status!r} cannot seek approval"
    if not campaign.preflight_at:
        return False, (
            "preflight is mandatory: a campaign cannot seek approval without "
            "a stored, reviewable applicability report"
        )
    dispatchable = [t for t in targets if t.applicability in DISPATCHABLE]
    if not dispatchable:
        return False, (
            "no device in scope can run this action class, so there is "
            "nothing to approve"
        )
    outstanding = acknowledgement_required(dispatchable)
    if outstanding and not acknowledgement_valid(campaign):
        return False, (
            f"{len(outstanding)} target(s) are implemented but not currently "
            f"permitted, or have not declared. A named person must exclude "
            f"or acknowledge them before this campaign can be approved."
        )
    return True, ""


def plan_site_order(
    site_ids: Iterable[str], site_names: dict[str, str]
) -> list[tuple[int, str, str]]:
    """Order the SITES. Deliberately not the devices.

    Central Command orders sites because only it knows the org tree; it
    has no fault-domain data at all, so planning device waves here would
    be a safety fiction. The Site Manager plans within its own site with
    `plan_waves()` against real domains.

    Deterministic by name so a re-run of the same campaign visits the
    estate in the same order, which is what makes a partial run
    resumable and reviewable.
    """
    ordered = sorted(set(site_ids), key=lambda s: (site_names.get(s, ""), s))
    return [(i, s, site_names.get(s, "")) for i, s in enumerate(ordered)]


def campaign_terminal_state(site_rows: Iterable[Any]) -> Optional[str]:
    """Has the campaign finished, and how?

    Partial success is first-class: a campaign where one site halted and
    seven completed is `halted` WITH per-site detail, never rounded up
    to completed or down to failed. Rounding it either way would hide
    the single fact an operator needs.
    """
    rows = list(site_rows)
    if not rows:
        return None
    if any(r.status in ("pending", "running") for r in rows):
        return None
    if any(r.status == "halted" for r in rows):
        return STATUS_HALTED
    return STATUS_COMPLETED


def campaign_progress(site_rows: Iterable[Any], targets: Iterable[Any]) -> dict:
    """What an operator asks: how far, how many, what went wrong where."""
    rows = list(site_rows)
    targets = list(targets)
    by_status: dict[str, int] = {}
    for t in targets:
        by_status[t.status] = by_status.get(t.status, 0) + 1
    by_applicability: dict[str, int] = {}
    for t in targets:
        by_applicability[t.applicability] = by_applicability.get(t.applicability, 0) + 1
    return {
        "sites_total": len(rows),
        "sites_completed": sum(1 for r in rows if r.status == "completed"),
        "sites_halted": sum(1 for r in rows if r.status == "halted"),
        "sites_running": sum(1 for r in rows if r.status == "running"),
        "sites_pending": sum(1 for r in rows if r.status == "pending"),
        "targets_total": len(targets),
        "targets_by_status": by_status,
        "targets_by_applicability": by_applicability,
        "partial_success": (
            any(r.status == "completed" for r in rows)
            and any(r.status == "halted" for r in rows)
        ),
    }


# ---------------------------------------------------------------------------
# Site-wave state: APPROVED != EXECUTABLE != EXECUTED
# ---------------------------------------------------------------------------
#
# Three questions that must never collapse into one flag:
#
#   APPROVED    a named human authorized THIS exact plan and device set
#   EXECUTABLE  re-evaluated at dispatch; may narrow, may refuse outright
#   EXECUTED    it ran, and the outcome says how it went
#
# An approval is an authorization, not a promise. Collapsing the first two
# is how a system ends up executing on last week's truth.

WAVE_AUTONOMOUS = "autonomous"          # no human required; no approval subject
WAVE_PENDING_APPROVAL = "pending_approval"
WAVE_APPROVED = "approved"
WAVE_DENIED = "denied"
WAVE_VOIDED = "voided"                  # Q3: predecessor failed
WAVE_DISPATCHED = "dispatched"
WAVE_COMPLETED = "completed"
WAVE_FAILED = "failed"

#: Waves that may still be dispatched, given everything else agrees.
WAVE_RUNNABLE = (WAVE_AUTONOMOUS, WAVE_APPROVED)
#: Waves a halted site must void: authorized, but not started.
WAVE_VOIDABLE = (WAVE_AUTONOMOUS, WAVE_PENDING_APPROVAL, WAVE_APPROVED)
WAVE_TERMINAL = (WAVE_DENIED, WAVE_VOIDED, WAVE_COMPLETED, WAVE_FAILED)


def wave_subject_ref(
    campaign_id: str,
    campaign_version: int,
    site_id: str,
    wave_index: int,
    device_agent_ids: Iterable[str],
    plan_hash: str,
) -> str:
    """The digest the approval ledger records against.

    `cc_approval_records.subject_ref` is 64 characters, which a readable
    composite of two 32-hex ids plus a hash cannot fit -- so the subject
    is a digest and the readable mapping lives in `cc_campaign_waves`.

    It covers all six binding components, computed DIRECTLY rather than
    transitively through the plan hash: campaign, version, site, wave,
    the wave's exact device set, and the plan. That is what makes the
    binding structural -- change any one and the digest stops addressing
    this subject, so a stale approval cannot authorize new work even if
    someone forgets to check.
    """
    import hashlib

    from harkeniq.audit.chain import canonical_json

    payload = canonical_json({
        "campaign_id": campaign_id,
        "campaign_version": int(campaign_version),
        "site_id": site_id,
        "wave_index": int(wave_index),
        "device_agent_ids": sorted(str(d) for d in device_agent_ids),
        "plan_hash": plan_hash,
    })
    return hashlib.sha256(payload).hexdigest()[:32]


def waves_to_void(wave_rows: Iterable[Any], site_id: str) -> list[Any]:
    """Which of a halted site's waves lose their authorization (Q3).

    A later wave was approved as part of a sequence whose predecessor has
    now failed, so the assumption behind that approval no longer holds.
    Leaving it approved is stale authorization, and stale authorization
    is how a halted site quietly resumes.

    Only THIS site's waves. Another site's approvals are untouched: a
    halted site is not a halted campaign.
    """
    return [
        w for w in wave_rows
        if w.site_id == site_id and w.status in WAVE_VOIDABLE
    ]


def plan_is_current(stored_hash: str, replanned_hash: str) -> bool:
    """Did the site's plan survive to dispatch time?

    A difference means the estate changed materially -- a device joined
    the site, a fault domain moved, membership shifted. The wave is then
    REFUSED rather than narrowed: narrowing a plan that no longer exists
    would execute a subset of an authorization nobody gave.
    """
    return bool(stored_hash) and stored_hash == replanned_hash
