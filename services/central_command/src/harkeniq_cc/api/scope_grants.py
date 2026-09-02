"""Scope grants and the tenant's enforcement posture (E1.2).

Where authorization is administered. A grant is
``(principal, permission subset, scope refs)`` -- ratified decision B --
and it is the ONLY thing that confers authority. The organizational tree
says where a site sits and grants nobody anything.

Two rules this router exists to enforce, both of them arithmetic rather
than review:

* **Delegation cannot exceed the delegator.** A grantor may only hand
  out scope they themselves hold, checked against their own resolved
  scope. `role.manage` is required to grant at all.
* **The last administrator cannot be locked out.** Switching a tenant to
  strict enforcement runs a server-side preflight (ratified L1) and
  refuses, atomically, if no active unexpired principal would still hold
  `role.manage` at tenant scope afterwards.

The preflight lives here and NOT in the resolver. A resolver that knew
about administrators would carry that special case into every future
caller of it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import (
    forbid_out_of_scope,
    get_cc_state,
    get_scope,
    get_session,
    require_any_permission,
    require_permission,
)
from harkeniq_cc.actor import actor_of
from harkeniq_cc.auth import ROLE_PERMISSIONS
from harkeniq_cc.db.repos import (
    AuditRepo,
    OrgUnitRepo,
    ScopeGrantRepo,
    SiteRepo,
    TenantSettingsRepo,
)
from harkeniq_cc.scope import (
    ENFORCEMENT_MODES,
    ENFORCEMENT_STRICT,
    PRINCIPAL_TYPES,
    PRINCIPAL_USER,
    SCOPE_DEVICE,
    SCOPE_DEVICE_CLASS,
    SCOPE_ORG_UNIT,
    SCOPE_SITE,
    SCOPE_TENANT,
    SCOPE_TYPES,
    effective_permissions,
    preflight_strict,
)

logger = logging.getLogger("harkeniq.cc.api.scope_grants")

router = APIRouter(prefix="/api/scope-grants", tags=["scope-grants"])
settings_router = APIRouter(prefix="/api/tenant-settings", tags=["tenant-settings"])


class GrantRequest(BaseModel):
    principal_ref: str = Field(..., min_length=1, max_length=128)
    principal_type: str = Field(PRINCIPAL_USER, max_length=16)
    scope_type: str = Field(..., max_length=16)
    scope_ref: str = Field("", max_length=128)
    #: The role's permissions are the ceiling; this list intersects them.
    #: Omit for "the role's full set".
    permission_subset: Optional[list[str]] = None
    #: The role this grant narrows. Needed because CC resolves a person's
    #: role from their token, and the grantor is not that person.
    role: str = Field("", max_length=64)
    expires_at: Optional[datetime] = None
    note: str = Field("", max_length=512)


class EnforcementRequest(BaseModel):
    mode: str = Field(..., description="legacy_open | strict")


def _grant_dict(row) -> dict:
    return {
        "id": row.id,
        "principal_type": row.principal_type,
        "principal_ref": row.principal_ref,
        "scope_type": row.scope_type,
        "scope_ref": row.scope_ref,
        "permission_subset": row.permission_subset,
        "role": row.role,
        "granted_by": row.granted_by,
        "granted_at": row.granted_at.isoformat() if row.granted_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "revoked_by": row.revoked_by,
        "note": row.note,
    }


async def _resolve_scope_ref(session, tenant_id: str, scope_type: str, ref: str):
    """Validate the target exists. A grant to nothing is a silent no-op."""
    if scope_type == SCOPE_TENANT:
        return ""
    if not ref:
        raise HTTPException(
            status_code=400,
            detail=f"scope_type {scope_type!r} requires a scope_ref",
        )
    if scope_type == SCOPE_ORG_UNIT:
        unit = await OrgUnitRepo(session).get(tenant_id, ref)
        if unit is None:
            raise HTTPException(status_code=404, detail="org unit not found")
        return unit.path
    if scope_type == SCOPE_SITE:
        site = await SiteRepo(session).get_by_id(ref)
        if site is None or site.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="site not found")
        return ""
    if scope_type == SCOPE_DEVICE_CLASS:
        if ref.lower() not in ("server", "switch"):
            raise HTTPException(
                status_code=400,
                detail="device_class must be 'server' or 'switch'",
            )
        return ""
    if scope_type == SCOPE_DEVICE:
        return ""
    raise HTTPException(status_code=400, detail=f"unknown scope_type {scope_type!r}")


def _grant_visible(scope, row) -> bool:
    """Could this caller have made this grant? Then they may read it."""
    if getattr(scope, "tenant_wide", False):
        return True
    if row.scope_type == SCOPE_SITE:
        return row.scope_ref in scope.site_ids
    if row.scope_type == SCOPE_ORG_UNIT:
        return scope.covers_org_unit_id(row.scope_ref)
    # tenant, device_class and device grants are handed out only by a
    # tenant-wide grantor, so only a tenant-wide reader sees them.
    return False


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@router.get("/")
async def list_grants(
    principal_ref: str = "",
    principal_type: str = "",
    include_revoked: bool = False,
    user=Depends(require_any_permission("user.view", "audit.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Who may reach what, in this tenant.

    Read at `user.view` or `audit.view` -- the A13 precedent: an auditor
    reads the evidence of who was authorized without being able to
    change it.
    """
    repo = ScopeGrantRepo(session)
    if principal_ref:
        rows = await repo.list_for_principal(
            user.tenant_id, principal_ref,
            principal_type=principal_type or PRINCIPAL_USER,
            include_revoked=include_revoked,
        )
    else:
        rows = await repo.list_all(
            user.tenant_id, principal_type=principal_type,
            include_revoked=include_revoked,
        )
    # A23 (READ_SCOPED, made true): a grant is visible to a caller who
    # could have MADE it -- the same coverage rule `create_grant` applies
    # as its delegation ceiling. A cluster administrator reads the
    # cluster's delegations; the tenant's whole authorization map is a
    # tenant-scope read. Out-of-scope grants are absent, never 403.
    rows = [r for r in rows if _grant_visible(scope, r)]
    return {
        "grants": [_grant_dict(r) for r in rows],
        "scope_types": list(SCOPE_TYPES),
        "principal_types": list(PRINCIPAL_TYPES),
        "enforcement": await TenantSettingsRepo(session).enforcement(user.tenant_id),
        "tenant_id": user.tenant_id,
    }


@router.get("/me")
async def my_scope(
    user=Depends(require_any_permission("fleet.view", "audit.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """What the CALLER may reach. Every principal may read their own.

    `contextual_unit_ids` is returned separately from the authority
    fields and marked as such: it is what makes a breadcrumb render, and
    it confers nothing.
    """
    return {
        "principal_type": scope.principal_type,
        "principal_ref": scope.principal_ref,
        "enforcement": scope.enforcement,
        "tenant_wide": scope.tenant_wide,
        "site_ids": sorted(scope.site_ids),
        "org_unit_paths": sorted(scope.org_unit_paths),
        "device_ids": sorted(scope.device_ids),
        "device_classes": sorted(scope.device_classes),
        "contextual_unit_ids": {
            "ids": sorted(scope.contextual_unit_ids),
            "authority": False,
            "note": (
                "visible for navigation only; seeing an ancestor is not "
                "authority over it"
            ),
        },
        "grants": [
            {
                "scope_type": g.scope_type,
                "scope_ref": g.scope_ref,
                "permissions": sorted(g.permissions),
            }
            for g in scope.grants
        ],
    }


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


@router.post("/", status_code=201)
async def create_grant(
    body: GrantRequest,
    user=Depends(require_permission("role.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
    state=Depends(get_cc_state),
) -> dict:
    """Grant scope to a principal, within the grantor's own authority."""
    if body.scope_type not in SCOPE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"scope_type must be one of {', '.join(SCOPE_TYPES)}",
        )
    if body.principal_type not in PRINCIPAL_TYPES:
        raise HTTPException(status_code=400, detail="unknown principal_type")

    unit_path = await _resolve_scope_ref(
        session, user.tenant_id, body.scope_type, body.scope_ref
    )

    # The delegation ceiling. A grantor hands out only what they hold,
    # and contextual visibility of an ancestor is not holding it.
    if body.scope_type == SCOPE_TENANT:
        forbid_out_of_scope(
            scope, "role.manage", what="a tenant-wide grant", tenant_object=True
        )
    elif body.scope_type == SCOPE_ORG_UNIT:
        forbid_out_of_scope(
            scope, "role.manage",
            what=f"org unit {body.scope_ref!r}", org_unit_path=unit_path,
        )
    elif body.scope_type == SCOPE_SITE:
        forbid_out_of_scope(
            scope, "role.manage",
            what=f"site {body.scope_ref!r}", site_id=body.scope_ref,
        )
    else:
        # device and device_class span whatever they match, so only a
        # tenant-wide grantor may hand them out.
        forbid_out_of_scope(
            scope, "role.manage",
            what=f"{body.scope_type} {body.scope_ref!r}", tenant_object=True,
        )

    # A subset can only narrow. Checked HERE too, not merely at resolve
    # time, so an attempt to widen is refused visibly rather than
    # silently reduced to nothing later.
    if body.permission_subset is not None and body.role:
        role_perms = ROLE_PERMISSIONS.get(body.role, [])
        granted = effective_permissions(role_perms, body.permission_subset)
        rejected = sorted(set(body.permission_subset) - set(granted))
        if rejected and "*" not in role_perms:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"role {body.role!r} does not hold {', '.join(rejected)}; a "
                    "permission subset may only narrow a role, never widen it"
                ),
            )

    grant = await ScopeGrantRepo(session).grant(
        tenant_id=user.tenant_id,
        principal_type=body.principal_type,
        principal_ref=body.principal_ref,
        scope_type=body.scope_type,
        scope_ref=body.scope_ref,
        permission_subset=body.permission_subset,
        role=body.role,
        realm=getattr(state.config, "keycloak_realm", "") or "",
        granted_by=user.user_id,
        expires_at=body.expires_at,
        note=body.note,
    )
    await AuditRepo(session).append(
        actor=user.user_id, actor_ref=actor_of(user),
        action="scope.granted",
        subject=body.principal_ref,
        tenant_id=user.tenant_id,
        site_id=body.scope_ref if body.scope_type == SCOPE_SITE else None,
        detail={
            "principal_type": body.principal_type,
            "scope_type": body.scope_type,
            "scope_ref": body.scope_ref,
            "permission_subset": body.permission_subset,
            "expires_at": body.expires_at.isoformat() if body.expires_at else None,
        },
    )
    await session.commit()
    return _grant_dict(grant)


@router.delete("/{grant_id}")
async def revoke_grant(
    grant_id: str,
    user=Depends(require_permission("role.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Revoke a grant. A timestamp, never a delete.

    An approval recorded under this grant keeps a `scope_snapshot` that
    has to stay addressable afterwards (ratified L2).
    """
    repo = ScopeGrantRepo(session)
    grant = await repo.get(user.tenant_id, grant_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="grant not found")

    if grant.scope_type == SCOPE_SITE:
        forbid_out_of_scope(
            scope, "role.manage",
            what=f"site {grant.scope_ref!r}", site_id=grant.scope_ref,
        )
    elif grant.scope_type == SCOPE_ORG_UNIT:
        unit = await OrgUnitRepo(session).get(user.tenant_id, grant.scope_ref)
        forbid_out_of_scope(
            scope, "role.manage",
            what=f"org unit {grant.scope_ref!r}",
            org_unit_path=unit.path if unit else "",
        )
    else:
        forbid_out_of_scope(
            scope, "role.manage",
            what=f"a {grant.scope_type} grant", tenant_object=True,
        )

    await repo.revoke(grant, user.user_id)
    await AuditRepo(session).append(
        actor=user.user_id, actor_ref=actor_of(user),
        action="scope.revoked",
        subject=grant.principal_ref,
        tenant_id=user.tenant_id,
        detail={
            "grant_id": grant.id,
            "scope_type": grant.scope_type,
            "scope_ref": grant.scope_ref,
        },
    )
    await session.commit()
    return _grant_dict(grant)


# ---------------------------------------------------------------------------
# Enforcement posture, and the L1 preflight
# ---------------------------------------------------------------------------


@settings_router.get("/scope-enforcement")
async def get_enforcement(
    user=Depends(require_any_permission("fleet.view", "audit.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
    state=Depends(get_cc_state),
) -> dict:
    mode = await TenantSettingsRepo(session).enforcement(user.tenant_id)
    check = await _preflight(session, user.tenant_id, caller_scope=scope)
    census = await ScopeGrantRepo(session).realm_census(user.tenant_id)
    current = getattr(state.config, "keycloak_realm", "") or ""
    usable = census.get(current, 0) + census.get("", 0)
    stale = sum(v for k, v in census.items() if k and k != current)
    return {
        "tenant_id": user.tenant_id,
        "scope_enforcement": mode,
        "modes": list(ENFORCEMENT_MODES),
        "strict_ready": check.ok,
        "strict_blocked_reason": check.reason,
        "tenant_admin_count": check.admin_count,
        # E1.4: a tenant moved to a new realm keeps grants naming
        # subjects from the old one. They authorize nothing, and without
        # this the condition is invisible -- every principal simply sees
        # nothing and nobody can say why.
        "realm": current,
        "grants_for_this_realm": usable,
        "stale_grants_from_other_realms": stale,
        "realm_census": census,
        "locked_out": bool(mode == "strict" and usable == 0 and stale > 0),
    }


@settings_router.get("/scope-enforcement/impact")
async def enforcement_impact(
    days: int = 90,
    user=Depends(require_any_permission("fleet.view", "audit.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Who would lose access if this tenant enforced scope today? (A22.10.)

    The report half of report-before-enforce. The final invariant is
    unconditional -- no grant means no operational scope, for humans and
    agents alike -- but `legacy_open` is the DEFAULT posture and an
    existing tenant may hold no grant rows at all, so enforcing it in
    the same slice that decides it would lock real customers out of a
    running system.

    Central Command cannot enumerate a realm's principals (that is
    Keycloak's, and E1.4's), so this reports the two populations it CAN
    name truthfully:

      * every Operational Agent, which CC owns outright, and
      * every principal OBSERVED acting in this tenant's audit log,

    against the grants that actually exist. An admin can act on both
    lists. What it deliberately does not do is guess at principals who
    have never acted -- `enumerable` says so, so nobody mistakes a short
    list for a complete one.
    """
    from datetime import datetime, timedelta, timezone

    from harkeniq_cc.db.models import CCAuditLog
    from harkeniq_cc.db.repos import OperationalAgentRepo
    from harkeniq_cc.scope import PRINCIPAL_AGENT, PRINCIPAL_USER, is_active
    from sqlalchemy import select

    tenant_id = user.tenant_id
    mode = await TenantSettingsRepo(session).enforcement(tenant_id)
    grants = await ScopeGrantRepo(session).list_all(tenant_id)
    granted = {
        (g.principal_type or PRINCIPAL_USER, g.principal_ref)
        for g in grants if is_active(g)
    }

    covered_agents = {ref for kind, ref in granted if kind == PRINCIPAL_AGENT}
    agents_at_risk = [
        {"agent_id": a.id, "name": a.name, "status": a.status}
        for a in await OperationalAgentRepo(session).list_all(tenant_id)
        if a.status != "retired" and a.id not in covered_agents
    ]

    since = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    observed = (await session.execute(
        select(CCAuditLog.actor, CCAuditLog.actor_ref)
        .where(CCAuditLog.tenant_id == tenant_id, CCAuditLog.ts >= since)
        .distinct()
    )).all()
    covered_users = {ref for kind, ref in granted if kind == PRINCIPAL_USER}
    agent_ids = {a.id for a in await OperationalAgentRepo(session).list_all(tenant_id)}
    census = _census_actors(
        observed,
        evidence=await _identity_evidence(session, tenant_id),
        covered_users=covered_users,
        agent_ids=agent_ids,
    )
    people_at_risk = sorted(census["without_grant"])

    return {
        "tenant_id": tenant_id,
        "scope_enforcement": mode,
        "enforced": mode == "strict",
        "active_grants": len(granted),
        "agents_without_grant": agents_at_risk,
        "observed_principals_without_grant": people_at_risk,
        # A23-2: how each of those was recognised, and what could NOT be.
        "observed_principals_detail": census["detail"],
        "unresolved_legacy_actors": census["unresolved"],
        "identity_basis": (
            "stable principal identity: cc_audit_log.actor_ref where present; "
            "a legacy actor is resolved only through actor_of() (subject, "
            "attribution key) or through in-repo evidence pairing an email "
            "with a subject (approval records, approval-group members). An "
            "email that no record pairs with a subject is reported as "
            "unresolved, never matched by guess and never counted as a "
            "different person."
        ),
        "observed_window_days": int(days),
        # The honest limit, stated in the payload rather than a doc: a
        # principal who has never acted cannot appear here.
        "enumerable": False,
        "enumerable_note": (
            "Central Command cannot list a realm's principals; this names "
            "every Operational Agent and every principal seen acting in the "
            "last {} days. A principal who has never acted will not appear."
        ).format(int(days)),
        "invariant": (
            "no grant -> no operational scope -> no operational data -> no "
            "proposal target"
        ),
    }


async def _identity_evidence(session, tenant_id: str) -> dict[str, str]:
    """Email -> stable subject, from records the platform itself wrote.

    Two stores pair an address with a subject at write time:
    `cc_approval_records` (approver_ref + approver_email, E0.1) and
    `cc_approval_group_members` (principal_ref + user_email). Those pairs
    are evidence, not inference: the subject and the address were
    observed together on one authenticated request. Nothing else is
    consulted, and an address with no such pair stays unresolved.
    """
    from sqlalchemy import select as _select

    from harkeniq_cc.db.models import (
        CCApprovalGroup,
        CCApprovalGroupMember,
        CCApprovalRecord,
    )

    out: dict[str, str] = {}
    rows = (await session.execute(
        _select(CCApprovalRecord.approver_email, CCApprovalRecord.approver_ref)
        .where(CCApprovalRecord.tenant_id == tenant_id)
        .distinct()
    )).all()
    rows += (await session.execute(
        _select(CCApprovalGroupMember.user_email, CCApprovalGroupMember.principal_ref)
        .join(CCApprovalGroup, CCApprovalGroup.id == CCApprovalGroupMember.group_id)
        .where(CCApprovalGroup.tenant_id == tenant_id)
        .distinct()
    )).all()
    for email, ref in rows:
        email = (email or "").strip().lower()
        ref = (ref or "").strip()
        if email and ref and "@" in email and "@" not in ref:
            out.setdefault(email, ref)
    return out


def _census_actors(observed, *, evidence, covered_users, agent_ids) -> dict:
    """Resolve every observed audit actor to a STABLE identity (A23-2).

    Pure. `observed` is (actor, actor_ref) pairs. Precedence: the stored
    `actor_ref`; then what the one helper can derive from the legacy
    string; then an in-repo email->subject pair. Agents, campaigns and
    the system are not people and are reported elsewhere or not at all.
    What cannot be resolved is listed as such -- an unrecognised display
    string is not evidence of a second person.
    """
    from harkeniq_cc.actor import actor_of

    # The ledger's own new rows are evidence too: `actor_of()` wrote the
    # display string and the stable reference together on ONE
    # authenticated request, which is exactly the pairing the approval
    # records carry. A person recorded by email before A23-2 and by
    # (email, subject) after it is one person, provably.
    evidence = dict(evidence)
    for actor, actor_ref in observed:
        actor = (actor or "").strip()
        if actor_ref and "@" in actor and "@" not in actor_ref:
            evidence.setdefault(actor.lower(), actor_ref)

    forms: dict[str, set[str]] = {}
    unresolved: set[str] = set()
    for actor, actor_ref in observed:
        actor = actor or ""
        ref = actor_ref or actor_of(actor) or evidence.get(actor.strip().lower())
        if not ref:
            if actor and not actor.startswith(("system", "campaign:", "machine:")):
                unresolved.add(actor)
            continue
        if ref in agent_ids or ref.startswith(("campaign:", "system")):
            continue
        forms.setdefault(ref, set()).add(actor)
    without = {ref for ref in forms if ref not in covered_users}
    return {
        "without_grant": without,
        "detail": [
            {"principal_ref": ref, "observed_as": sorted(forms[ref]), "granted": ref in covered_users}
            for ref in sorted(forms)
        ],
        "unresolved": sorted(unresolved),
    }


async def _preflight(session, tenant_id: str, caller_scope=None):
    """Run the L1 check over every grant in the tenant."""
    grants = await ScopeGrantRepo(session).list_all(tenant_id)

    def role_permissions_for(row):
        # The grant records the role it narrows (see the model note).
        # A grant with no role named cannot be shown to carry
        # `role.manage`, so it does NOT count toward the preflight:
        # counting it would let the flip pass on somebody who turns out
        # not to be an administrator, which is the exact lockout L1
        # exists to prevent.
        return ROLE_PERMISSIONS.get(getattr(row, "role", "") or "", [])

    return preflight_strict(
        grants, role_permissions_for, caller_scope=caller_scope
    )


@settings_router.put("/scope-enforcement")
async def set_enforcement(
    body: EnforcementRequest,
    user=Depends(require_permission("role.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Switch the tenant between legacy_open and strict.

    Ratified L1: the flip to strict is refused unless at least one
    active, unexpired principal holds a tenant-scope grant containing
    `role.manage`. The refusal names the missing condition and
    **applies nothing** -- there is no partial mode change.
    """
    if body.mode not in ENFORCEMENT_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"mode must be one of {', '.join(ENFORCEMENT_MODES)}",
        )
    forbid_out_of_scope(
        scope, "role.manage",
        what="the tenant's scope enforcement mode", tenant_object=True,
    )

    if body.mode == ENFORCEMENT_STRICT:
        check = await _preflight(session, user.tenant_id, caller_scope=scope)
        if not check.ok:
            # 409, and nothing written. A tenant that locked itself out
            # of its own administration would need the platform-plane
            # break-glass to recover, which is exceptional recovery and
            # must not be the normal path.
            raise HTTPException(status_code=409, detail=check.reason)

    settings = await TenantSettingsRepo(session).set_enforcement(
        user.tenant_id, body.mode, user.user_id
    )
    await AuditRepo(session).append(
        actor=user.user_id, actor_ref=actor_of(user),
        action="scope.enforcement_changed",
        subject=user.tenant_id,
        tenant_id=user.tenant_id,
        detail={"mode": body.mode},
    )
    await session.commit()
    return {
        "tenant_id": user.tenant_id,
        "scope_enforcement": settings.scope_enforcement,
        "updated_by": settings.updated_by,
    }
