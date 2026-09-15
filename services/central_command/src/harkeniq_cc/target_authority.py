"""Authorization over a SET of targets (A6-4B0b-S1, spec A30.22).

A campaign site-wave is the platform's one first-class MULTI-target
authorization subject: an immutable device set bound to a plan hash,
raised as one approval subject and dispatched as one unit. Every other
origin -- a node action, an agent proposal, an activation -- names one
target, and the approval gate was written for that shape. The wave path
satisfied it with ``device_agent_ids[0]``, so a principal whose grant
reached ONE device approved work on all of them.

The rule this module holds, where it can be read as a rule:

    AUTHORIZED  ==  EVERY required target is covered by the decider's
                    CURRENT canonical effective scope.

Never "any", never the first, never a representative, never the site as a
proxy where target-specific authority is required, never a visible
subset. One uncovered target refuses the whole set. The set itself is
never narrowed, filtered or recomputed to fit the caller -- capability may
narrow what EXECUTES (A18 D2, `revalidate_wave`, unchanged); authority
never narrows what was APPROVED.

Everything here consumes the canonical `ResolvedScope` and asks it the
canonical question, `permits(...)`, once per target. Nothing here reads a
grant row as authority, and nothing here is a second scope model: the
helpers are loops over the one resolver's answer, kept small enough that
the security semantics are visible in the code rather than in a comment.

At dispatch (A30.23) the permission basis each approver is resolved with
is their CURRENT realm role, fetched from the identity plane and turned
into permissions by the request path's own `auth.role_basis` -- never the
role their approval recorded, which is evidence and stays untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

#: The permission a wave approver must hold over EVERY device in the wave.
WAVE_PERMISSION = "action.approve"


class TargetIntegrityError(ValueError):
    """The target set cannot be authorized AS STATED.

    Raised BEFORE any authority question is asked: an empty set, a
    duplicate, a device Central Command cannot identify at the wave's
    site, or a stored wave whose recomputed digest no longer matches its
    own subject. Refusing here is what keeps "we could not check" from
    ever reading as "checked and allowed".
    """

    def __init__(self, reason: str, detail: Optional[dict] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = dict(detail or {})


@dataclass(frozen=True)
class AuthorizedTarget:
    """One device as the authorization question sees it.

    `device_class` is the CURRENT fleet class, or "" when the fleet row
    carries none. It is deliberately not defaulted: an empty class never
    matches a `device_class` grant (`Grant.covers_device`'s rule), which is
    the fail-closed reading spec A30.22 ratifies for this path.
    """

    device_agent_id: str
    site_id: str
    device_class: str


# ---------------------------------------------------------------------------
# The immutable target set
# ---------------------------------------------------------------------------


def wave_subject_matches(wave: Any) -> bool:
    """Does the stored wave still describe the subject the ledger recorded?

    `wave_subject_ref` is a digest over campaign, version, site, wave
    index, the exact device list and the plan hash. It was computed once,
    at `build_waves`, and used only as a lookup KEY -- so an out-of-band
    write to `device_agent_ids` left every check green. Recomputing it here
    turns the digest into a verified binding: a wave that no longer hashes
    to its own `subject_ref` is refused, whoever changed it.

    Only meaningful for a wave that carries a subject (a human-approved
    one). An autonomous wave raises no subject and stores "".
    """
    from harkeniq_cc.campaigns import wave_subject_ref

    stored = getattr(wave, "subject_ref", "") or ""
    if not stored:
        return False
    recomputed = wave_subject_ref(
        wave.campaign_id,
        int(wave.campaign_version),
        wave.site_id,
        int(wave.wave_index),
        list(getattr(wave, "device_agent_ids", None) or []),
        wave.plan_hash,
    )
    return recomputed == stored


def wave_targets(wave: Any, fleet_rows: Iterable[Any]) -> tuple[AuthorizedTarget, ...]:
    """The IMMUTABLE target set of one site-wave, identified against the
    CURRENT fleet.

    `fleet_rows` are the fleet-cache rows the caller read for the wave's
    site (one indexed read, never one per device). Refuses, rather than
    narrows, when the set is empty, when an id repeats, or when a device
    the wave names has no row AT THAT SITE -- a device Central Command
    cannot identify cannot be authorized, and a device that has moved
    sites is not the device the approver looked at.

    Returned sorted for determinism; the order carries no authority.
    """
    ids = [str(d) for d in (getattr(wave, "device_agent_ids", None) or [])]
    if not ids:
        raise TargetIntegrityError(
            "empty_target_set", {"site_id": getattr(wave, "site_id", "")},
        )
    if len(set(ids)) != len(ids):
        dupes = sorted(d for d in set(ids) if ids.count(d) > 1)
        raise TargetIntegrityError("duplicate_target", {"devices": dupes})

    site_id = getattr(wave, "site_id", "") or ""
    by_id: dict[str, Any] = {}
    for row in fleet_rows:
        if getattr(row, "site_id", None) != site_id:
            continue  # a row from another site is not this wave's device
        by_id[str(row.agent_id)] = row

    unknown = sorted(d for d in ids if d not in by_id)
    if unknown:
        raise TargetIntegrityError(
            "unknown_target", {"site_id": site_id, "devices": unknown},
        )
    return tuple(
        AuthorizedTarget(
            device_agent_id=d,
            site_id=site_id,
            device_class=(getattr(by_id[d], "device_class", "") or ""),
        )
        for d in sorted(ids)
    )


# ---------------------------------------------------------------------------
# The all-target question
# ---------------------------------------------------------------------------


def uncovered_targets(
    scope: Any, permission: str, targets: Sequence[AuthorizedTarget]
) -> tuple[str, ...]:
    """The device ids in `targets` this scope does NOT hold `permission` over.

    An empty tuple means every target is covered. Each target is asked
    through the canonical `ResolvedScope.permits` with its own site,
    device id and class, so a `device` grant matches one id, a
    `device_class` grant matches every target of that class and none of
    another, and tenant / site / org_unit grants fall through to
    `covers_site` exactly as they do for a single-target subject.

    An empty `targets` is not "nothing to refuse"; it is a set that cannot
    be authorized, and it raises.
    """
    if not targets:
        raise TargetIntegrityError("empty_target_set")
    return tuple(
        t.device_agent_id
        for t in targets
        if not scope.permits(
            permission,
            site_id=t.site_id,
            device_agent_id=t.device_agent_id,
            device_class=t.device_class,
        )
    )


def all_targets_covered(
    scope: Any, permission: str, targets: Sequence[AuthorizedTarget]
) -> bool:
    """True only when `uncovered_targets` is empty. Spelled out so a caller
    cannot pass a filtered subset by accident: it takes the whole set."""
    return not uncovered_targets(scope, permission, targets)


# ---------------------------------------------------------------------------
# Policy over a target set
# ---------------------------------------------------------------------------


async def governing_policy_for_targets(
    session: Any,
    tenant_id: str,
    action_type: str,
    targets: Sequence[AuthorizedTarget],
) -> tuple[Any, Any, list, tuple[str, ...]]:
    """(policy, group, members, conflict) for a target SET.

    The single-target `governing_policy` resolves from one device's class.
    For a set, the policy is resolved per DISTINCT class present; when
    every class resolves to the same policy that policy governs. When the
    classes resolve to DIFFERENT policies the fourth element names them
    and the first is None: a wave that two policies govern differently
    cannot be signed under either, so the caller refuses with the reason.
    Deterministic and fail-closed; no multi-policy arithmetic is invented.
    """
    from harkeniq_cc.approval_policy import resolve_policy_for_classes
    from harkeniq_cc.autonomy import action_risk_map
    from harkeniq_cc.db.repos import ApprovalGroupRepo, ApprovalPolicyRepo

    policies = await ApprovalPolicyRepo(session).list_all(tenant_id)
    risk = action_risk_map().get((action_type or "").upper(), "")
    policy, conflict = resolve_policy_for_classes(
        policies,
        action_type=action_type,
        device_classes=[t.device_class for t in targets],
        risk=risk,
    )
    if conflict:
        return None, None, [], conflict
    group = None
    members: list = []
    group_id = getattr(policy, "group_id", None) if policy is not None else None
    if group_id:
        repo = ApprovalGroupRepo(session)
        group = await repo.get_by_id(group_id)
        if group is not None and group.tenant_id != tenant_id:
            # A policy pointing at another tenant's group is a
            # misconfiguration, not an authorization path.
            group = None
        if group is not None:
            members = list(await repo.list_members(group.id))
    return policy, group, members, ()


# ---------------------------------------------------------------------------
# Current authority at dispatch
# ---------------------------------------------------------------------------

#: `CurrentBasis.status` vocabulary. Closed: the withheld audit entry
#: names one of these per lost approver, so a demotion is distinguishable
#: from a revocation and from an outage without opening Keycloak.
BASIS_RESOLVED = "resolved"
BASIS_PRINCIPAL_NOT_FOUND = "principal_not_found"
BASIS_PRINCIPAL_DISABLED = "principal_disabled"
BASIS_UNRESOLVABLE = "unresolvable"
BASIS_STATUSES = frozenset({
    BASIS_RESOLVED, BASIS_PRINCIPAL_NOT_FOUND, BASIS_PRINCIPAL_DISABLED,
    BASIS_UNRESOLVABLE,
})

#: Why one approver no longer counts. `current_role` and `scope` are the
#: two halves of authority (A30.23); the rest are identity statuses.
CAUSE_CURRENT_ROLE = "current_role"
CAUSE_SCOPE = "scope"
LOSS_CAUSES = frozenset({CAUSE_CURRENT_ROLE, CAUSE_SCOPE}) | (BASIS_STATUSES - {BASIS_RESOLVED})


@dataclass(frozen=True)
class CurrentBasis:
    """One principal's CURRENT permission basis, or why there is none.

    Produced from the realm roles the identity plane reports NOW and the
    ONE role rule the request path uses (`auth.role_basis`). Only
    `resolved` carries a role; every other status is a refusal with the
    reason, and none of them ever fall back to a recorded role.
    """

    status: str
    role: str = ""
    permissions: tuple[str, ...] = ()
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.status == BASIS_RESOLVED


async def approver_current_basis(state: Any, *, realm: str, subject: str) -> CurrentBasis:
    """The CURRENT permission basis of one principal, WITHOUT a token.

    A30.23. The approval recorded the role its approver's token carried
    when they decided; that is evidence, not a basis. The basis comes
    from the realm roles Keycloak holds for the subject NOW, read over
    the existing CC->Console internal channel (Central Command holds no
    Keycloak admin credential, A20) and turned into permissions by the
    same `role_basis` the request path calls for a live token.

    Fail closed, with the cause named: no configured realm, no subject,
    an unreachable Console, a realm the Console cannot bind to a tenant,
    a malformed answer, a subject the realm does not hold, or a disabled
    account each yield a basis that carries nothing.
    """
    from harkeniq_cc import identity_client
    from harkeniq_cc.auth import role_basis

    if not realm:
        return CurrentBasis(
            BASIS_UNRESOLVABLE,
            reason="no tenant realm is configured, so current authority cannot be resolved",
        )
    if not subject:
        return CurrentBasis(BASIS_UNRESOLVABLE, reason="the approval names no principal")
    body, reason = await identity_client.current_authority(
        state, realm=realm, subject=subject,
    )
    if body is None:
        return CurrentBasis(BASIS_UNRESOLVABLE, reason=reason or "no answer from the identity plane")
    if not isinstance(body, dict) or body.get("realm") != realm or body.get("subject") != subject:
        return CurrentBasis(
            BASIS_UNRESOLVABLE,
            reason="the identity plane answered for a different realm or principal",
        )
    if not body.get("found"):
        return CurrentBasis(
            BASIS_PRINCIPAL_NOT_FOUND,
            reason=f"realm {realm!r} no longer holds this principal",
        )
    if not body.get("enabled"):
        return CurrentBasis(BASIS_PRINCIPAL_DISABLED, reason="the principal's account is disabled")
    roles = body.get("realm_roles")
    if not isinstance(roles, list):
        return CurrentBasis(
            BASIS_UNRESOLVABLE, reason="the identity plane reported no role list",
        )
    role, permissions = role_basis([str(r) for r in roles])
    return CurrentBasis(BASIS_RESOLVED, role=role, permissions=tuple(permissions))


def current_authority_source(state: Any, realm: str):
    """The production `authority_source`: subject -> `CurrentBasis`, from
    the application state's identity-plane channel and the configured
    tenant realm. Built by the runner and handed in; there is no default
    inside the gate, because a default would be the historical role
    surviving as a fallback."""

    async def _source(subject: str) -> CurrentBasis:
        return await approver_current_basis(state, realm=realm, subject=subject)

    return _source


async def approver_current_scope(
    session: Any, *, tenant_id: str, realm: str, record: Any, basis: CurrentBasis
) -> Any:
    """The CURRENT scope of one ledger approver, resolved WITHOUT a token.

    The permission basis is the one `approver_current_basis` resolved
    from the approver's CURRENT realm roles (A30.23) -- never the role the
    approval recorded -- applied to the grants they hold NOW, through the
    one loader every principal resolves through. Inside `resolve()` the
    grant-recorded role ceiling (A23-3) still narrows.

    A basis that is not `resolved` carries no permissions: every grant
    drops and the approver covers nothing. Callers refuse before reaching
    here in that case; this function simply cannot be made to widen.
    """
    from harkeniq_cc.governance import PRINCIPAL_USER, load_scope

    email = (getattr(record, "approver_email", "") or "").strip()
    return await load_scope(
        session,
        tenant_id=tenant_id,
        principal_ref=record.approver_ref,
        role_permissions=list(basis.permissions) if basis.resolved else [],
        principal_type=PRINCIPAL_USER,
        realm=realm,
        aliases=(email,) if email else (),
    )


async def revalidate_wave_authority(
    session: Any,
    *,
    tenant_id: str,
    realm: str,
    targets: Sequence[AuthorizedTarget],
    records: Sequence[Any],
    needed: int,
    authority_source: Any,
) -> dict:
    """Does this wave's approval STILL stand on current authority?

    Each APPROVED ledger record is re-asked, with its approver's CURRENT
    permission basis (`authority_source`, A30.23) and current scope,
    whether it holds `action.approve` over EVERY target. E0.1's own
    completion rule is then re-run over the approvals that still do. The
    records themselves are not touched: approval is historical truth, and
    this decides only whether it is ALSO current execution authority.

    Each lost approver carries a cause from `LOSS_CAUSES`: the identity
    plane could not vouch for them, their current role lacks the
    permission, or their grants no longer cover the set. Denials are
    carried through unchanged so a terminal denial stays terminal (D16).
    Composition does not rescue a wave: two approvers who each cover part
    of the set are two approvers who each fail.
    """
    from harkeniq_cc.approval_policy import (
        DECISION_APPROVED,
        STATE_APPROVED,
        evaluate_completion,
    )

    lost: list[dict] = []
    standing: list[Any] = []
    for record in records:
        if record.decision != DECISION_APPROVED:
            standing.append(record)
            continue
        basis = await authority_source(record.approver_ref)
        if not isinstance(basis, CurrentBasis) or not basis.resolved:
            status = getattr(basis, "status", BASIS_UNRESOLVABLE)
            lost.append({
                "approver_ref": record.approver_ref,
                "cause": status if status in LOSS_CAUSES else BASIS_UNRESOLVABLE,
                "current_role": "",
                "reason": (getattr(basis, "reason", "") or "")[:200],
                "uncovered": len(targets),
                "of": len(targets),
            })
            continue
        scope = await approver_current_scope(
            session, tenant_id=tenant_id, realm=realm, record=record, basis=basis,
        )
        missing = uncovered_targets(scope, WAVE_PERMISSION, targets)
        if missing:
            lost.append({
                "approver_ref": record.approver_ref,
                "cause": (
                    CAUSE_CURRENT_ROLE
                    if WAVE_PERMISSION not in basis.permissions
                    else CAUSE_SCOPE
                ),
                "current_role": basis.role,
                "reason": "",
                "uncovered": len(missing),
                "of": len(targets),
            })
        else:
            standing.append(record)

    block = evaluate_completion(standing, needed)
    ok = block["state"] == STATE_APPROVED
    reason = ""
    if not ok:
        causes = ",".join(sorted({entry["cause"] for entry in lost})) or "none"
        reason = (
            f"{len(lost)} approver(s) no longer hold {WAVE_PERMISSION!r} over "
            f"every device in this wave; {block['received']} of "
            f"{block['required']} approvals still stand (causes: {causes})"
        )
    return {
        "ok": ok,
        "reason": reason,
        "lost": lost,
        "standing": block["received"],
        "required": block["required"],
    }


async def wave_dispatch_authority(
    session: Any,
    *,
    tenant_id: str,
    realm: str,
    campaign: Any,
    wave: Any,
    fleet_rows: Iterable[Any],
    authority_source: Any,
) -> dict:
    """The dispatch-time authority verdict for one human-approved wave.

    Sits in `_advance_site` after the plan-hash check and before capability
    revalidation -- `revalidate_dispatch`'s position, for waves. It is not
    that function: its inputs are an agent's. Order of questions, each
    fail-closed on its own: the stored wave still hashes to its subject;
    the target set is identifiable at the wave's site; ONE policy governs
    the set; and every approver who completed the decision still holds
    the permission on their CURRENT role (`authority_source`, A30.23) and
    still covers every target. Returns ``{"ok", "reason", "detail"}`` and
    writes nothing; the caller owns what a refusal does to the wave.
    """
    from harkeniq_cc.approval_policy import SUBJECT_CAMPAIGN_WAVE, required_approvers
    from harkeniq_cc.db.repos import ApprovalRecordRepo

    if not wave_subject_matches(wave):
        return {
            "ok": False,
            "reason": (
                "this wave's device set no longer matches its approved "
                "subject; the approval cannot authorize it"
            ),
            "detail": {"kind": "subject_mismatch"},
        }
    try:
        targets = wave_targets(wave, fleet_rows)
    except TargetIntegrityError as exc:
        return {
            "ok": False,
            "reason": f"this wave cannot be authorized as stated: {exc.reason}",
            "detail": {"kind": exc.reason, **exc.detail},
        }
    policy, group, _members, conflict = await governing_policy_for_targets(
        session, tenant_id, campaign.action_type, targets,
    )
    if conflict:
        return {
            "ok": False,
            "reason": (
                "the devices in this wave are governed by different approval "
                "policies; split the campaign by device class"
            ),
            "detail": {"kind": "policy_conflict", "policies": list(conflict)},
        }
    records = await ApprovalRecordRepo(session).list_for_subject(
        SUBJECT_CAMPAIGN_WAVE, wave.subject_ref,
    )
    verdict = await revalidate_wave_authority(
        session,
        tenant_id=tenant_id,
        realm=realm,
        targets=targets,
        records=records,
        needed=required_approvers(policy, group),
        authority_source=authority_source,
    )
    return {
        "ok": verdict["ok"],
        "reason": verdict["reason"],
        "detail": {
            "kind": "authority_lost" if not verdict["ok"] else "",
            "lost": verdict["lost"],
            "standing": verdict["standing"],
            "required": verdict["required"],
            "targets": [t.device_agent_id for t in targets],
        },
    }
