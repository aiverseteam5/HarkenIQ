"""Capability Registry API: what the fleet can ACTUALLY do.

The read that answers the question no surface in this platform could
answer before: not "may this action run without a human" (that is
`/api/autonomy`) but "can this action run at all, and on which devices".

Governance
----------
`fleet.view`, exactly matching `/api/autonomy` and for the same reason
(the D2 read-split, landed in S1 and applied consistently in E0.3):
capability is posture, and the people living under the trust ladder can
see it. No new permission is invented, and this router adds NO mutation
at all -- there is nothing here to mutate, because the Registry authors
nothing. It reflects what nodes declare.

Scope
-----
E1.2 all the way down. The device read is scope-filtered in the
repository, so the Registry describes exactly the fleet this principal
may see. A principal with no grants under strict mode sees no devices
and therefore no effective reach -- which is the honest answer, not an
error and not the whole tenant.

Authority
---------
This contract confers none. Capability is not permission, not scope, not
autonomy, not approval and not execution authority; the node's own allow
list remains the final execution authority. A class reported
`available` may still be refused by the stop switch, the error budget,
an approval policy, a precondition or the node itself.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_scope, get_session, require_permission
from harkeniq_cc.actor import actor_of
from harkeniq_cc.auth import UserContext
from harkeniq_cc.capabilities import (
    build_capability_registry,
    device_capability_reason,
)
from harkeniq_cc.governance import load_capability_registry

router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])


@router.get(
    "/",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def capability_registry(
    site_id: str | None = Query(None, description="restrict to one site"),
    action_type: str | None = Query(None, description="one action class"),
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Every governed action class, with its real reach into this fleet.

    Unimplemented classes are REPORTED, never filtered away -- a class
    with full governance and no executor behind it is precisely what
    this endpoint exists to surface.
    """
    return await load_capability_registry(
        session,
        tenant_id=user.tenant_id,
        scope=scope,
        site_id=site_id,
        action_type=action_type,
    )


@router.get(
    "/devices/{device_id}",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def device_capabilities(
    device_id: str,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """One device's declaration, and why each class can or cannot run.

    Three sets are reported separately on purpose. "The code cannot do
    it" and "this node does not permit it" are different problems with
    different fixes, and an operator looking at a device that will not
    act needs to know which one they have.
    """
    from harkeniq_cc.db.models import CCFleetCache, CCSite

    row = await session.get(CCFleetCache, device_id)
    if row is None:
        raise HTTPException(status_code=404, detail="device not found")
    site = await session.get(CCSite, row.site_id)
    if site is None or site.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="device not found")
    # E1.2 layer 2, and 404 rather than 403 for the same reason as
    # /api/fleet/{id}: a 403 confirms the device exists.
    if not scope.covers_device(
        row.agent_id, row.site_id, row.device_class or "server"
    ):
        raise HTTPException(status_code=404, detail="device not found")

    registry = build_capability_registry(
        tenant_id=user.tenant_id, devices=[row], sites=[site]
    )
    declaration = row.capabilities if isinstance(row.capabilities, dict) else None
    classes = []
    for entry in registry["classes"]:
        reason = device_capability_reason(
            {
                "declared": declaration is not None,
                "implemented": sorted(
                    (declaration or {}).get("implemented") or []
                ) if declaration and declaration.get("reach_known") else None,
                "_effective_set": (
                    frozenset((declaration or {}).get("effective") or [])
                    if declaration and declaration.get("reach_known")
                    and (declaration or {}).get("effective") is not None
                    else None
                ),
            },
            entry["action_type"],
            {"implemented": entry["implemented"]},
        )
        classes.append({
            "action_type": entry["action_type"],
            "risk": entry["risk"],
            "reversibility": entry["reversibility"],
            "inverse_action": entry["inverse_action"],
            "implemented": entry["implemented"],
            "implemented_by": entry["implemented_by"],
            "can_execute": reason == "",
            "blocked_by": reason or None,
        })

    return {
        "device": {
            "id": row.id,
            "agent_id": row.agent_id,
            "agent_name": row.agent_name,
            "site_id": row.site_id,
            "site_name": site.site_name,
            "vendor": row.vendor,
            "model": row.model,
            "device_class": row.device_class or "server",
        },
        "declared": declaration is not None,
        "declaration": declaration,
        "classes": classes,
        "fleet": registry["fleet"],
        "contract": registry["contract"],
    }


# ---------------------------------------------------------------------------
# A4: the condition -> capability catalogue (spec A21)
# ---------------------------------------------------------------------------
#
# The Registry above answers "can this action run at all, and where". The
# catalogue answers a different question: "which capability is a CANDIDATE
# for which observed condition". It is the first mutation this router
# carries, and it is deliberately narrow -- the Registry still authors
# nothing, and the catalogue can never contradict it.


class CatalogueEntry(BaseModel):
    subsystem: str = Field(..., min_length=1, max_length=64)
    action_type: str = Field(..., min_length=1, max_length=64)
    because: str = Field("", max_length=512)
    provenance: str = Field("", max_length=255)
    enabled: bool = True


class CatalogueBody(BaseModel):
    """Full replacement, for the reason A0's bindings are.

    An operator reasoning about what their agents may propose should see
    the complete set in one request, not reconstruct it from a history of
    deltas.
    """

    entries: list[CatalogueEntry]


@router.get(
    "/catalogue",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def get_catalogue(
    request: Request,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Which capability is a candidate for which condition.

    Registry reach is joined BESIDE each entry, never merged into it
    (A17.7, applied to a third consumer): "this condition maps to this
    action" and "an executor can currently perform it here" are different
    facts, and an operator debugging a silent agent needs to see which one
    is missing.
    """
    from harkeniq_cc.capability_catalogue import catalogue_view
    from harkeniq_cc.db.repos import CapabilityCatalogueRepo

    rows = await CapabilityCatalogueRepo(session).list_for_tenant(user.tenant_id)
    await session.commit()  # a lazy seed for a new tenant is a real write
    registry = await load_capability_registry(
        session, tenant_id=user.tenant_id, scope=scope,
    )
    return {
        "tenant_id": user.tenant_id,
        **catalogue_view(rows, registry),
    }


@router.put(
    "/catalogue",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def put_catalogue(
    body: CatalogueBody,
    request: Request,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Replace this tenant's catalogue.

    Tenant governance has no site dimension, so changing it is TENANT
    authority -- the same rule S5's autonomy budgets follow. A
    cluster-scoped principal may READ why their agent proposes what it
    does and may not rewrite the mapping for everyone else.

    Refuses on CAPABILITY, never on policy (A17.7). A class whose node
    allow lists happen to exclude it today is a valid entry: an allow list
    is mutable operator policy, and refusing on it would make it
    impossible to configure ahead of a config rollout. What cannot be
    mapped is a class no executor IMPLEMENTS.
    """
    from harkeniq.capabilities import action_facts

    from harkeniq_cc.api.deps import forbid_out_of_scope
    from harkeniq_cc.capability_catalogue import catalogue_view, validate_entry
    from harkeniq_cc.db.repos import AuditRepo, CapabilityCatalogueRepo

    forbid_out_of_scope(
        scope, "site.manage", what="capability catalogue", tenant_object=True,
    )

    facts = action_facts()
    implemented = {k for k, v in facts.items() if v.get("implemented")}
    seen: set[tuple[str, str]] = set()
    for entry in body.entries:
        ok, reason = validate_entry(
            entry.subsystem, entry.action_type,
            known=facts.keys(), implemented=implemented,
        )
        if not ok:
            raise HTTPException(400, reason)
        key = (entry.subsystem.lower(), entry.action_type.upper())
        if key in seen:
            raise HTTPException(
                400,
                f"{entry.action_type} is mapped to {entry.subsystem} twice; "
                f"one condition maps to one action once",
            )
        seen.add(key)

    actor = user.email or user.user_id
    repo = CapabilityCatalogueRepo(session)
    written = await repo.replace(
        user.tenant_id, [e.model_dump() for e in body.entries], actor,
    )
    await AuditRepo(session).append(
        actor=actor,
        actor_ref=actor_of(user),
        action="capability_catalogue.replaced",
        subject=user.tenant_id,
        tenant_id=user.tenant_id,
        detail={
            "entries": written,
            "mappings": sorted(f"{s}:{a}" for s, a in seen),
        },
    )
    await session.commit()
    rows = await repo.list_for_tenant(user.tenant_id)
    return {"tenant_id": user.tenant_id, "entries": written,
            **catalogue_view(rows, None)}
